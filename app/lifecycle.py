"""Business lifecycle: quiet salons get two warnings, then are deleted.

A salon counts as active when a booking was created recently; anything else (browsing,
chatter) does not reset the clock. Warnings go to the owner in their own language when
known. Any new booking, or a KEEP reply, cancels the countdown. The daily retention
loop runs the sweep."""
from __future__ import annotations

import logging
from datetime import datetime
from zoneinfo import ZoneInfo

from .names import LABEL
from .channels import outbound
from .channels.whatsapp import mask
from .db import clear_salon_cache
from .messages import t

log = logging.getLogger("rembo.lifecycle")

WARN_DAYS = 7
FINAL_DAYS = 13
DELETE_DAYS = 14
MAX_HOLD_BACK_DAYS = 60   # unresolved refunds or manual claims delay deletion, but never past this


async def delete_salon(pool, salon_id: str) -> dict:
    """Remove a salon and everything that cannot outlive it. Tables with
    ON DELETE CASCADE follow the salon row on their own; bookings and payments
    do not cascade, so they go first."""
    out = {}
    out["reviews"] = int((await pool.execute(
        "DELETE FROM payment_reviews WHERE payment_id IN (SELECT id FROM payments WHERE salon_id=$1::uuid)", salon_id)).split()[-1])
    out["callbacks"] = int((await pool.execute(
        "DELETE FROM mpesa_callbacks c USING payments p WHERE p.salon_id=$1::uuid "
        "AND c.checkout_request_id=p.checkout_request_id", salon_id)).split()[-1])
    out["payments"] = int((await pool.execute("DELETE FROM payments WHERE salon_id=$1::uuid", salon_id)).split()[-1])
    await pool.execute("DELETE FROM visit_checks WHERE salon_id=$1::uuid", salon_id)
    await pool.execute("DELETE FROM agent_turns WHERE salon_id=$1::uuid", salon_id)
    await pool.execute("DELETE FROM conversation_trace WHERE salon_id=$1::uuid", salon_id)
    await pool.execute("DELETE FROM reminder_jobs WHERE salon_id=$1::uuid", salon_id)
    out["bookings"] = int((await pool.execute("DELETE FROM bookings WHERE salon_id=$1::uuid", salon_id)).split()[-1])
    out["salons"] = int((await pool.execute("DELETE FROM salons WHERE id=$1::uuid", salon_id)).split()[-1])
    clear_salon_cache()
    return out


async def money_unresolved(pool, salon_id: str) -> bool:
    """A refund still owed, a payment awaiting review, or a manual claim the owner has not answered. Deleting the salon would delete the
    only record of it. (An abandoned STK prompt does not count: nobody can act on it.)"""
    return bool(await pool.fetchval(
        "SELECT 1 FROM payments p WHERE p.salon_id=$1::uuid AND (p.status IN ('needs_refund','review') OR (p.status='pending' AND p.method='manual')) "
        "UNION ALL SELECT 1 FROM payment_reviews r JOIN payments p ON p.id=r.payment_id "
        "WHERE p.salon_id=$1::uuid AND r.status IN ('pending_review','refund_due') LIMIT 1", salon_id))


async def delete_salon_and_notify(ctx, salon_id: str) -> dict:
    """Delete a salon and tell every upcoming customer, so nobody turns up to a dead business.
    Returns the delete_salon counts plus how many customers were told."""
    salon = await ctx.pool.fetchrow(
        "SELECT id::text, " + LABEL + " AS name, wa_phone_number_id, wa_access_token, timezone, owner_wa_id FROM salons WHERE id=$1::uuid", salon_id)
    upcoming = []
    if salon is not None:
        upcoming = [dict(r) for r in await ctx.pool.fetch(
            "SELECT b.starts_at, c.wa_id, c.lang FROM bookings b JOIN clients c ON c.id=b.client_id "
            "WHERE b.salon_id=$1::uuid AND b.status IN ('held','confirmed') AND b.starts_at > now() ORDER BY b.starts_at", salon_id)]
    counts = await delete_salon(ctx.pool, salon_id)
    counts["notified"] = 0
    if salon is None:
        return counts
    tz = ZoneInfo(salon["timezone"] or "Africa/Nairobi")
    async with outbound.message_scope(f"salon-delete:{salon_id}"):
        for u in upcoming:
            when = u["starts_at"].astimezone(tz).strftime("%a %d %b, %H:%M")
            try:
                await outbound.send_text(ctx, dict(salon), u["wa_id"],
                                         t(u["lang"] or "en", "closed_cancelled", salon=salon["name"], when=when))
                counts["notified"] += 1
            except Exception:
                log.exception("could not tell %s about the deletion", mask(u["wa_id"] or ""))
    return counts


