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

from . import guard, mpesa, payments, smalltalk, waitlist
from .availability import Slot, choose, day_slots
from .bookings import busy_times, hold_slot, release
from .channels import outbound
from .contact import display_contact
from .messages import ensure_help_hint, t
from .reminders import cancel_reminders, schedule_owner_cancellation, schedule_reminders

if TYPE_CHECKING:
    from .ai.llm import Extraction
    from .ai.vision import PaymentScreenshotGuess
    from .conversation import Chat

log = logging.getLogger("rembo.booking")
HORIZON_DAYS = 14
MANUAL_OWNER_CHECK_MINUTES = 5


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
    history = st.setdefault("history", [])
    first_reply = not any(item.get("role") == "assistant" for item in history if isinstance(item, dict))
    if first_reply:
        last_user = next((item.get("text", "") for item in reversed(history)
                          if isinstance(item, dict) and item.get("role") != "assistant"), "")
        text = ensure_help_hint(text, smalltalk.language(last_user or "", "en"))
    history.append({"role": "assistant", "text": text, "ts": datetime.now(ZoneInfo("UTC")).isoformat()})
    if len(st["history"]) > guard.MAX_HISTORY * 2:
        st["history"] = st["history"][-guard.MAX_HISTORY * 2 :]
    await _save(ctx, salon_id, wa_id, st)
    await chat.text(text)


async def _reply_buttons(ctx, salon_id: str, wa_id: str, st: dict, chat: Chat, body: str, buttons) -> None:
    history = st.setdefault("history", [])
    first_reply = not any(item.get("role") == "assistant" for item in history if isinstance(item, dict))
    if first_reply:
        last_user = next((item.get("text", "") for item in reversed(history)
                          if isinstance(item, dict) and item.get("role") != "assistant"), "")
        body = ensure_help_hint(body, smalltalk.language(last_user or "", "en"))
    history.append({"role": "assistant", "text": body, "ts": datetime.now(ZoneInfo("UTC")).isoformat()})
    if len(st["history"]) > guard.MAX_HISTORY * 2:
        st["history"] = st["history"][-guard.MAX_HISTORY * 2 :]
    await _save(ctx, salon_id, wa_id, st)
    await chat.buttons(body, buttons)


def _manual_payment_buttons(booking_id: str, lang: str, *, include_paid: bool = True) -> list[tuple[str, str]]:
    buttons = []
    if include_paid:
        buttons.append((f"manualpay:paid:{booking_id}", t(lang, "manual_paid_button")))
    buttons.append((f"manualpay:cancel:{booking_id}", t(lang, "manual_cancel_button")))
    return buttons


def _manual_payment_instructions(text: str, lang: str) -> str:
    return text + t(lang, "manual_payment_actions")


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
    await cancel_reminders(ctx.pool, row["id"])
    await schedule_owner_cancellation(ctx.pool, row["id"])
    await waitlist.offer_for_booking(ctx, salon, row["id"])
    payment = await ctx.pool.fetchrow(
        "SELECT amount_kes, phone FROM payments WHERE booking_id=$1::uuid AND status IN ('paid','needs_refund') "
        "ORDER BY settled_at DESC NULLS LAST LIMIT 1", row["id"])
    pending_claim = await ctx.pool.fetchval(
        "SELECT 1 FROM payments WHERE booking_id=$1::uuid AND method='manual' AND status IN ('pending','review') LIMIT 1",
        row["id"])
    paid = payment["amount_kes"] if payment else None
    key = "cancelled_paid" if paid else "cancelled_pending_claim" if pending_claim else "cancelled"
    await _reply(ctx, salon["id"], chat.to, cleared, chat, t(lang, key, when=when_label(row["starts_at"], tz), salon=salon["name"]))
    if paid and salon["owner_wa_id"]:
        await outbound.send_text(ctx, salon, salon["owner_wa_id"],
                                 t("en", "owner_refund", amount=paid,
                                   phone=display_contact(payment["phone"]), ref="", when=when_label(row["starts_at"], tz), why="the customer cancelled"))


