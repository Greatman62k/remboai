"""The booking engine: slots, holds, deposits, cancellations, lateness and visit checks.

The chat router in conversation.py decides what a message means. Everything after
that decision lives here, so this file owns booking state and money movement and the
router file owns language, routing and dispatch."""
from __future__ import annotations

import difflib
import json
import logging
import re
from datetime import date, datetime, time, timedelta
from typing import TYPE_CHECKING
from zoneinfo import ZoneInfo

import asyncpg

from . import guard, mpesa, payments, waitlist
from .availability import Slot, choose, day_slots
from .bookings import busy_times, hold_slot, release
from .channels import outbound
from .contact import display_contact
from .messages import t
from .reminders import cancel_reminders, schedule_reminders

if TYPE_CHECKING:
    from .ai.llm import Extraction
    from .conversation import Chat

log = logging.getLogger("rembo.booking")
HORIZON_DAYS = 14


async def _save(ctx, salon_id: str, wa_id: str, state: dict) -> None:
    hist = state.get("history", [])
    if len(hist) > guard.MAX_HISTORY * 2:
        state["history"] = hist[-guard.MAX_HISTORY * 2 :]
    await ctx.pool.execute(
        "INSERT INTO conversations (salon_id, wa_id, state) VALUES ($1::uuid,$2,$3::jsonb) "
        "ON CONFLICT (salon_id, wa_id) DO UPDATE SET state=EXCLUDED.state, updated_at=now()", salon_id, wa_id, json.dumps(state))


async def peek_state(ctx, salon_id: str, wa_id: str) -> dict:
    """The saved conversation state, without creating a client row.

    Strangers the shared bot has only shown the business list to must not
    materialise as customers; the picker repeat guard reads this instead of _load."""
    raw = await ctx.pool.fetchval("SELECT state FROM conversations WHERE salon_id=$1::uuid AND wa_id=$2", salon_id, wa_id)
    state = raw if isinstance(raw, dict) else json.loads(raw or "{}")
    return state if isinstance(state, dict) else {}


async def stranger_state(ctx, sender: str) -> dict:
    """What the shared bot remembers about a sender it met before any business existed."""
    raw = await ctx.pool.fetchval("SELECT state FROM stranger_state WHERE sender=$1", sender)
    state = raw if isinstance(raw, dict) else json.loads(raw or "{}")
    return state if isinstance(state, dict) else {}


STRANGER_HISTORY = 40


async def save_stranger(ctx, sender: str, state: dict) -> None:
    hist = state.get("history", [])
    if len(hist) > STRANGER_HISTORY:
        state["history"] = hist[-STRANGER_HISTORY:]
    await ctx.pool.execute(
        "INSERT INTO stranger_state (sender, state) VALUES ($1,$2::jsonb) "
        "ON CONFLICT (sender) DO UPDATE SET state=EXCLUDED.state, updated_at=now()", sender, json.dumps(state))


async def _catalog(ctx, salon_id: str) -> list[dict]:
    rows = await ctx.pool.fetch(
        "SELECT s.id::text, s.name, s.aliases, s.duration_min, s.price_kes, "
        "COALESCE(array_agg(ss.staff_id::text) FILTER (WHERE st.active), '{}') AS staff_ids "
        "FROM services s LEFT JOIN staff_services ss ON ss.service_id = s.id LEFT JOIN staff st ON st.id = ss.staff_id "
        "WHERE s.salon_id=$1::uuid AND s.active GROUP BY s.id ORDER BY s.name", salon_id)
    return [dict(r) for r in rows]


async def _reply(ctx, salon_id: str, wa_id: str, st: dict, chat: Chat, text: str) -> None:
    st.setdefault("history", []).append({"role": "assistant", "text": text, "ts": datetime.now(ZoneInfo("UTC")).isoformat()})
    if len(st["history"]) > guard.MAX_HISTORY * 2:
        st["history"] = st["history"][-guard.MAX_HISTORY * 2 :]
    await _save(ctx, salon_id, wa_id, st)
    await chat.text(text)


