"""Seed a dedicated Glance staging database with idempotent synthetic cases.

This script has several deliberate target checks. It will not seed the default
Rembo database, an arbitrary database/user, localhost services, or a Glance
deployment without an explicit staging confirmation.
"""
from __future__ import annotations

import asyncio
import json
import os
import re
import sys
from pathlib import Path
from urllib.parse import urlsplit

# Support both `python scripts/seed_glance_staging.py` and module execution.
ROOT = Path(__file__).resolve().parent.parent
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from app.config import get_settings
from app.glance_sessions import validate_security_settings
from app.handoffs import create_handoff, record_customer_message
from app.db import make_pool


EXPECTED_DATABASE = "remboai_glance_staging"
EXPECTED_DATABASE_USER = "rembo_stage"
SEED_CONFIRMATION = "I_CONFIRM_THIS_IS_ISOLATED_GLANCE_STAGING"
_OWNER_ID = re.compile(r"^[1-9][0-9]{4,19}$")


def validate_seed_target(settings, environ: dict[str, str]) -> tuple[str, str, str, str]:
    """Return validated owner/bot/database/Redis identities without revealing secrets."""
    if not bool(getattr(settings, "glance_enabled", False)):
        raise ValueError("GLANCE_ENABLED must be true in the isolated staging environment")
    validate_security_settings(settings)
    if environ.get("GLANCE_STAGING_CONFIRM") != SEED_CONFIRMATION:
        raise ValueError("explicit isolated staging confirmation is required")

    db_host = (environ.get("GLANCE_STAGING_DATABASE_HOST") or "").strip()
    redis_host = (environ.get("GLANCE_STAGING_REDIS_HOST") or "").strip()
    if not db_host or not redis_host:
        raise ValueError("expected staging database and Redis hosts must be configured")
    if environ.get("GLANCE_STAGING_DATABASE_NAME") != EXPECTED_DATABASE:
        raise ValueError("GLANCE_STAGING_DATABASE_NAME must name the dedicated staging database")

    db_url = urlsplit(settings.database_url)
    redis_url = urlsplit(settings.redis_url)
    if db_url.scheme not in {"postgres", "postgresql"} or db_url.hostname != db_host:
        raise ValueError("DATABASE_URL does not match the expected staging database host")
    if db_url.path.lstrip("/") != EXPECTED_DATABASE or db_url.username != EXPECTED_DATABASE_USER:
        raise ValueError("DATABASE_URL must use the dedicated Glance staging database and user")
    if redis_url.scheme not in {"redis", "rediss"} or redis_url.hostname != redis_host:
        raise ValueError("REDIS_URL does not match the expected staging Redis host")

    owner_chat_id = (environ.get("GLANCE_STAGING_OWNER_CHAT_ID") or "").strip()
    if not _OWNER_ID.fullmatch(owner_chat_id):
        raise ValueError("a dedicated synthetic Telegram owner chat ID is required")
    token = (getattr(settings, "telegram_bot_token", "") or "").strip()
    match = re.fullmatch(r"([1-9][0-9]*):[^\s]+", token)
    if not match:
        raise ValueError("a dedicated staging Telegram bot token is required")
    if not (getattr(settings, "glance_cors_origins", "") or "").strip():
        raise ValueError("an exact packaged WebView origin is required before seeding staging")

    return f"tg:{owner_chat_id}", f"tg:{match.group(1)}", db_host, redis_host


async def _get_or_create_salon(pool, *, slug: str, name: str, owner: str, bot_key: str) -> str:
    phone_id = f"glance-stage-synthetic:{slug}"
    async with pool.acquire() as con:
        row = await con.fetchrow(
            "SELECT id::text,name,owner_wa_id,tg_bot_id,active FROM salons WHERE wa_phone_number_id=$1",
            phone_id)
        if row is not None:
            if (row["name"], row["owner_wa_id"], row["tg_bot_id"], row["active"]) != (name, owner, bot_key, True):
                raise RuntimeError("existing synthetic salon metadata differs; refusing to modify it")
            return row["id"]
        return await con.fetchval(
            "INSERT INTO salons (name,wa_phone_number_id,owner_wa_id,tg_bot_id) "
            "VALUES ($1,$2,$3,$4) RETURNING id::text", name, phone_id, owner, bot_key)


async def _get_or_create_customer(pool, salon_id: str, suffix: str) -> tuple[str, str]:
    identity = f"tg:glance-synthetic-customer-{suffix}"
    name = f"[SYNTHETIC] Glance Customer {suffix.upper()}"
    async with pool.acquire() as con:
        row = await con.fetchrow(
            "SELECT id::text,name FROM clients WHERE salon_id=$1::uuid AND wa_id=$2", salon_id, identity)
        if row is not None:
            if row["name"] != name:
                raise RuntimeError("existing synthetic customer metadata differs; refusing to modify it")
            return row["id"], identity
        client_id = await con.fetchval(
            "INSERT INTO clients (salon_id,wa_id,name) VALUES ($1::uuid,$2,$3) RETURNING id::text",
            salon_id, identity, name)
    return client_id, identity