async def handle_manual_payment_tap(ctx, salon, client, st, services, tz, now, chat, lang, reply_id: str) -> bool:
    """Handle the customer's payment-evidence and cancel buttons for their current manual hold."""
    match = re.fullmatch(r"manualpay:(paid|cancel):([0-9a-fA-F-]{36})", reply_id or "")
    if not match:
        return False
    action, booking_id = match[1], match[2]
    if st.get("state") != "held" or st.get("booking_id") != booking_id:
        await chat.text(t(lang, "manual_action_closed"))
        return True
    row = await ctx.pool.fetchrow(
        "SELECT b.status, b.client_id::text AS client_id, b.owner_check_status, sp.manual "
        "FROM bookings b JOIN salon_payment sp ON sp.salon_id=b.salon_id "
        "WHERE b.id=$1::uuid AND b.salon_id=$2::uuid", booking_id, salon["id"])
    if (row is None or row["client_id"] != client["id"] or not row["manual"]
            or row["status"] != "held" or row["owner_check_status"] == "pending"):
        await chat.text(t(lang, "manual_action_closed"))
        return True
    if action == "cancel":
        await _cancel(ctx, salon, client, st, services, tz, now, chat, lang, booking_id=booking_id)
        return True
    pending = await ctx.pool.fetchval(
        "SELECT 1 FROM payments WHERE booking_id=$1::uuid AND method='manual' AND status IN ('pending','review') LIMIT 1",
        booking_id)
    if pending:
        await _reply_buttons(ctx, salon["id"], chat.to, st, chat,
                             t(lang, "till_still_waiting", salon=salon["name"], amount=st.get("amount") or 0),
                             _manual_payment_buttons(booking_id, lang, include_paid=False))
        return True
    await _reply_buttons(ctx, salon["id"], chat.to, st, chat,
                         t(lang, "manual_payment_evidence", salon=salon["name"]),
                         _manual_payment_buttons(booking_id, lang, include_paid=False))
    return True


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
        await cancel_reminders(ctx.pool, row["id"])
        await schedule_owner_cancellation(ctx.pool, row["id"])
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
            await _reply(ctx, salon["id"], chat.to, st, chat, t(lang, "fallback", salon=salon["name"], example=example_for(services, lang)))
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
        await _reply(ctx, salon["id"], chat.to, st, chat, t(lang, "fallback", salon=salon["name"], example=example_for(services, lang)))
        return
    service = next((s for s in services if s["id"] == st["service_id"]), None)
    pick = slots[choice - 1]
    if service is None:
        await _reply(ctx, salon["id"], chat.to, st, chat, t(lang, "fallback", salon=salon["name"], example=example_for(services, lang)))
        return
    if st.get("late_move_booking"):
        await _move_choice(ctx, salon, client, st, pick, tz, chat, lang)
        return
    acct = await payments.account(ctx.pool, salon["id"])
    if int(salon.get("deposit_kes") or 0) > 0 and acct is None:
        if st.get("booking_id"):
            await release(ctx.pool, st["booking_id"])
        state = {"history": st.get("history", [])}
        await _save(ctx, salon["id"], chat.to, state)
        await _reply(ctx, salon["id"], chat.to, state, chat, t(lang, "payment_unconfigured", salon=salon["name"]))
        return
    amount = payments.deposit_amount(salon, service) if acct else 0
    manual_owner_check = amount > 0 and bool(acct and acct.get("manual")) and not st.get("owner_preapproved")
    if manual_owner_check and not salon.get("owner_wa_id"):
        await _reply(ctx, salon["id"], chat.to, st, chat, t(lang, "manual_owner_unavailable", salon=salon["name"]))
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
        starts_at=datetime.fromisoformat(pick["starts_at"]), ends_at=datetime.fromisoformat(pick["ends_at"]),
        minutes=MANUAL_OWNER_CHECK_MINUTES if manual_owner_check else ctx.settings.hold_minutes, message_id=mid)
    if booking_id is None:
        await _offer(ctx, salon, st, service, date.fromisoformat(st["date"]), st.get("window"),
                     time.fromisoformat(st["time"]) if st.get("time") else None, tz, now, chat, lang, note_key="slot_gone")
        return
    when = when_label(datetime.fromisoformat(pick["starts_at"]), tz)
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
    if acct.get("manual"):
        if manual_owner_check:
            deadline = datetime.now(ZoneInfo("UTC")) + timedelta(minutes=MANUAL_OWNER_CHECK_MINUTES)
            await ctx.pool.execute(
                "UPDATE bookings SET owner_check_status='pending', owner_check_expires_at=$2, "
                "hold_expires_at=$2 WHERE id=$1::uuid AND status='held'", booking_id, deadline)
            waiting_state = {**state, "state": "owner_check_pending", "owner_check_expires_at": deadline.isoformat()}
            await _save(ctx, salon["id"], chat.to, waiting_state)
            await outbound.send_buttons(
                ctx, salon, salon["owner_wa_id"],
                t("en", "owner_manual_check", client=client["name"] or "customer", service=service["name"],
                  when=when, stylist=pick["staff_name"]),
                [(f"availability:yes:{booking_id}", "Available"), (f"availability:no:{booking_id}", "Unavailable")])
            await _reply(ctx, salon["id"], chat.to, waiting_state, chat,
                         t(lang, "owner_manual_waiting", service=service["name"], when=when, stylist=pick["staff_name"]))
            return
        await _save(ctx, salon["id"], chat.to, state)
        body = held + "\n" + await _prompt(ctx, salon, booking_id, "", amount, lang)
        await _reply_buttons(ctx, salon["id"], chat.to, state, chat,
                             _manual_payment_instructions(body, lang), _manual_payment_buttons(booking_id, lang))
        return
    if phone is None:
        await _save(ctx, salon["id"], chat.to, state)
        await _reply(ctx, salon["id"], chat.to, state, chat, held + "\n" + t(lang, "ask_number", amount=amount))
        return
    state_with_phone = {**state, "phone": phone}
    await _save(ctx, salon["id"], chat.to, state_with_phone)
    await _reply(ctx, salon["id"], chat.to, state_with_phone, chat, held + "\n" + await _prompt(ctx, salon, booking_id, phone, amount, lang))