async def _reply_buttons(ctx, salon_id: str, wa_id: str, st: dict, chat: Chat, body: str, buttons) -> None:
    st.setdefault("history", []).append({"role": "assistant", "text": body, "ts": datetime.now(ZoneInfo("UTC")).isoformat()})
    if len(st["history"]) > guard.MAX_HISTORY * 2:
        st["history"] = st["history"][-guard.MAX_HISTORY * 2 :]
    await _save(ctx, salon_id, wa_id, st)
    await chat.buttons(body, buttons)


def when_label(dt: datetime, tz: ZoneInfo) -> str:
    d = dt.astimezone(tz)
    return d.strftime("%a %d %b, %H:%M")


def match_service(name: str | None, services: list[dict]) -> dict | None:
    if not name:
        return None
    keys: dict[str, dict] = {}
    for s in services:
        keys[s["name"].lower()] = s
        for a in s["aliases"]:
            keys.setdefault(a.lower(), s)
    q = name.lower().strip()
    if q in keys:
        return keys[q]
    close = difflib.get_close_matches(q, list(keys), n=1, cutoff=0.75)
    return keys[close[0]] if close else None


def menu(services: list[dict]) -> str:
    if not services:
        return "No services are currently listed. Please contact us directly and we will be happy to assist you."
    from .service_categories import category_label
    grouped: dict[str, list[dict]] = {}
    for s in services:
        cat = s.get("category") or "other"
        grouped.setdefault(cat, []).append(s)
    order = ["barbering", "braids", "hair", "nails", "makeup", "other"]
    lines = []
    for cat in order:
        if cat not in grouped:
            continue
        lines.append(f"{category_label(cat)}:")
        for s in sorted(grouped[cat], key=lambda x: x["name"]):
            lines.append(f"- {s['name']} (KES {s['price_kes']}, {s['duration_min']} min)")
    for cat, items in grouped.items():
        if cat not in order:
            lines.append(f"{category_label(cat)}:")
            for s in sorted(items, key=lambda x: x["name"]):
                lines.append(f"- {s['name']} (KES {s['price_kes']}, {s['duration_min']} min)")
    return "\n".join(lines)


def example_for(services: list[dict], lang: str) -> str:
    if not services:
        return "an appointment Saturday afternoon" if lang == "en" else "miadi Jumamosi mchana"
    svc = sorted(services, key=lambda s: s["name"])[0]["name"]
    if lang == "sw":
        return f"{svc} Jumamosi mchana"
    return f"{svc} Saturday afternoon"


async def _cancel(ctx, salon, client, st, services, tz, now, chat, lang, booking_id: str | None = None) -> None:
    mid = outbound.current_message_id()
    row = await ctx.pool.fetchrow("SELECT id::text, starts_at FROM bookings WHERE client_id=$1::uuid AND cancel_message_id=$2", client["id"], mid) if mid else None
    if row is None:
        row = await ctx.pool.fetchrow(
            "UPDATE bookings SET status='cancelled', cancel_message_id=$2 WHERE id = (SELECT id FROM bookings WHERE client_id=$1::uuid AND status IN ('held','confirmed') "
            "AND starts_at > now() AND ($3::uuid IS NULL OR id = $3::uuid) ORDER BY starts_at LIMIT 1) RETURNING id::text, starts_at", client["id"], mid, booking_id)
    if row is None:
        await _reply(ctx, salon["id"], chat.to, st, chat, t(lang, "nothing_to_cancel"))
        return
    cleared = {"history": st.get("history", [])}
    await _save(ctx, salon["id"], chat.to, cleared)
    await payments.park_for_cancellation(ctx.pool, row["id"])
    await payments.fail_open_manual_claims(ctx.pool, row["id"])
    await cancel_reminders(ctx.pool, row["id"])
    await waitlist.offer_for_booking(ctx, salon, row["id"])
    payment = await ctx.pool.fetchrow(
        "SELECT amount_kes, phone FROM payments WHERE booking_id=$1::uuid AND status IN ('paid','needs_refund') "
        "ORDER BY settled_at DESC NULLS LAST LIMIT 1", row["id"])
    paid = payment["amount_kes"] if payment else None
    key = "cancelled_paid" if paid else "cancelled"
    await _reply(ctx, salon["id"], chat.to, cleared, chat, t(lang, key, when=when_label(row["starts_at"], tz), salon=salon["name"]))
    if paid and salon["owner_wa_id"]:
        await outbound.send_text(ctx, salon, salon["owner_wa_id"],
                                 t("en", "owner_refund", amount=paid,
                                   phone=display_contact(payment["phone"]), ref="", when=when_label(row["starts_at"], tz), why="the customer cancelled"))


