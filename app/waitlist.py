"""Waitlist fill: a cancelled slot is offered to waiting customers in join order.

The first waiter to tap Yes within 10 minutes books the exact freed slot
(same stylist, same start and end). No, expiry, or a taken slot moves the
offer to the next waiter. Only code here decides who gets the freed slot.
"""
from __future__ import annotations

import logging
from datetime import datetime, timedelta
from zoneinfo import ZoneInfo

from .channels import outbound
from .messages import t

log = logging.getLogger("rembo.waitlist")

OFFER_MINUTES = 10


async def join(pool, salon_id: str, client_id: str, service_id: str, day) -> str:
    """Wait for a freed slot for this service and day. Idempotent: one open
    entry per customer, service, and day. Returns 'joined' or 'already'."""
    existing = await pool.fetchval(
        "SELECT 1 FROM waitlist WHERE salon_id=$1::uuid AND client_id=$2::uuid AND service_id=$3::uuid "
        "AND day=$4 AND status IN ('waiting','offered')", salon_id, client_id, service_id, day)
    if existing:
        return "already"
    await pool.execute(
        "INSERT INTO waitlist (salon_id, client_id, service_id, day) VALUES ($1::uuid,$2::uuid,$3::uuid,$4)",
        salon_id, client_id, service_id, day)
    return "joined"


async def _offer_next(ctx, salon: dict, staff_id: str, staff_name: str, service_id: str,
                      service_name: str, starts_at: datetime, ends_at: datetime) -> bool:
    """Offer the freed slot to the oldest waiting customer. True when an
    offer went out, False when nobody is waiting."""
    from .bookings import busy_times
    timezone = ZoneInfo(salon.get("timezone") or "Africa/Nairobi")
    local_start = starts_at.astimezone(timezone)
    day = local_start.date()
    while True:
        row = await ctx.pool.fetchrow(
            "SELECT w.id::text AS id, c.wa_id AS phone, c.lang FROM waitlist w JOIN clients c ON c.id=w.client_id "
            "WHERE w.salon_id=$1::uuid AND w.service_id=$2::uuid AND w.day=$3 AND w.status='waiting' "
            "ORDER BY w.created_at LIMIT 1", salon["id"], service_id, day)
        if row is None:
            return False
        async with ctx.pool.acquire() as con:
            busy = await busy_times(con, [staff_id], starts_at, ends_at)
        if busy.get(staff_id):
            return False
        taken = await ctx.pool.fetchval(
            "SELECT 1 FROM bookings WHERE client_id=(SELECT client_id FROM waitlist WHERE id=$1::uuid) "
            "AND status IN ('held','confirmed') AND starts_at < $3 AND ends_at > $2", row["id"], starts_at, ends_at)
        if taken:
            await ctx.pool.execute("UPDATE waitlist SET status='expired' WHERE id=$1::uuid", row["id"])
            continue
        lang = row["lang"] or "en"
        body = t(lang, "waitlist_offer", service=service_name,
                 when=local_start.strftime("%a %d %b, %H:%M"), stylist=staff_name)
        buttons = [(f"wait-yes:{row['id']}", t(lang, "waitlist_btn_yes")[:20]),
                   (f"wait-no:{row['id']}", t(lang, "waitlist_btn_no")[:20])]
        try:
            sent = await outbound.send_buttons(ctx, salon, row["phone"], body, buttons)
        except Exception:
            log.exception("waitlist offer failed for %s", row["id"])
            sent = False
        if not sent:
            return False
        await ctx.pool.execute(
            "UPDATE waitlist SET status='offered', notified_at=now(), offer_staff_id=$2::uuid, "
            "offer_starts_at=$3, offer_ends_at=$4, offer_expires_at=now() + make_interval(mins => $5) "
            "WHERE id=$1::uuid AND status='waiting'", row["id"], staff_id, starts_at, ends_at, OFFER_MINUTES)
        return True


async def offer_freed_slot(ctx, salon: dict, staff_id: str, staff_name: str, service_id: str,
                           service_name: str, starts_at: datetime, ends_at: datetime) -> None:
    """A cancellation freed this slot: hand it to the waitlist, if any."""
    if starts_at is None or starts_at.tzinfo is None or starts_at <= datetime.now(starts_at.tzinfo):
        return
    try:
        await _offer_next(ctx, salon, staff_id, staff_name, service_id, service_name, starts_at, ends_at)
    except Exception:
        log.exception("waitlist offer failed for salon %s", salon.get("id"))


