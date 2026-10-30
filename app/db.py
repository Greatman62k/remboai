from pathlib import Path
import logging
import time
import asyncpg

log = logging.getLogger("rembo.db")

MIGRATIONS = Path(__file__).resolve().parent.parent / "migrations"

MIN_POOL = 3

async def make_pool(dsn: str, max_size: int = 10) -> asyncpg.Pool:
    max_size = max(MIN_POOL, int(max_size))
    async def init(conn):
        import json
        def _enc(v):
            if isinstance(v, str):
                return v
            return json.dumps(v)
        await conn.set_type_codec("jsonb", encoder=_enc, decoder=lambda x: __import__("json").loads(x) if isinstance(x, str) else x, schema="pg_catalog")
    try:
        return await asyncpg.create_pool(dsn, min_size=1, max_size=max_size, init=init)
    except Exception:
        log.warning("connection pool without the jsonb codec: jsonb columns will come back as strings")
        return await asyncpg.create_pool(dsn, min_size=1, max_size=max_size)

async def migrate(pool: asyncpg.Pool, directory: Path = MIGRATIONS) -> list[str]:
    applied_now: list[str] = []
    async with pool.acquire() as con:
        await con.execute("SELECT pg_advisory_lock(hashtext('remboai:migrations'))")
        try:
            await _migrate_locked(con, directory, applied_now)
        finally:
            await con.execute("SELECT pg_advisory_unlock(hashtext('remboai:migrations'))")
    return applied_now


async def _migrate_locked(con: asyncpg.Connection, directory: Path, applied_now: list[str]) -> None:
    await con.execute("CREATE TABLE IF NOT EXISTS schema_migrations (name text PRIMARY KEY, applied_at timestamptz NOT NULL DEFAULT now())")
    done = {r["name"] for r in await con.fetch("SELECT name FROM schema_migrations")}
    for f in sorted(directory.glob("*.sql")):
        if f.name in done:
            continue
        async with con.transaction():
            await con.execute(f.read_text())
            await con.execute("INSERT INTO schema_migrations (name) VALUES ($1)", f.name)
        applied_now.append(f.name)

_SALON_SQL = "SELECT id::text AS id, name, wa_phone_number_id, wa_business_number, owner_wa_id, wa_access_token, timezone, deposit_kes, open_hours, tg_bot_id, active FROM salons WHERE wa_phone_number_id = $1"
_salon_cache: dict[str, tuple[float, dict | None]] = {}

def clear_salon_cache(phone_number_id: str | None = None) -> None:
    if phone_number_id is None:
        _salon_cache.clear()
    else:
        _salon_cache.pop(phone_number_id, None)

async def salon_by_phone_id(pool: asyncpg.Pool, phone_number_id: str, settings=None) -> dict | None:
    hit = _salon_cache.get(phone_number_id)
    if hit and hit[0] > time.monotonic():
        return hit[1]
    row = await pool.fetchrow(_SALON_SQL, phone_number_id)
    salon = dict(row) if row else None
    if salon is not None:
        if settings is None:
            from .config import get_settings
            settings = get_settings()
        try:
            from .token_crypto import decrypt_salon_tokens
            salon = decrypt_salon_tokens(salon, settings)
        except Exception:
            log.warning("stored tokens for salon %s cannot be decrypted; using the platform token", salon.get("id"))
            salon["wa_access_token"] = None
    _salon_cache[phone_number_id] = (time.monotonic() + 60, salon)
    return salon