async def _late_tap(ctx, salon, client, st, services, tz, now, chat, lang, reply_id: str) -> None:
    kind, _, bid = reply_id.partition(":")
    action = kind[5:] if kind.startswith("late-") else ""
    row = await ctx.pool.fetchrow(
        "SELECT b.id::text, b.status, b.starts_at, sv.id::text AS service_id, sv.name AS service "
        "FROM bookings b JOIN services sv ON sv.id=b.service_id "
        "WHERE b.id=$1::uuid AND b.salon_id=$2::uuid AND b.client_id=$3::uuid", bid, salon["id"], client["id"]) if bid else None
    if row is None or row["status"] not in ("held", "confirmed"):
        return
    row = dict(row)
    if action == "keep":
        await _reply(ctx, salon["id"], chat.to, st, chat, t(lang, "late_kept"))
        return
    if action == "cancel":
        mid = outbound.current_message_id()
        await ctx.pool.execute("UPDATE bookings SET status='cancelled', cancel_message_id=$2 WHERE id=$1::uuid AND status IN ('held','confirmed')",
                               row["id"], mid)
        await payments.park_for_cancellation(ctx.pool, row["id"])
        await payments.fail_open_manual_claims(ctx.pool, row["id"])
        await cancel_reminders(ctx.pool, row["id"])
        await waitlist.offer_for_booking(ctx, salon, row["id"])
        payment = await ctx.pool.fetchrow(
            "SELECT amount_kes, phone FROM payments WHERE booking_id=$1::uuid AND status IN ('paid','needs_refund') "
            "ORDER BY settled_at DESC NULLS LAST LIMIT 1", row["id"])
        paid = payment["amount_kes"] if payment else None
        key = "cancelled_paid" if paid else "cancelled"
        cleared_late = {"history": st.get("history", [])}
        await _save(ctx, salon["id"], chat.to, cleared_late)
        await _reply(ctx, salon["id"], chat.to, cleared_late, chat, t(lang, key, when=when_label(row["starts_at"], tz), salon=salon["name"]))
        if paid and salon["owner_wa_id"]:
            await outbound.send_text(ctx, salon, salon["owner_wa_id"],
                                     t("en", "owner_refund", amount=paid, phone=display_contact(payment["phone"]), ref="",
                                       when=when_label(row["starts_at"], tz), why="the customer cancelled"))
        return
    if action == "move":
        service = next((s for s in services if s["id"] == row["service_id"]), None)
        if service is None:
            await _reply(ctx, salon["id"], chat.to, st, chat, t(lang, "fallback", example=example_for(services, lang)))
            return
        move_state = {"state": "idle", "history": st.get("history", []), "late_move_booking": row["id"]}
        await _offer(ctx, salon, move_state, service, row["starts_at"].astimezone(tz).date(), None, None, tz, now, chat, lang, note_key="late_move_ask")
        return