async def _owner_booking_row(pool, booking_id: str, lock: bool = False):
    suffix = " FOR UPDATE OF b" if lock else ""
    return await pool.fetchrow(
        "SELECT b.id::text AS booking_id, b.salon_id::text AS salon_id, b.client_id::text AS client_id, "
        "b.staff_id::text AS staff_id, b.service_id::text AS service_id, b.status AS booking_status, "
        "b.owner_check_status, b.owner_check_expires_at, b.owner_check_timeout_notice_at, "
        "b.owner_customer_notice_at, b.owner_late_notice_at, b.customer_reply_expires_at, b.starts_at, b.ends_at, "
        "c.wa_id, c.name AS client_name, c.lang, s.name AS salon_name, s.wa_phone_number_id, s.wa_access_token, "
        "s.timezone, s.deposit_kes, sv.name AS service_name, sv.price_kes, sv.duration_min, st.name AS stylist_name "
        "FROM bookings b JOIN clients c ON c.id=b.client_id JOIN salons s ON s.id=b.salon_id "
        "JOIN services sv ON sv.id=b.service_id JOIN staff st ON st.id=b.staff_id "
        "WHERE b.id=$1::uuid" + suffix, booking_id)


async def _owner_customer_state(ctx, row) -> dict:
    raw = await ctx.pool.fetchval("SELECT state FROM conversations WHERE salon_id=$1::uuid AND wa_id=$2", row["salon_id"], row["wa_id"])
    state = raw if isinstance(raw, dict) else json.loads(raw or "{}")
    return state if isinstance(state, dict) else {}


async def _clear_owner_customer_state(ctx, row, booking_id: str) -> None:
    state = await _owner_customer_state(ctx, row)
    if state.get("booking_id") == booking_id or state.get("owner_late_booking_id") == booking_id:
        await _save(ctx, row["salon_id"], row["wa_id"], {"history": state.get("history", [])})


def _owner_salon(row) -> dict:
    return {"id": row["salon_id"], "name": row["salon_name"], "wa_phone_number_id": row["wa_phone_number_id"],
            "wa_access_token": row["wa_access_token"], "deposit_kes": row["deposit_kes"], "timezone": row["timezone"]}


async def _send_owner_timeout_notice(ctx, row) -> bool:
    if row is None or row["owner_check_timeout_notice_at"] is not None:
        return False
    ok = await outbound.send_text(
        ctx, _owner_salon(row), row["wa_id"],
        t(row["lang"] or "en", "owner_manual_timeout"))
    if ok:
        await ctx.pool.execute("UPDATE bookings SET owner_check_timeout_notice_at=now() WHERE id=$1::uuid AND owner_check_timeout_notice_at IS NULL",
                               row["booking_id"])
    return ok