async def _seed_case(pool, salon_id: str, client_id: str, customer: str, suffix: str) -> str:
    initial_id = f"glance-stage-seed-{suffix}-initial"
    async with pool.acquire() as con:
        cases = await con.fetch(
            "SELECT id::text,status FROM handoffs WHERE salon_id=$1::uuid AND client_id=$2::uuid "
            "ORDER BY created_at,id", salon_id, client_id)
    open_cases = [case for case in cases if case["status"] == "open"]
    if len(open_cases) > 1:
        raise RuntimeError("multiple open synthetic cases found; refusing to alter staging data")
    if open_cases:
        case_id = open_cases[0]["id"]
        async with pool.acquire() as con:
            initial_exists = await con.fetchval(
                "SELECT EXISTS (SELECT 1 FROM handoff_events WHERE handoff_id=$1::uuid "
                "AND provider_message_id=$2 AND event_type='customer_message')", case_id, initial_id)
        if not initial_exists:
            raise RuntimeError("an unrelated open case exists for a synthetic customer; refusing to modify it")
    elif cases:
        raise RuntimeError("a previous seeded case is resolved; use a fresh isolated staging database")
    else:
        created = await create_handoff(
            pool, salon_id, client_id, customer, "human_request",
            f"[SYNTHETIC] Please help with a staging test at salon {suffix.upper()}.",
            source_message_id=initial_id)
        case_id = created["id"]

    # Stable provider IDs make each append a no-op on reruns while preserving
    # exactly eleven customer-visible seed events for the latest-ten check.
    for index in range(1, 11):
        await record_customer_message(
            pool, salon_id, customer,
            f"[SYNTHETIC] Context event {index:02d} for staging case {suffix.upper()}.",
            f"glance-stage-seed-{suffix}-context-{index:02d}")
    return case_id


async def seed() -> dict:
    settings = get_settings()
    owner, bot_key, db_host, redis_host = validate_seed_target(settings, os.environ)
    pool = await make_pool(settings.database_url)
    try:
        async with pool.acquire() as con:
            identity = await con.fetchrow("SELECT current_database() AS db,current_user AS db_user")
            if identity["db"] != EXPECTED_DATABASE or identity["db_user"] != EXPECTED_DATABASE_USER:
                raise RuntimeError("connected PostgreSQL identity does not match the isolated staging target")
            applied = await con.fetchval(
                "SELECT EXISTS (SELECT 1 FROM schema_migrations WHERE name='048_glance_day2_workflow.sql')")
            if not applied:
                raise RuntimeError("migration 047 is not applied; start the staging app through its normal migration path first")
            foreign_salons = await con.fetchval(
                "SELECT count(*) FROM salons WHERE wa_phone_number_id NOT LIKE 'glance-stage-synthetic:%' "
                "OR name NOT LIKE '[SYNTHETIC] Glance Stage %' "
                "OR owner_wa_id IS DISTINCT FROM $1 OR tg_bot_id IS DISTINCT FROM $2",
                owner, bot_key)
            foreign_clients = await con.fetchval(
                "SELECT count(*) FROM clients WHERE wa_id NOT LIKE 'tg:glance-synthetic-customer-%' "
                "OR name IS NULL OR name NOT LIKE '[SYNTHETIC] Glance Customer %'")
            if foreign_salons or foreign_clients:
                raise RuntimeError("staging database contains non-synthetic tenant data; refusing to seed")

        result = {"database": EXPECTED_DATABASE, "database_host": db_host, "redis_host": redis_host,
                  "salons": [], "cases": []}
        for suffix in ("a", "b"):
            name = f"[SYNTHETIC] Glance Stage Salon {suffix.upper()}"
            salon_id = await _get_or_create_salon(
                pool, slug=f"salon-{suffix}", name=name, owner=owner, bot_key=bot_key)
            client_id, customer = await _get_or_create_customer(pool, salon_id, suffix)
            case_id = await _seed_case(pool, salon_id, client_id, customer, suffix)
            result["salons"].append({"id": salon_id, "label": name})
            result["cases"].append({"salon_id": salon_id, "case_id": case_id, "visible_seed_events": 11})
        return result
    finally:
        await pool.close()


def main() -> int:
    try:
        result = asyncio.run(seed())
    except ValueError as exc:
        # These controlled validation messages never include configured values.
        print(f"Glance staging seed refused: {exc}", file=sys.stderr)
        return 1
    except RuntimeError as exc:
        print(f"Glance staging seed refused: {exc}", file=sys.stderr)
        return 1
    except Exception as exc:
        # Never print a DSN, bot token, owner chat ID or driver exception payload.
        print(f"Glance staging seed failed: {exc.__class__.__name__} (details suppressed)", file=sys.stderr)
        return 1
    print(json.dumps(result, indent=2, sort_keys=True))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