async def _visit_tap(ctx, salon, client, st, tz, chat, lang, reply_id: str) -> None:
    kind, _, bid = reply_id.partition(":")
    answer = kind[6:] if kind.startswith("visit-") else ""
    row = await ctx.pool.fetchrow(
        "SELECT v.attended, b.status, b.starts_at, sv.name AS service, s.name AS salon_name, s.owner_wa_id, c.wa_id AS phone, "
        "c.name AS client_name, c.lang FROM visit_checks v JOIN bookings b ON b.id=v.booking_id "
        "JOIN services sv ON sv.id=b.service_id JOIN salons s ON s.id=b.salon_id JOIN clients c ON c.id=b.client_id "
        "WHERE v.booking_id=$1::uuid AND b.salon_id=$2::uuid AND b.client_id=$3::uuid",
        bid, salon["id"], client["id"]) if bid and answer in ("yes", "no") else None
    if row is None:
        return
    row = dict(row)
    clang = row["lang"] or lang
    if answer == "yes":
        await ctx.pool.execute("UPDATE visit_checks SET attended=true, answered_at=now() WHERE booking_id=$1::uuid", bid)
        state = {"state": "awaiting_feedback", "visit_booking": bid, "history": st.get("history", [])}
        await _save(ctx, salon["id"], chat.to, state)
        await _reply(ctx, salon["id"], chat.to, state, chat, t(clang, "visit_thanks", salon=row["salon_name"]))
        return
    await ctx.pool.execute("UPDATE visit_checks SET attended=false, answered_at=now() WHERE booking_id=$1::uuid", bid)
    cleared_visit = {"history": st.get("history", [])}
    await _save(ctx, salon["id"], chat.to, cleared_visit)
    when = row["starts_at"].astimezone(tz).strftime("%a %d %b, %H:%M") if row["starts_at"] else ""
    if row["owner_wa_id"]:
        await outbound.send_text(ctx, salon, row["owner_wa_id"],
                                 t("en", "owner_visit_missed", client=row["client_name"] or display_contact(row["phone"]), phone=display_contact(row["phone"]),
                                   service=row["service"], when=when))
    await _reply(ctx, salon["id"], chat.to, cleared_visit, chat, t(clang, "visit_owner_told"))


async def find_slots(ctx, salon, service: dict, day: date, window: str | None, at: time | None, tz, now) -> tuple[list[Slot], bool]:
    """Up to three slots for the request. exact=False means nothing matched and these are the closest alternatives."""
    staff = [{"id": sid, "name": n} for sid, n in
             [(r["id"], r["name"]) for r in await ctx.pool.fetch(
                 "SELECT id::text, name FROM staff WHERE id = ANY($1::uuid[]) AND active ORDER BY name", service["staff_ids"])]]
    if not staff:
        return [], True
    start = datetime.combine(day, time(0), tz)
    end = start + timedelta(days=HORIZON_DAYS + 1)
    async with ctx.pool.acquire() as con:
        busy = await busy_times(con, [s["id"] for s in staff], start, end)
    dur, earliest = timedelta(minutes=service["duration_min"]), now + timedelta(minutes=30)
    first_fallback: list[Slot] = []
    for offset in range(HORIZON_DAYS):
        slots = day_slots(day + timedelta(days=offset), salon["open_hours"], tz, dur, staff, busy, earliest)
        if not slots:
            continue
        picked, exact = choose(slots, window=window, at=at)
        if picked and exact:
            return picked, offset == 0
        if offset == 0:
            first_fallback = picked
    if first_fallback:
        return first_fallback, False
    return [], True


async def _offer(ctx, salon, st, service, day, window, at, tz, now, chat, lang, note_key: str | None = None) -> None:
    slots, exact = await find_slots(ctx, salon, service, day, window, at, tz, now)
    if not slots:
        state = {"state": "no_slots", "service_id": service["id"], "date": day.isoformat(), "history": st.get("history", [])}
        await _save(ctx, salon["id"], chat.to, state)
        await _reply(ctx, salon["id"], chat.to, state, chat,
                     t(lang, "no_slots", service=service["name"]) + "\n" + t(lang, "waitlist_hint"))
        return
    lines = "\n".join(t(lang, "slot_line", n=i + 1, when=when_label(s.starts_at, tz), stylist=s.staff_name) for i, s in enumerate(slots))
    state = {"state": "offered", "service_id": service["id"], "date": day.isoformat(), "window": window, "time": at.strftime("%H:%M") if at else None,
             "slots": [{"staff_id": s.staff_id, "staff_name": s.staff_name, "starts_at": s.starts_at.isoformat(), "ends_at": s.ends_at.isoformat()} for s in slots],
             "history": st.get("history", [])}
    if st.get("late_move_booking"):
        state["late_move_booking"] = st["late_move_booking"]
    await _save(ctx, salon["id"], chat.to, state)
    intro = t(lang, note_key) + "\n" if note_key else ""
    body = intro + t(lang, "offer", service=service["name"], note="" if exact else t(lang, "nearest"), lines=lines)
    await _reply_buttons(ctx, salon["id"], chat.to, state, chat, body, [(f"slot:{i}", when_label(s.starts_at, tz).replace(",", "")[:20]) for i, s in enumerate(slots)])


