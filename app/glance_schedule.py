"""Salon-scoped, read-only schedule data for RemboAI Glance."""
from __future__ import annotations

from datetime import date, datetime, time, timedelta
from zoneinfo import ZoneInfo


def _salon_zone(value: str | None) -> ZoneInfo:
    try:
        return ZoneInfo(value or "UTC")
    except Exception:
        return ZoneInfo("UTC")


async def list_schedule(pool, salon_id: str, owner_identity: str, schedule_date: date | None = None,
                        limit: int = 20, offset: int = 0) -> dict:
    """Return active bookings for one local salon day.

    This mirrors the owner command's booking eligibility: confirmed bookings
    and still-live held bookings are visible, while expired holds are omitted.
    Only display-safe business fields are selected. Phone numbers, payment
    amounts, payment references and evidence never enter this read model.
    """
    limit = max(1, min(int(limit), 50))
    offset = max(0, min(int(offset), 1000))
    async with pool.acquire() as con:
        async with con.transaction():
            salon = await con.fetchrow(
                "SELECT id::text AS id,timezone,owner_wa_id FROM salons "
                "WHERE id=$1::uuid AND active FOR SHARE", salon_id)
            if salon is None or not salon["owner_wa_id"] or salon["owner_wa_id"].strip().lstrip("+") != (owner_identity or "").strip().lstrip("+"):
                raise PermissionError("actor is not the salon owner")

            zone = _salon_zone(salon["timezone"])
            schedule_date = schedule_date or datetime.now(zone).date()
            start = datetime.combine(schedule_date, time.min, zone)
            # Adding a calendar day in the salon timezone avoids treating a
            # daylight-saving transition as a fixed 24-hour UTC interval.
            next_day = schedule_date + timedelta(days=1)
            end = datetime.combine(next_day, time.min, zone)
            rows = await con.fetch(
                "SELECT b.id::text AS booking_id,b.starts_at,b.ends_at,b.status,"
                "c.name AS customer_name,sv.name AS service_name,st.name AS staff_name,"
                "CASE WHEN EXISTS (SELECT 1 FROM payments p WHERE p.booking_id=b.id "
                "AND p.salon_id=b.salon_id AND p.status='paid') THEN 'paid' "
                "WHEN b.status='held' THEN 'awaiting_deposit' "
                "ELSE 'no_deposit_recorded' END AS payment_state "
                "FROM bookings b "
                "JOIN clients c ON c.id=b.client_id AND c.salon_id=b.salon_id "
                "JOIN services sv ON sv.id=b.service_id AND sv.salon_id=b.salon_id "
                "JOIN staff st ON st.id=b.staff_id AND st.salon_id=b.salon_id "
                "WHERE b.salon_id=$1::uuid AND b.status IN ('confirmed','held') "
                "AND b.starts_at >= $2 AND b.starts_at < $3 "
                "AND (b.status='confirmed' OR b.hold_expires_at > now()) "
                "ORDER BY b.starts_at,b.id LIMIT $4 OFFSET $5",
                salon_id, start, end, limit + 1, offset)
            return {
                "date": schedule_date.isoformat(),
                "timezone": zone.key,
                "items": [dict(row) for row in rows],
                "limit": limit,
                "offset": offset,
            }