async def _send_owner_late_offer(ctx, row) -> bool:
    if row is None or row["owner_late_notice_at"] is not None:
        return False
    tz = ZoneInfo(row["timezone"])
    when = when_label(row["starts_at"], tz)
    lang = row["lang"] or "en"
    body = t(lang, "owner_manual_late_offer", service=row["service_name"], when=when, stylist=row["stylist_name"])
    buttons = [(f"ownerresume:yes:{row['booking_id']}", t(lang, "owner_manual_late_yes")),
               (f"ownerresume:no:{row['booking_id']}", t(lang, "owner_manual_late_no"))]
    ok = await outbound.send_buttons(ctx, _owner_salon(row), row["wa_id"], body, buttons)
    if not ok:
        return False
    deadline = datetime.now(ZoneInfo("UTC")) + timedelta(minutes=5)
    await ctx.pool.execute("UPDATE bookings SET owner_late_notice_at=now(), customer_reply_expires_at=$2 "
                           "WHERE id=$1::uuid AND owner_check_status='late_approved' AND owner_late_notice_at IS NULL",
                           row["booking_id"], deadline)
    state = await _owner_customer_state(ctx, row)
    safe_to_resume_text = state.get("state") in (None, "", "owner_check_pending", "owner_late_offer") or state.get("booking_id") == row["booking_id"]
    if safe_to_resume_text:
        late_slot = {"staff_id": row["staff_id"], "staff_name": row["stylist_name"],
                     "starts_at": row["starts_at"].isoformat(), "ends_at": row["ends_at"].isoformat()}
        await _save(ctx, row["salon_id"], row["wa_id"], {
            "state": "owner_late_offer", "owner_late_booking_id": row["booking_id"],
            "owner_late_expires_at": deadline.isoformat(), "service_id": row["service_id"],
            "service_name": row["service_name"], "date": row["starts_at"].date().isoformat(),
            "time": row["starts_at"].astimezone(tz).strftime("%H:%M"), "late_slot": late_slot,
            "history": state.get("history", []),
        })
    return True


async def _send_manual_deposit_prompt(ctx, row) -> bool:
    if row is None or row["owner_check_status"] != "approved" or row["owner_customer_notice_at"] is not None:
        return False
    salon = _owner_salon(row)
    acct = await payments.account(ctx.pool, row["salon_id"])
    if acct is None:
        await ctx.pool.execute("UPDATE bookings SET status='cancelled', owner_check_status='declined', hold_expires_at=NULL WHERE id=$1::uuid",
                               row["booking_id"])
        await outbound.send_text(ctx, salon, row["wa_id"], t(row["lang"] or "en", "payment_unconfigured", salon=row["salon_name"]))
        return False
    tz = ZoneInfo(row["timezone"])
    when = when_label(row["starts_at"], tz)
    amount = payments.deposit_amount(salon, {"price_kes": row["price_kes"]})
    state_before = await _owner_customer_state(ctx, row)
    if amount == 0:
        await ctx.pool.execute("UPDATE bookings SET status='confirmed', hold_expires_at=NULL WHERE id=$1::uuid AND status='held'",
                               row["booking_id"])
        try:
            await schedule_reminders(ctx.pool, row["booking_id"])
        except Exception:
            log.exception("could not schedule reminders for owner-approved free booking %s", row["booking_id"])
        clean = {"history": state_before.get("history", [])}
        await _save(ctx, row["salon_id"], row["wa_id"], clean)
        await outbound.send_text(ctx, salon, row["wa_id"],
                                 t(row["lang"] or "en", "booked_free", service=row["service_name"], when=when,
                                   stylist=row["stylist_name"], salon=row["salon_name"]))
        if row.get("owner_wa_id"):
            await outbound.send_text(ctx, salon, row["owner_wa_id"],
                                     t("en", "owner_booked_free", client=row["client_name"] or "customer",
                                       service=row["service_name"], when=when, stylist=row["stylist_name"]))
        await ctx.pool.execute("UPDATE bookings SET owner_customer_notice_at=now() WHERE id=$1::uuid", row["booking_id"])
        return True
    state = {"state": "held", "booking_id": row["booking_id"], "amount": amount,
             "service_id": row["service_id"], "service_name": row["service_name"],
             "held_at": datetime.now(tz).isoformat(), "held_when": when, "held_stylist": row["stylist_name"],
             "history": state_before.get("history", [])}
    phone = mpesa.normalize_msisdn(row["wa_id"])
    manual = bool(acct.get("manual"))
    if manual:
        prompt = payments.manual_instructions(row["lang"] or "en", amount, acct, row["salon_name"])
    elif phone:
        state["phone"] = phone
        prompt = await _prompt(ctx, salon, row["booking_id"], phone, amount, row["lang"] or "en")
    else:
        prompt = t(row["lang"] or "en", "ask_number", amount=amount)
    await _save(ctx, row["salon_id"], row["wa_id"], state)
    message = (t(row["lang"] or "en", "owner_manual_yes") + "\n" +
               t(row["lang"] or "en", "held", service=row["service_name"], when=when,
                 stylist=row["stylist_name"], mins=ctx.settings.hold_minutes) + "\n" + prompt)
    if manual:
        ok = await outbound.send_buttons(ctx, salon, row["wa_id"], _manual_payment_instructions(message, row["lang"] or "en"),
                                          _manual_payment_buttons(row["booking_id"], row["lang"] or "en"))
    else:
        ok = await outbound.send_text(ctx, salon, row["wa_id"], message)
    if ok:
        await ctx.pool.execute("UPDATE bookings SET owner_customer_notice_at=now() WHERE id=$1::uuid AND owner_customer_notice_at IS NULL",
                               row["booking_id"])
    return ok