async def _book(ctx, salon, client, st, services, tz, now, chat, lang, ex: Extraction, tap_id: str | None = None) -> None:
    if ex.intent == "choose_slot":
        await _choose(ctx, salon, client, st, services, tz, now, chat, lang, ex.choice or 0, tap_id=tap_id)
        return
    if st.get("state") == "held" and st.get("booking_id"):
        await release(ctx.pool, st["booking_id"])
    mem = st if st.get("state") in ("asking", "offered", "held") else {}
    if ex.service:
        service = match_service(ex.service, services)
    else:
        service = match_service(mem.get("service_name"), services) or next((s for s in services if s["id"] == mem.get("service_id")), None)
    if ex.service and service is None:
        await _reply(ctx, salon["id"], chat.to, st, chat, t(lang, "unknown_service", menu=menu(services)))
        return
    if getattr(ex, "far_date", False):
        pending_far = {"state": "asking", "service_name": service["name"] if service else None, "history": st.get("history", [])}
        await _save(ctx, salon["id"], chat.to, pending_far)
        await _reply(ctx, salon["id"], chat.to, pending_far, chat, t(lang, "far_date"))
        return
    day = date.fromisoformat(ex.date) if ex.date else (date.fromisoformat(mem["date"]) if mem.get("date") else None)
    window = ex.window or mem.get("window")
    at = time.fromisoformat(ex.time) if ex.time else (time.fromisoformat(mem["time"]) if mem.get("time") else None)
    pending = {"state": "asking", "service_name": service["name"] if service else None, "date": day.isoformat() if day else None,
               "window": window, "time": at.strftime("%H:%M") if at else None, "history": st.get("history", [])}
    if service is None:
        await _save(ctx, salon["id"], chat.to, pending)
        await _reply(ctx, salon["id"], chat.to, pending, chat, t(lang, "ask_service", menu=menu(services)))
        return
    if day is None:
        await _save(ctx, salon["id"], chat.to, pending)
        await _reply(ctx, salon["id"], chat.to, pending, chat, t(lang, "ask_date"))
        return
    await _offer(ctx, salon, st, service, day, window, at, tz, now, chat, lang)