async def owner_lang(pool, salon_id: str, owner_wa_id: str | None) -> str:
    if owner_wa_id:
        lang = await pool.fetchval("SELECT lang FROM clients WHERE salon_id=$1::uuid AND wa_id=$2", salon_id, owner_wa_id)
        if lang in ("en", "sw"):
            return lang
    return "en"


# One pass over bookings. A correlated "(SELECT max(...) WHERE salon_id=s.id)" scanned the whole table once per salon: 14.9 s against 0.24 s at 1.26M bookings.
SALONS_WITH_LAST_BOOKING_SQL = (
    "SELECT s.id::text, s.name, s.wa_phone_number_id, s.wa_access_token, s.owner_wa_id, "
    "s.inactivity_warned_at, s.inactivity_final_at, s.created_at, lb.last_booking "
    "FROM salons s LEFT JOIN (SELECT salon_id, max(created_at) AS last_booking FROM bookings GROUP BY salon_id) lb ON lb.salon_id=s.id "
    "WHERE s.active")


async def sweep(ctx) -> dict:
    """Warn and delete quiet salons. Returns what happened, per salon id."""
    now = datetime.now(ZoneInfo("UTC"))
    rows = await ctx.pool.fetch(SALONS_WITH_LAST_BOOKING_SQL)
    done: dict = {"warned": [], "final": [], "deleted": [], "reset": []}
    for r in rows:
        salon = dict(r)
        base = salon["last_booking"] or salon["created_at"]
        age = (now - base).total_seconds() / 86400
        if age < WARN_DAYS:
            if salon["inactivity_warned_at"] or salon["inactivity_final_at"]:
                await ctx.pool.execute("UPDATE salons SET inactivity_warned_at=NULL, inactivity_final_at=NULL WHERE id=$1::uuid", salon["id"])
                done["reset"].append(salon["id"])
            continue
        if age >= DELETE_DAYS and age < MAX_HOLD_BACK_DAYS and await money_unresolved(ctx.pool, salon["id"]):
            log.warning("not deleting quiet salon %s (%s): money is unresolved", salon["id"], salon["name"])
            continue
        if not salon["owner_wa_id"]:
            if age >= DELETE_DAYS:
                log.warning("deleting ownerless salon %s (%s) after %.0f quiet days", salon["id"], salon["name"], age)
                await delete_salon_and_notify(ctx, salon["id"])
                done["deleted"].append(salon["id"])
            continue
        lang = await owner_lang(ctx.pool, salon["id"], salon["owner_wa_id"])
        if age < FINAL_DAYS:
            if not salon["inactivity_warned_at"]:
                await outbound.send_text(ctx, salon, salon["owner_wa_id"], t(lang, "owner_warn1", salon=salon["name"]))
                await ctx.pool.execute("UPDATE salons SET inactivity_warned_at=now() WHERE id=$1::uuid", salon["id"])
                done["warned"].append(salon["id"])
            continue
        if age < DELETE_DAYS:
            if not salon["inactivity_final_at"]:
                await outbound.send_text(ctx, salon, salon["owner_wa_id"], t(lang, "owner_warn_final", salon=salon["name"]))
                await ctx.pool.execute("UPDATE salons SET inactivity_final_at=now() WHERE id=$1::uuid", salon["id"])
                done["final"].append(salon["id"])
            continue
        await delete_salon_and_notify(ctx, salon["id"])
        done["deleted"].append(salon["id"])
        try:
            await outbound.send_plain(ctx, salon["owner_wa_id"], t(lang, "owner_deleted", salon=salon["name"]))
        except Exception:
            log.exception("could not tell %s about the deletion", mask(salon["owner_wa_id"] or ""))
    return done
