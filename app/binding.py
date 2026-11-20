"""Salon binding: one shared Telegram bot serves many salons, so a sender links to a salon with /start <slug>."""
from __future__ import annotations

import re

import asyncpg

RESERVED = {"register", "start", "help", "admin", "support", "rembo"}


def slugify(name: str) -> str:
    slug = re.sub(r"[^a-z0-9]+", "-", (name or "").lower()).strip("-")
    return slug[:40] or "salon"


async def ensure_slug(db, salon_id: str, name: str) -> str:
    """A unique invite slug for the salon, kept stable once set. `db` is a pool or a connection, so registration can assign the slug inside
    the same transaction that creates the business. Two businesses asking at the same instant cannot both take a slug: the unique index
    decides, and the loser moves on to the next free one."""
    if hasattr(db, "acquire"):
        async with db.acquire() as con:
            return await ensure_slug(con, salon_id, name)
    con = db
    row = await con.fetchrow("SELECT slug FROM salons WHERE id=$1::uuid", salon_id)
    if row and row["slug"]:
        return row["slug"]
    base = slugify(name)
    if base in RESERVED:
        base = f"{base}-salon"
    taken = {r["slug"] for r in await con.fetch("SELECT slug FROM salons WHERE slug = $1 OR slug LIKE $2", base, base + "-%")}
    slug, n = base, 2
    for _ in range(100):
        while slug in taken:
            slug, n = f"{base}-{n}", n + 1
        try:
            async with con.transaction():           # a savepoint when the caller already has a transaction, so a collision does not abort it
                got = await con.fetchval("UPDATE salons SET slug=$2 WHERE id=$1::uuid AND slug IS NULL RETURNING slug", salon_id, slug)
        except asyncpg.UniqueViolationError:
            taken.add(slug)                         # somebody took it between our look and our write
            continue
        return got or await con.fetchval("SELECT slug FROM salons WHERE id=$1::uuid", salon_id)
    raise RuntimeError("could not find a free invite slug")


async def bind_sender(pool, sender: str, slug: str, days: int = 30) -> str | None:
    """Link a sender to the salon behind a slug. Returns the salon id, or None for an unknown slug."""
    row = await pool.fetchrow("SELECT id::text FROM salons WHERE slug=$1 AND active", slug.lower())
    if row is None:
        return None
    await pool.execute(
        "INSERT INTO sender_salon (sender, salon_id, bound_at, expires_at) VALUES ($1,$2::uuid,now(),now() + ($3 || ' days')::interval) "
        "ON CONFLICT (sender) DO UPDATE SET salon_id=EXCLUDED.salon_id, bound_at=now(), expires_at=now() + ($3 || ' days')::interval",
        sender, row["id"], str(days))
    return row["id"]


async def bound_salon(pool, sender: str) -> str | None:
    row = await pool.fetchrow("SELECT salon_id::text FROM sender_salon WHERE sender=$1 AND expires_at > now()", sender)
    return row["salon_id"] if row else None


async def registration_allowed(pool, sender: str, limit: int = 3) -> bool:
    """At most a few business registrations per sender per day: a mistyped afternoon cannot spawn a directory."""
    n = await pool.fetchval(
        "INSERT INTO onboarding_attempts (sender, day, count) VALUES ($1, CURRENT_DATE, 1) "
        "ON CONFLICT (sender, day) DO UPDATE SET count = onboarding_attempts.count + 1 RETURNING count", sender)
    return int(n or 0) <= limit