async def offer_for_booking(ctx, salon: dict, booking_id: str) -> None:
    """Offer a just-cancelled booking's slot onward. Reads the booking row so
    every cancel path (customer, late tap, owner) shares one call."""
    row = await ctx.pool.fetchrow(
        "SELECT b.staff_id::text AS staff_id, s.name AS staff_name, b.service_id::text AS service_id, "
        "sv.name AS service, b.starts_at, b.ends_at, sl.timezone FROM bookings b "
        "JOIN staff s ON s.id=b.staff_id JOIN services sv ON sv.id=b.service_id "
        "JOIN salons sl ON sl.id=b.salon_id WHERE b.id=$1::uuid", booking_id)
    if row is None:
        return
    salon = {**salon, "timezone": row["timezone"]}
    await offer_freed_slot(ctx, salon, row["staff_id"], row["staff_name"], row["service_id"],
                           row["service"], row["starts_at"], row["ends_at"])


async def handle_tap(ctx, salon: dict, client: dict, st: dict, chat, lang: str, tz, reply_id: str) -> bool:
    """Yes books the freed slot, No passes it to the next waiter. True when handled."""
    from . import payments
    from .booking_flow import _prompt, _reply, _save, when_label
    from .bookings import hold_slot
    from .reminders import schedule_reminders
    kind, _, wid = reply_id.partition(":")
    if kind not in ("wait-yes", "wait-no") or not wid:
        return False
    row = await ctx.pool.fetchrow(
        "SELECT w.id::text AS id, w.status, w.offer_staff_id::text AS staff_id, s.name AS staff_name, "
        "w.offer_starts_at AS starts_at, w.offer_ends_at AS ends_at, w.offer_expires_at AS expires_at, "
        "w.service_id::text AS service_id, sv.name AS service, sv.price_kes AS price "
        "FROM waitlist w JOIN staff s ON s.id=w.offer_staff_id JOIN services sv ON sv.id=w.service_id "
        "WHERE w.id=$1::uuid AND w.salon_id=$2::uuid AND w.client_id=$3::uuid",
        wid, salon["id"], client["id"])
    if row is None or row["status"] not in ("offered", "booked"):
        await _reply(ctx, salon["id"], chat.to, st, chat, t(lang, "waitlist_gone"))
        return True
    row = dict(row)
    if row["status"] == "booked":
        return True
    if kind == "wait-no":
        await ctx.pool.execute("UPDATE waitlist SET status='declined' WHERE id=$1::uuid", wid)
        await _reply(ctx, salon["id"], chat.to, st, chat, t(lang, "waitlist_declined", salon=salon["name"]))
        await _offer_next(ctx, salon, row["staff_id"], row["staff_name"], row["service_id"],
                          row["service"], row["starts_at"], row["ends_at"])
        return True
    if row["expires_at"] is not None and datetime.now(row["expires_at"].tzinfo) > row["expires_at"]:
        await ctx.pool.execute("UPDATE waitlist SET status='expired' WHERE id=$1::uuid", wid)
        await _reply(ctx, salon["id"], chat.to, st, chat, t(lang, "waitlist_expired"))
        await _offer_next(ctx, salon, row["staff_id"], row["staff_name"], row["service_id"],
                          row["service"], row["starts_at"], row["ends_at"])
        return True
    booking_id = await hold_slot(
        ctx.pool, salon_id=salon["id"], staff_id=row["staff_id"], service_id=row["service_id"],
        client_id=client["id"], starts_at=row["starts_at"], ends_at=row["ends_at"],
        minutes=ctx.settings.hold_minutes, message_id=f"waitlist:{wid}")
    if booking_id is None:
        await ctx.pool.execute("UPDATE waitlist SET status='expired' WHERE id=$1::uuid", wid)
        await _reply(ctx, salon["id"], chat.to, st, chat, t(lang, "waitlist_taken", service=row["service"]))
        await _offer_next(ctx, salon, row["staff_id"], row["staff_name"], row["service_id"],
                          row["service"], row["starts_at"], row["ends_at"])
        return True
    await ctx.pool.execute("UPDATE waitlist SET status='booked' WHERE id=$1::uuid", wid)
    when = when_label(row["starts_at"], tz)
    acct = await payments.account(ctx.pool, salon["id"])
    amount = payments.deposit_amount({**salon, "deposit_kes": salon.get("deposit_kes", 0)},
                                     {"price_kes": row["price"]}) if acct else 0
    if amount == 0:
        await ctx.pool.execute("UPDATE bookings SET status='confirmed', hold_expires_at=NULL WHERE id=$1::uuid AND status='held'", booking_id)
        try:
            await schedule_reminders(ctx.pool, booking_id)
        except Exception:
            log.exception("could not schedule reminders for waitlist booking %s", booking_id)
        state = {"history": st.get("history", [])}
        await _save(ctx, salon["id"], chat.to, state)
        await _reply(ctx, salon["id"], chat.to, state, chat,
                     t(lang, "booked_free", service=row["service"], when=when, stylist=row["staff_name"], salon=salon["name"]))
        if salon.get("owner_wa_id"):
            await outbound.send_text(ctx, salon, salon["owner_wa_id"],
                                     t("en", "owner_booked_free", client=client.get("name") or chat.to,
                                       service=row["service"], when=when, stylist=row["staff_name"]))
        return True
    held_state = {"state": "held", "booking_id": booking_id, "amount": amount, "service_id": row["service_id"],
                  "service_name": row["service"], "held_when": when, "held_stylist": row["staff_name"],
                  "history": st.get("history", [])}
    import app.mpesa as mpesa
    phone = mpesa.normalize_msisdn(chat.to)
    if phone is None:
        await _save(ctx, salon["id"], chat.to, held_state)
        await _reply(ctx, salon["id"], chat.to, held_state, chat,
                     t(lang, "held", service=row["service"], when=when, stylist=row["staff_name"], mins=ctx.settings.hold_minutes)
                     + "\n" + t(lang, "ask_number", amount=amount))
        return True
    held_state["phone"] = phone
    await _save(ctx, salon["id"], chat.to, held_state)
    await _reply(ctx, salon["id"], chat.to, held_state, chat,
                 t(lang, "held", service=row["service"], when=when, stylist=row["staff_name"], mins=ctx.settings.hold_minutes)
                 + "\n" + await _prompt(ctx, salon, booking_id, phone, amount, lang))
    return True