async def _choose(ctx, salon, client, st, services, tz, now, chat, lang, choice: int, tap_id: str | None = None) -> None:
    slots = st.get("slots") or []
    if st.get("state") != "offered" or not 1 <= choice <= len(slots):
        await _reply(ctx, salon["id"], chat.to, st, chat, t(lang, "fallback", example=example_for(services, lang)))
        return
    service = next((s for s in services if s["id"] == st["service_id"]), None)
    pick = slots[choice - 1]
    if service is None:
        await _reply(ctx, salon["id"], chat.to, st, chat, t(lang, "fallback", example=example_for(services, lang)))
        return
    if st.get("late_move_booking"):
        await _move_choice(ctx, salon, client, st, pick, tz, chat, lang)
        return
    old = st.get("booking_id")
    if old:
        await release(ctx.pool, old)
    mid = outbound.current_message_id()
    active = await ctx.pool.fetchval(
        "SELECT count(*) FROM bookings WHERE client_id=$1::uuid AND status IN ('held','confirmed') AND starts_at > now() "
        "AND ($2::text IS NULL OR source_message_id IS DISTINCT FROM $2)", client["id"], mid)
    if ctx.settings.max_active_bookings and active >= ctx.settings.max_active_bookings:
        state = {"history": st.get("history", [])}
        await _save(ctx, salon["id"], chat.to, state)
        await _reply(ctx, salon["id"], chat.to, state, chat, t(lang, "too_many_bookings", n=active, salon=salon["name"]))
        return
    booking_id = await hold_slot(
        ctx.pool, salon_id=salon["id"], staff_id=pick["staff_id"], service_id=service["id"], client_id=client["id"],
        starts_at=datetime.fromisoformat(pick["starts_at"]), ends_at=datetime.fromisoformat(pick["ends_at"]), minutes=ctx.settings.hold_minutes, message_id=mid)
    if booking_id is None:
        await _offer(ctx, salon, st, service, date.fromisoformat(st["date"]), st.get("window"),
                     time.fromisoformat(st["time"]) if st.get("time") else None, tz, now, chat, lang, note_key="slot_gone")
        return
    when = when_label(datetime.fromisoformat(pick["starts_at"]), tz)
    acct = await payments.account(ctx.pool, salon["id"])
    amount = payments.deposit_amount(salon, service) if acct else 0
    if amount == 0:
        await ctx.pool.execute("UPDATE bookings SET status='confirmed', hold_expires_at=NULL WHERE id=$1::uuid AND status='held'", booking_id)
        try:
            await schedule_reminders(ctx.pool, booking_id)
        except Exception:
            log.exception("could not schedule reminders for free booking %s", booking_id)
        state = {"history": st.get("history", [])}
        await _save(ctx, salon["id"], chat.to, state)
        await _reply(ctx, salon["id"], chat.to, state, chat, t(lang, "booked_free", service=service["name"], when=when, stylist=pick["staff_name"], salon=salon["name"]))
        if salon["owner_wa_id"]:
            await outbound.send_text(ctx, salon, salon["owner_wa_id"],
                                     t("en", "owner_booked_free", client=client["name"] or mpesa.normalize_msisdn(chat.to) or "customer",
                                       service=service["name"], when=when, stylist=pick["staff_name"]))
        return
    held = t(lang, "held", service=service["name"], when=when, stylist=pick["staff_name"], mins=ctx.settings.hold_minutes)
    state = {"state": "held", "booking_id": booking_id, "amount": amount, "service_id": service["id"], "service_name": service["name"], "held_at": datetime.now(tz).isoformat(), "held_when": when, "held_stylist": pick["staff_name"], "history": st.get("history", []), "last_tap": {"id": tap_id or f"slot:{choice-1}", "at": datetime.now(tz).isoformat()}}
    phone = mpesa.normalize_msisdn(chat.to)
    if phone is None:
        await _save(ctx, salon["id"], chat.to, state)
        await _reply(ctx, salon["id"], chat.to, state, chat, held + "\n" + t(lang, "ask_number", amount=amount))
        return
    state_with_phone = {**state, "phone": phone}
    await _save(ctx, salon["id"], chat.to, state_with_phone)
    await _reply(ctx, salon["id"], chat.to, state_with_phone, chat, held + "\n" + await _prompt(ctx, salon, booking_id, phone, amount, lang))