async def owner_availability_response(ctx, salon: dict, booking_id: str, answer: str) -> str:
    """Process the owner's five-minute manual-payment availability answer."""
    event = "stale"
    timeout_needs_notice = False
    async with ctx.pool.acquire() as con:
        async with con.transaction():
            row = await _owner_booking_row(con, booking_id, lock=True)
            if row is None or row["salon_id"] != salon["id"]:
                return "stale"
            now = datetime.now(ZoneInfo("UTC"))
            status = row["owner_check_status"]
            if status == "pending" and row["booking_status"] == "held":
                expired = row["owner_check_expires_at"] is None or row["owner_check_expires_at"] <= now
                if expired:
                    await con.execute("UPDATE bookings SET status='cancelled', hold_expires_at=NULL, owner_check_status='timed_out', "
                                      "owner_check_responded_at=$2 WHERE id=$1::uuid", booking_id, now)
                    timeout_needs_notice = True
                    if answer == "yes":
                        await con.execute("UPDATE bookings SET owner_check_status='late_approved', owner_late_notice_at=NULL, "
                                          "customer_reply_expires_at=NULL WHERE id=$1::uuid", booking_id)
                        event = "late"
                    else:
                        event = "timeout"
                elif answer == "yes":
                    await con.execute("UPDATE bookings SET owner_check_status='approved', owner_check_responded_at=$2, "
                                      "hold_expires_at=$3 WHERE id=$1::uuid", booking_id, now,
                                      now + timedelta(minutes=int(ctx.settings.hold_minutes)))
                    event = "approved"
                else:
                    await con.execute("UPDATE bookings SET status='cancelled', hold_expires_at=NULL, owner_check_status='declined', "
                                      "owner_check_responded_at=$2 WHERE id=$1::uuid", booking_id, now)
                    event = "declined"
            elif status == "timed_out" and answer == "yes":
                await con.execute("UPDATE bookings SET owner_check_status='late_approved', owner_check_responded_at=$2, "
                                  "owner_late_notice_at=NULL, customer_reply_expires_at=NULL WHERE id=$1::uuid", booking_id, now)
                event = "late"
            elif status == "approved" and answer == "yes" and row["owner_customer_notice_at"] is None:
                event = "approved"
            elif status == "late_approved" and answer == "yes" and row["owner_late_notice_at"] is None:
                event = "late"
    row = await _owner_booking_row(ctx.pool, booking_id)
    if timeout_needs_notice:
        await _send_owner_timeout_notice(ctx, row)
    if event == "approved":
        await _send_manual_deposit_prompt(ctx, row)
    elif event == "late":
        await _send_owner_late_offer(ctx, row)
    elif event == "declined":
        state = await _owner_customer_state(ctx, row)
        await _save(ctx, row["salon_id"], row["wa_id"], {
            "state": "asking", "service_id": row["service_id"], "service_name": row["service_name"],
            "history": state.get("history", []),
        })
        await outbound.send_text(ctx, _owner_salon(row), row["wa_id"],
                                 t(row["lang"] or "en", "owner_manual_no"))
    return event