async def sweep_expired(ctx) -> int:
    """Offers left untapped past their 10 minutes go to the next waiter. Runs every minute."""
    rows = await ctx.pool.fetch(
        "SELECT w.id::text AS id, w.salon_id::text AS salon_id, w.offer_staff_id::text AS staff_id, s.name AS staff_name, "
        "w.service_id::text AS service_id, sv.name AS service, w.offer_starts_at AS starts_at, w.offer_ends_at AS ends_at, "
        "sl.name AS salon_name, sl.owner_wa_id, sl.wa_phone_number_id, sl.wa_access_token, sl.tg_bot_id, sl.deposit_kes "
        "FROM waitlist w JOIN staff s ON s.id=w.offer_staff_id JOIN services sv ON sv.id=w.service_id "
        "JOIN salons sl ON sl.id=w.salon_id "
        "WHERE w.status='offered' AND w.offer_expires_at <= now() LIMIT 20")
    n = 0
    for r in rows:
        try:
            await ctx.pool.execute("UPDATE waitlist SET status='expired' WHERE id=$1::uuid AND status='offered'", r["id"])
            salon = {"id": r["salon_id"], "name": r["salon_name"], "owner_wa_id": r["owner_wa_id"],
                     "wa_phone_number_id": r["wa_phone_number_id"], "wa_access_token": r["wa_access_token"],
                     "tg_bot_id": r["tg_bot_id"], "deposit_kes": r["deposit_kes"]}
            await _offer_next(ctx, salon, r["staff_id"], r["staff_name"], r["service_id"],
                              r["service"], r["starts_at"], r["ends_at"])
            n += 1
        except Exception:
            log.exception("waitlist sweep failed for %s", r["id"])
    return n