async def _move_choice(ctx, salon, client, st, pick, tz, chat, lang) -> None:
    """Move a confirmed booking to a picked slot after a lateness notice. A retry that finds the move done just confirms again."""
    bid = st["late_move_booking"]
    target = await ctx.pool.fetchrow("SELECT status, starts_at FROM bookings WHERE id=$1::uuid AND salon_id=$2::uuid AND client_id=$3::uuid",
                                     bid, salon["id"], client["id"])
    if target is None or target["status"] not in ("held", "confirmed"):
        await _reply(ctx, salon["id"], chat.to, st, chat, t(lang, "hold_gone"))
        return
    new_start = datetime.fromisoformat(pick["starts_at"])
    new_end = datetime.fromisoformat(pick["ends_at"])
    if target["starts_at"] == new_start:
        service = next((s for s in await _catalog(ctx, salon["id"]) if s["id"] == st["service_id"]), None)
        await _reply(ctx, salon["id"], chat.to, {"history": st.get("history", [])}, chat,
                     t(lang, "late_moved", service=service["name"] if service else "", when=when_label(new_start, tz), stylist=pick["staff_name"]))
        return
    try:
        n = await ctx.pool.execute("UPDATE bookings SET staff_id=$2::uuid, starts_at=$3, ends_at=$4 WHERE id=$1::uuid AND status IN ('held','confirmed')",
                                   bid, pick["staff_id"], new_start, new_end)
    except asyncpg.ExclusionViolationError:
        await _reply(ctx, salon["id"], chat.to, st, chat, t(lang, "late_move_taken"))
        return
    if n == "UPDATE 0":
        await _reply(ctx, salon["id"], chat.to, st, chat, t(lang, "hold_gone"))
        return
    await cancel_reminders(ctx.pool, bid)
    try:
        await schedule_reminders(ctx.pool, bid)
    except Exception:
        log.exception("could not reschedule reminders for moved booking %s", bid)
    service = next((s for s in await _catalog(ctx, salon["id"]) if s["id"] == st["service_id"]), None)
    cleared_move = {"history": st.get("history", [])}
    await _save(ctx, salon["id"], chat.to, cleared_move)
    await _reply(ctx, salon["id"], chat.to, cleared_move, chat,
                 t(lang, "late_moved", service=service["name"] if service else "", when=when_label(new_start, tz), stylist=pick["staff_name"]))
    if salon["owner_wa_id"]:
        await outbound.send_text(ctx, salon, salon["owner_wa_id"],
                                 t("en", "owner_moved", client=client["name"] or chat.to, service=service["name"] if service else "",
                                   when=when_label(new_start, tz), stylist=pick["staff_name"]))


async def _prompt(ctx, salon, booking_id: str, phone: str, amount: int, lang: str) -> str:
    """Send the M-Pesa prompt and say what happened. A manual till gets no prompt:
    the customer pays at the till and claims here."""
    acct = await payments.account(ctx.pool, salon["id"])
    if acct is not None and acct.get("manual"):
        return t(lang, "till_how", amount=amount, till=acct["party_b"], salon=salon["name"])
    result = await payments.request_deposit(ctx, salon, booking_id, phone, amount, message_id=outbound.current_message_id())
    if result == "failed":
        return t(lang, "stk_failed")
    return t(lang, "stk_wait" if result == "wait" else "stk_sent", amount=amount, phone=phone)


async def _till_claim(ctx, salon, client, st, chat, lang, text: str) -> None:
    """A manual-till customer wrote back while held. A clean amount-plus-code claim is filed and the
    owner is asked to confirm with one tap; anything unclear is asked again or passed to review.
    Pasted SMS text is reference only: a claim never confirms a booking by itself."""
    bid = st.get("booking_id")
    row = await ctx.pool.fetchrow(
        "SELECT b.status, sv.name AS service FROM bookings b JOIN services sv ON sv.id=b.service_id WHERE b.id=$1::uuid", bid)
    if row is None or row["status"] != "held":
        await _save(ctx, salon["id"], chat.to, {})
        await _reply(ctx, salon["id"], chat.to, st, chat, t(lang, "hold_gone"))
        return
    amount = st.get("amount") or 0
    pending = await ctx.pool.fetchrow(
        "SELECT id::text, amount_kes FROM payments WHERE booking_id=$1::uuid AND status='pending' AND method='manual' ORDER BY created_at DESC LIMIT 1", bid)
    if pending is not None:
        await _reply(ctx, salon["id"], chat.to, st, chat, t(lang, "till_still_waiting", salon=salon["name"], amount=pending["amount_kes"]))
        return
    claimed, code, ambiguous = payments.parse_claim(text)
    if ambiguous or (claimed is not None and amount and claimed != amount):
        await _file_for_review(ctx, salon, st, chat, lang, bid, amount, claimed, code, text)
        return
    if claimed is None or code is None:
        await _reply(ctx, salon["id"], chat.to, st, chat, t(lang, "till_again", amount=amount))
        return
    if await ctx.pool.fetchval("SELECT 1 FROM payments WHERE mpesa_receipt=$1", code):
        await _reply(ctx, salon["id"], chat.to, st, chat, t(lang, "till_code_used"))
        return
    pay_id = await payments.record_manual_claim(ctx.pool, bid, salon["id"], mpesa.normalize_msisdn(chat.to) or chat.to,
                                                claimed, code, text)
    await ctx.pool.execute(f"UPDATE bookings SET hold_expires_at = GREATEST(hold_expires_at, now() + interval '{payments.MANUAL_HOLD}') "
                           "WHERE id=$1::uuid AND status='held'", bid)
    await _reply(ctx, salon["id"], chat.to, st, chat, t(lang, "till_waiting", salon=salon["name"], amount=claimed))
    if salon.get("owner_wa_id"):
        await _ask_owner(ctx, salon, st, chat, lang, bid, pay_id, claimed, code, text)


