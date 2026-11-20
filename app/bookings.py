"""Everything that touches the calendar. Only code here decides whether a slot is free."""
from __future__ import annotations

from datetime import datetime, timedelta

import asyncpg


async def busy_times(con, staff_ids: list[str], start: datetime, end: datetime) -> dict[str, list[tuple[datetime, datetime]]]:
    rows = await con.fetch(
        "SELECT staff_id::text, starts_at, ends_at FROM bookings WHERE staff_id = ANY($1::uuid[]) "
        "AND status IN ('held','confirmed') AND (status = 'confirmed' OR hold_expires_at > now()) "
        "AND starts_at < $3 AND ends_at > $2", staff_ids, start, end)
    out: dict[str, list[tuple[datetime, datetime]]] = {}
    for r in rows:
        out.setdefault(r["staff_id"], []).append((r["starts_at"], r["ends_at"]))
    off = await con.fetch(
        "SELECT staff_id::text, starts_at, ends_at FROM time_off WHERE starts_at < $3 AND ends_at > $2 AND "
        "(staff_id = ANY($1::uuid[]) OR (staff_id IS NULL AND salon_id IN (SELECT salon_id FROM staff WHERE id = ANY($1::uuid[]))))",
        staff_ids, start, end)
    for r in off:
        for sid in ([r["staff_id"]] if r["staff_id"] else staff_ids):
            out.setdefault(sid, []).append((r["starts_at"], r["ends_at"]))
    return out


async def hold_slot(pool: asyncpg.Pool, *, salon_id: str, staff_id: str, service_id: str, client_id: str,
                    starts_at: datetime, ends_at: datetime, minutes: int, message_id: str | None = None) -> str | None:
    """Reserve a slot for `minutes`. Returns the booking id, or None if somebody else holds or has booked it.

    Two defences: a transaction-level advisory lock per stylist makes simultaneous holds queue up instead of
    colliding, and the table's exclusion constraint refuses an overlap no matter what the code did."""
    async with pool.acquire() as con:
        try:
            async with con.transaction():
                await con.execute("SELECT pg_advisory_xact_lock(hashtext($1))", f"hold:{staff_id}")
                if message_id:
                    mine = await con.fetchval(
                        "UPDATE bookings SET hold_expires_at = CASE WHEN status='held' THEN now() + make_interval(mins => $3) ELSE hold_expires_at END "
                        "WHERE source_message_id=$1 AND client_id=$2::uuid AND status IN ('held','confirmed') RETURNING id::text", message_id, client_id, minutes)
                    if mine:
                        return mine
                await expire_stale_holds(con, staff_id)
                if await con.fetchval(
                        "SELECT 1 FROM time_off WHERE salon_id=$1::uuid AND (staff_id IS NULL OR staff_id=$2::uuid) "
                        "AND starts_at < $4 AND ends_at > $3 LIMIT 1", salon_id, staff_id, starts_at, ends_at):
                    return None
                return await con.fetchval(
                    "INSERT INTO bookings (salon_id, staff_id, service_id, client_id, starts_at, ends_at, hold_expires_at, source_message_id) "
                    "VALUES ($1::uuid,$2::uuid,$3::uuid,$4::uuid,$5,$6, now() + make_interval(mins => $7), $8) RETURNING id::text",
                    salon_id, staff_id, service_id, client_id, starts_at, ends_at, minutes, message_id)
        except (asyncpg.ExclusionViolationError, asyncpg.DeadlockDetectedError):
            return None


async def release(pool: asyncpg.Pool, booking_id: str) -> None:
    await pool.execute("UPDATE bookings SET status='cancelled' WHERE id=$1::uuid AND status='held'", booking_id)


_EXPIRE_SQL = (
    "UPDATE bookings b SET status='expired' WHERE b.status='held' AND b.hold_expires_at < now() "
    "AND ($1::uuid IS NULL OR b.staff_id=$1::uuid) "
    "AND NOT EXISTS (SELECT 1 FROM payments p WHERE p.booking_id=b.id AND p.status='pending' AND p.created_at > now() - interval '5 minutes') "
    "RETURNING b.id")


async def expire_stale_holds(con, staff_id: str | None = None) -> int:
    """Mark lapsed holds as expired (all of them, or one stylist's). Availability already treats a lapsed hold as free, but the
    exclusion constraint only lets go of it once its status changes, so anything about to take a slot calls this first.
    Three statements however many holds lapse, not two more for each one."""
    ids = [r["id"] for r in await con.fetch(_EXPIRE_SQL, staff_id)]
    if ids:
        await con.execute("UPDATE reminder_jobs SET status='cancelled' WHERE booking_id = ANY($1::uuid[]) AND status='pending'", ids)
        await con.execute("UPDATE payments SET result_desc='booking hold expired; owner verification still pending', updated_at=now() "
                          "WHERE booking_id = ANY($1::uuid[]) AND status='pending' AND method='manual'", ids)
    return len(ids)


async def expire_holds(pool: asyncpg.Pool) -> int:
    async with pool.acquire() as con:
        return await expire_stale_holds(con)