async def cancel_owner_availability_request(ctx, salon: dict, sender: str, st: dict, chat, lang: str) -> bool:
    booking_id = st.get("booking_id")
    if not booking_id:
        return False
    changed = await ctx.pool.fetchval(
        "UPDATE bookings SET status='cancelled', hold_expires_at=NULL, owner_check_status='declined', "
        "owner_check_responded_at=now() WHERE id=$1::uuid AND salon_id=$2::uuid AND owner_check_status='pending' "
        "AND status='held' RETURNING id::text", booking_id, salon["id"])
    if changed:
        await _save(ctx, salon["id"], sender, {"history": st.get("history", [])})
        await _reply(ctx, salon["id"], sender, {"history": st.get("history", [])}, chat,
                     t(lang, "owner_manual_cancelled"))
        return True
    return False


async def expire_owner_availability_checks(ctx) -> int:
    """Release timed-out manual checks, retry notices, and discard unanswered late offers."""
    count = 0
    due = await ctx.pool.fetch("SELECT id::text AS booking_id FROM bookings WHERE status='held' "
                               "AND owner_check_status='pending' AND owner_check_expires_at<=now()")
    for item in due:
        bid = item["booking_id"]
        changed = await ctx.pool.fetchval(
            "UPDATE bookings SET status='cancelled', hold_expires_at=NULL, owner_check_status='timed_out', "
            "owner_check_responded_at=now() WHERE id=$1::uuid AND status='held' AND owner_check_status='pending' "
            "AND owner_check_expires_at<=now() RETURNING id::text", bid)
        if changed:
            row = await _owner_booking_row(ctx.pool, bid)
            await _clear_owner_customer_state(ctx, row, bid)
            await _send_owner_timeout_notice(ctx, row)
            count += 1
    retry_timeouts = await ctx.pool.fetch("SELECT id::text AS booking_id FROM bookings WHERE owner_check_status='timed_out' "
                                          "AND owner_check_timeout_notice_at IS NULL")
    for item in retry_timeouts:
        await _send_owner_timeout_notice(ctx, await _owner_booking_row(ctx.pool, item["booking_id"]))
    late = await ctx.pool.fetch("SELECT id::text AS booking_id FROM bookings WHERE owner_check_status='late_approved' "
                                "AND owner_late_notice_at IS NULL")
    for item in late:
        await _send_owner_late_offer(ctx, await _owner_booking_row(ctx.pool, item["booking_id"]))
    expired_customers = await ctx.pool.fetch("SELECT id::text AS booking_id FROM bookings WHERE owner_check_status='late_approved' "
                                             "AND customer_reply_expires_at<=now()")
    for item in expired_customers:
        bid = item["booking_id"]
        changed = await ctx.pool.fetchval("UPDATE bookings SET owner_check_status='customer_expired' "
                                          "WHERE id=$1::uuid AND owner_check_status='late_approved' "
                                          "AND customer_reply_expires_at<=now() RETURNING id::text", bid)
        if changed:
            row = await _owner_booking_row(ctx.pool, bid)
            await _clear_owner_customer_state(ctx, row, bid)
            count += 1
    return count


async def resume_late_owner_booking(ctx, salon, client, st, services, tz, now, chat, lang, booking_id: str, answer: str) -> bool:
    row = await _owner_booking_row(ctx.pool, booking_id)
    if (row is None or row["salon_id"] != salon["id"] or row["owner_check_status"] != "late_approved"
            or row["customer_reply_expires_at"] is None or row["customer_reply_expires_at"] <= datetime.now(ZoneInfo("UTC"))):
        await ctx.pool.execute("UPDATE bookings SET owner_check_status='customer_expired' WHERE id=$1::uuid "
                               "AND owner_check_status='late_approved'", booking_id)
        clean = {"history": st.get("history", [])}
        await _save(ctx, salon["id"], chat.to, clean)
        await _reply(ctx, salon["id"], chat.to, clean, chat, t(lang, "owner_manual_late_expired"))
        return True
    if answer == "no":
        await ctx.pool.execute("UPDATE bookings SET owner_check_status='customer_declined' WHERE id=$1::uuid "
                               "AND owner_check_status='late_approved'", booking_id)
        clean = {"history": st.get("history", [])}
        await _save(ctx, salon["id"], chat.to, clean)
        await _reply(ctx, salon["id"], chat.to, clean, chat, t(lang, "owner_manual_late_declined"))
        return True
    await ctx.pool.execute("UPDATE bookings SET owner_check_status='customer_accepted' WHERE id=$1::uuid "
                           "AND owner_check_status='late_approved'", booking_id)
    offered = {
        "state": "offered", "service_id": row["service_id"], "service_name": row["service_name"],
        "date": row["starts_at"].date().isoformat(), "window": None,
        "time": row["starts_at"].astimezone(tz).strftime("%H:%M"),
        "slots": [{"staff_id": row["staff_id"], "staff_name": row["stylist_name"],
                   "starts_at": row["starts_at"].isoformat(), "ends_at": row["ends_at"].isoformat()}],
        "owner_preapproved": True, "history": st.get("history", []),
    }
    await _save(ctx, salon["id"], chat.to, offered)
    await _choose(ctx, salon, client, offered, services, tz, now, chat, lang, 1)
    return True