async def _file_for_review(ctx, salon, st, chat, lang, bid: str, expected: int, claimed: int | None, code: str | None, text: str) -> None:
    pay_id = await ctx.pool.fetchval(
        "INSERT INTO payments (booking_id, salon_id, phone, amount_kes, method, status, mpesa_receipt, claim_ref) "
        "VALUES ($1::uuid,$2::uuid,$3,$4,'manual','review',$5,$6) RETURNING id::text",
        bid, salon["id"], mpesa.normalize_msisdn(chat.to) or chat.to, expected or claimed or 1, code, text[:500])
    reason = "amount_mismatch" if claimed and expected and claimed != expected else "payment_unresolved"
    await ctx.pool.execute(
        "INSERT INTO payment_reviews (payment_id, reason, status, expected_amount, received_amount, mpesa_receipt) "
        "VALUES ($1,$2,'pending_review',$3,$4,$5) ON CONFLICT (payment_id) DO NOTHING",
        pay_id, reason, expected or 0, claimed, code)
    await _reply(ctx, salon["id"], chat.to, st, chat, t(lang, "till_review", salon=salon["name"]))
    if salon.get("owner_wa_id"):
        await outbound.send_text(ctx, salon, salon["owner_wa_id"],
                                 t("en", "till_owner_ask", service=st.get("service_name") or "", when=st.get("held_when") or "",
                                   client=chat.to, amount=claimed or expected, code=code or "?", ref=text[:200]))


async def _ask_owner(ctx, salon, st, chat, lang, bid: str, pay_id: str, claimed: int, code: str, text: str) -> None:
    await outbound.send_buttons(
        ctx, salon, salon["owner_wa_id"],
        t("en", "till_owner_ask", service=st.get("service_name") or "", when=st.get("held_when") or "",
          client=chat.to, amount=claimed, code=code, ref=text[:200]),
        [(f"till:yes:{pay_id}", "Yes"), (f"till:no:{pay_id}", "No")])


async def _pay(ctx, salon, st, services, tz, chat, lang, phone: str | None, msg_text: str | None = None) -> None:
    """The customer sent a number, or said "pay": (re)send the deposit prompt for the booking being held."""
    row = await ctx.pool.fetchrow(
        "SELECT b.status, sv.price_kes FROM bookings b JOIN services sv ON sv.id=b.service_id WHERE b.id=$1::uuid", st["booking_id"])
    if row is None or row["status"] != "held":
        await _save(ctx, salon["id"], chat.to, {})
        await _reply(ctx, salon["id"], chat.to, st, chat, t(lang, "hold_gone"))
        return
    amount = st.get("amount") or min(int(salon["deposit_kes"]), row["price_kes"])
    if phone is None:
        await _reply(ctx, salon["id"], chat.to, st, chat, t(lang, "ask_number", amount=amount))
        return
    if re.search(r"paid|done|sent|tuma|nimelipa|nimesha", msg_text or "", re.I):
        pending = await ctx.pool.fetchval("SELECT 1 FROM payments WHERE booking_id=$1::uuid AND status='pending' AND checkout_request_id IS NOT NULL AND created_at > now() - interval '3 minutes'", st["booking_id"])
        if pending:
            new_state_pending = {**st, "phone": phone} if phone else st
            await _reply(ctx, salon["id"], chat.to, new_state_pending, chat, t(lang, "stk_wait", amount=amount, phone=phone))
            return
    new_state = {**st, "phone": phone}
    await _reply(ctx, salon["id"], chat.to, new_state, chat, await _prompt(ctx, salon, st["booking_id"], phone, amount, lang))
