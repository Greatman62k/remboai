"""Everything that touches the calendar. Only code here decides whether a slot is free."""
from __future__ import annotations

from datetime import datetime, timedelta

import asyncpg


async def busy_times(con, staff_ids: list[str], start: datetime, end: datetime) -> dict[str, list[tuple[datetime, datetime]]]:
    rows = await con.fetch(
        "SELECT staff_id::text, starts_at, ends_at FROM bookings WHERE staff_id = ANY($1::uuid[]) "
        "AND status IN ('held','confirmed') AND starts_at < $3 AND ends_at > $2", staff_ids, start, end)
    out: dict[str, list[tuple[datetime, datetime]]] = {}
    for r in rows:
        out.setdefault(r["staff_id"], []).append((r["starts_at"], r["ends_at"]))
    # Time off: a row with no stylist closes the whole salon, so it blocks every stylist asked about.
    off = await con.fetch(
        "SELECT staff_id::text, starts_at, ends_at FROM time_off WHERE starts_at < $3 AND ends_at > $2 AND "
        "(staff_id = ANY($1::uuid[]) OR (staff_id IS NULL AND salon_id IN (SELECT salon_id FROM staff WHERE id = ANY($1::uuid[]))))",
        staff_ids, start, end)
    for r in off:
        for sid in ([r["staff_id"]] if r["staff_id"] else staff_ids):
            out.setdefault(sid, []).append((r["starts_at"], r["ends_at"]))
    return out


async def hold_slot(pool: asyncpg.Pool, *, salon_id: str, staff_id: str, service_id: str, client_id: str,
                    starts_at: datetime, ends_at: datetime, minutes: int) -> str | None:
    """Reserve a slot for `minutes`. Returns the booking id, or None if somebody else holds or has booked it.

    Two defences: a transaction-level advisory lock per stylist makes simultaneous holds queue up instead of
    colliding, and the table's exclusion constraint refuses an overlap no matter what the code did."""
    async with pool.acquire() as con:
        try:
            async with con.transaction():
                await con.execute("SELECT pg_advisory_xact_lock(hashtext($1))", f"hold:{staff_id}")
                if await con.fetchval(
                        "SELECT 1 FROM time_off WHERE salon_id=$1::uuid AND (staff_id IS NULL OR staff_id=$2::uuid) "
                        "AND starts_at < $4 AND ends_at > $3 LIMIT 1", salon_id, staff_id, starts_at, ends_at):
                    return None  # the owner closed that time after the slot was offered
                return await con.fetchval(
                    "INSERT INTO bookings (salon_id, staff_id, service_id, client_id, starts_at, ends_at, hold_expires_at) "
                    "VALUES ($1::uuid,$2::uuid,$3::uuid,$4::uuid,$5,$6, now() + make_interval(mins => $7)) RETURNING id::text",
                    salon_id, staff_id, service_id, client_id, starts_at, ends_at, minutes)
        except (asyncpg.ExclusionViolationError, asyncpg.DeadlockDetectedError):
            return None


async def release(pool: asyncpg.Pool, booking_id: str) -> None:
    await pool.execute("UPDATE bookings SET status='cancelled' WHERE id=$1::uuid AND status='held'", booking_id)


async def expire_holds(pool: asyncpg.Pool) -> int:
    # A hold with a payment prompt still open is kept: the customer may be typing their PIN right now.
    r = await pool.execute(
        "UPDATE bookings b SET status='expired' WHERE b.status='held' AND b.hold_expires_at < now() AND NOT EXISTS "
        "(SELECT 1 FROM payments p WHERE p.booking_id=b.id AND p.status='pending' AND p.created_at > now() - interval '5 minutes')")
    return int(r.split()[-1])