async def expire_owner_late_offers(ctx) -> int:
    """Compatibility entry point for the app sweeper; see expire_owner_availability_checks."""
    return await expire_owner_availability_checks(ctx)


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
    """Use STK for Daraja accounts or give the selected manual transfer instructions."""
    acct = await payments.account(ctx.pool, salon["id"])
    if acct is not None and acct.get("manual"):
        return payments.manual_instructions(lang, amount, acct, salon["name"])
    result = await payments.request_deposit(ctx, salon, booking_id, phone, amount, message_id=outbound.current_message_id())
    if result == "failed":
        return t(lang, "stk_failed")
    return t(lang, "stk_wait" if result == "wait" else "stk_sent", amount=amount, phone=phone)


async def _till_claim(ctx, salon, client, st, chat, lang, text: str,
                      extracted: PaymentScreenshotGuess | None = None) -> None:
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
        "SELECT id::text, amount_kes FROM payments WHERE booking_id=$1::uuid AND status IN ('pending','review') "
        "AND method='manual' ORDER BY created_at DESC LIMIT 1", bid)
    if pending is not None:
        await _reply_buttons(ctx, salon["id"], chat.to, st, chat,
                             t(lang, "till_still_waiting", salon=salon["name"], amount=pending["amount_kes"]),
                             _manual_payment_buttons(bid, lang, include_paid=False))
        return
    acct = await payments.account(ctx.pool, salon["id"])
    provider = payments.manual_provider(acct)
    evidence = text
    if extracted is not None:
        if not acct or not acct.get("manual") or extracted.provider != provider:
            await _reply(ctx, salon["id"], chat.to, st, chat, t(lang, "till_photo_unreadable", salon=salon["name"]))
            return
        claimed = extracted.amount
        code = payments.normalize_reference(extracted.reference)
        ambiguous = False
        details = []
        if extracted.recipient:
            details.append(f"recipient {extracted.recipient}")
        timestamp = " ".join(part for part in (extracted.date, extracted.time) if part)
        if timestamp:
            details.append(f"date/time {timestamp}")
        evidence = "Vision extracted (unverified)" + (": " + "; ".join(details) if details else ".")
    else:
        claimed, code, ambiguous = payments.parse_claim(text)
    if ambiguous or (claimed is not None and amount and claimed != amount):
        await _file_for_review(ctx, salon, st, chat, lang, bid, amount, claimed, code, evidence)
        return
    if claimed is None or code is None:
        if extracted is not None:
            await _reply_buttons(ctx, salon["id"], chat.to, st, chat, t(lang, "till_photo_unreadable", salon=salon["name"]),
                                 _manual_payment_buttons(bid, lang, include_paid=False))
            return
        if re.fullmatch(r"\s*(?:pay|lipa|lipa sasa|malipo|maelekezo)\s*[.!?]*", text or "", re.I):
            await _reply_buttons(ctx, salon["id"], chat.to, st, chat,
                                 _manual_payment_instructions(payments.manual_instructions(lang, amount, acct or {}, salon["name"]), lang),
                                 _manual_payment_buttons(bid, lang))
            return
        await _reply_buttons(ctx, salon["id"], chat.to, st, chat, t(lang, "till_again", amount=amount),
                             _manual_payment_buttons(bid, lang))
        return
    if await ctx.pool.fetchval("SELECT 1 FROM payments WHERE provider=$1 AND mpesa_receipt=$2", provider, code):
        await _reply_buttons(ctx, salon["id"], chat.to, st, chat, t(lang, "till_code_used"),
                             _manual_payment_buttons(bid, lang))
        return
    pay_id = await payments.record_manual_claim(ctx.pool, bid, salon["id"], mpesa.normalize_msisdn(chat.to) or chat.to,
                                                claimed, code, evidence, provider=provider)
    await ctx.pool.execute(f"UPDATE bookings SET hold_expires_at = GREATEST(hold_expires_at, now() + interval '{payments.MANUAL_HOLD}') "
                           "WHERE id=$1::uuid AND status='held'", bid)
    if extracted is not None:
        recipient_note = f", recipient {extracted.recipient}" if extracted.recipient else ""
        when_note = f", date/time {' '.join(part for part in (extracted.date, extracted.time) if part)}" if extracted.date or extracted.time else ""
        customer_message = t(lang, "till_screenshot_waiting", salon=salon["name"], amount=claimed,
                             code=code, recipient=recipient_note, when=when_note)
    else:
        customer_message = t(lang, "till_waiting", salon=salon["name"], amount=claimed)
    await _reply_buttons(ctx, salon["id"], chat.to, st, chat, customer_message,
                         _manual_payment_buttons(bid, lang, include_paid=False))
    if salon.get("owner_wa_id"):
        await _ask_owner(ctx, salon, st, chat, lang, bid, pay_id, claimed, code, evidence)


async def _file_for_review(ctx, salon, st, chat, lang, bid: str, expected: int, claimed: int | None, code: str | None, text: str) -> None:
    acct = await payments.account(ctx.pool, salon["id"])
    provider = payments.manual_provider(acct)
    pay_id = await ctx.pool.fetchval(
        "INSERT INTO payments (booking_id, salon_id, phone, amount_kes, method, provider, status, mpesa_receipt, claim_ref) "
        "VALUES ($1::uuid,$2::uuid,$3,$4,'manual',$5,'review',$6,$7) RETURNING id::text",
        bid, salon["id"], mpesa.normalize_msisdn(chat.to) or chat.to, expected or claimed or 1, provider, code, text[:500])
    reason = "amount_mismatch" if claimed and expected and claimed != expected else "payment_unresolved"
    await ctx.pool.execute(
        "INSERT INTO payment_reviews (payment_id, reason, status, expected_amount, received_amount, mpesa_receipt) "
        "VALUES ($1,$2,'pending_review',$3,$4,$5) ON CONFLICT (payment_id) DO NOTHING",
        pay_id, reason, expected or 0, claimed, code)
    await ctx.pool.execute(f"UPDATE bookings SET hold_expires_at=GREATEST(hold_expires_at, now() + interval '{payments.MANUAL_HOLD}') "
                           "WHERE id=$1::uuid AND status='held'", bid)
    await _reply_buttons(ctx, salon["id"], chat.to, st, chat, t(lang, "till_review", salon=salon["name"]),
                         _manual_payment_buttons(bid, lang, include_paid=False))
    if salon.get("owner_wa_id"):
        body = t("en", "till_owner_ask", service=st.get("service_name") or "", when=st.get("held_when") or "",
                 client=chat.to, amount=claimed or expected, code=code or "?", ref=text[:200],
                 destination=payments.destination_label(acct))
        body += "\n" + t("en", "till_owner_amount_review", expected=expected or 0,
                         received=claimed if claimed is not None else "unclear")
        await outbound.send_buttons(ctx, salon, salon["owner_wa_id"], body,
                                    [(f"till:yes:{pay_id}", "Yes"), (f"till:no:{pay_id}", "No")])


async def _ask_owner(ctx, salon, st, chat, lang, bid: str, pay_id: str, claimed: int, code: str, text: str) -> None:
    await outbound.send_buttons(
        ctx, salon, salon["owner_wa_id"],
        t("en", "till_owner_ask", service=st.get("service_name") or "", when=st.get("held_when") or "",
          client=chat.to, amount=claimed, code=code, ref=text[:200],
          destination=payments.destination_label(await payments.account(ctx.pool, salon["id"]))),
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
    acct = await payments.account(ctx.pool, salon["id"])
    if acct and acct.get("manual"):
        await _reply_buttons(ctx, salon["id"], chat.to, st, chat,
                             _manual_payment_instructions(await _prompt(ctx, salon, st["booking_id"], "", amount, lang), lang),
                             _manual_payment_buttons(st["booking_id"], lang))
        return
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
