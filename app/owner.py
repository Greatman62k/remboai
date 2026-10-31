"""The salon owner's side of WhatsApp. A small fixed vocabulary matched by code (no model): an owner is giving
orders that change real bookings, so the words are exact and the effect is predictable.

An owner message that is not a command falls through to the customer conversation, so the owner can also try
the booking flow from his own phone."""
from __future__ import annotations

import json
import logging
import re
from datetime import date, datetime, time, timedelta
from zoneinfo import ZoneInfo

import asyncpg

from . import payments
from .channels import outbound
from .availability import day_slots
from .bookings import busy_times
from .messages import t
from .reminders import cancel_reminders, schedule_reminders
from .timeparse import parse_day, parse_time
from .channels.whatsapp import mask

log = logging.getLogger("rembo.owner")
SUMMARY_HOUR = 20

HELP = re.compile(r"^\s*(help|menu|msaada|\?)\W*$", re.I)
LIST = re.compile(r"^\s*(today|tomorrow|leo|kesho|bookings?|schedule|ratiba|miadi)\b", re.I)
NOSHOW = re.compile(r"^\s*(noshow|no.show|no_show|missed|mhudhurio)\s+#?(\d{1,2})\W*$", re.I)
BLOCK = re.compile(r"^\s*(block|close|funga)\b(.*)$", re.I | re.S)
UNBLOCK = re.compile(r"^\s*(unblock|reopen|open|fungua)\b(.*)$", re.I | re.S)
CANCEL = re.compile(r"^\s*(cancel|ghairi)\s+#?(\d{1,2})\W*$", re.I)
CANCEL_WHO = re.compile(r"^\s*(cancel|ghairi)\s+(\+?[\d][\d\s\-]{5,18})\W*$", re.I)
MOVE = re.compile(r"^\s*(move|hamisha|sogeza)\s+(\S+)\s+(.+)$", re.I | re.S)
LATE = re.compile(r"^\s*(late|chelewa)\s+(.+?)\s+(\d{1,3})\s*$", re.I | re.S)
INVITE = re.compile(r"^\s*(invite|mwaliko|share)\W*$", re.I)
KEEP = re.compile(r"^\s*(keep|endelea)\W*$", re.I)
DEPOSIT = re.compile(r"^\s*(deposit|amana)\b\s*(.*?)\s*$", re.I | re.S)
SUMMARY = re.compile(r"^\s*(summary|muhtasari)\W*$", re.I)
TILL_TAP = re.compile(r"^till:(yes|no):([0-9a-fA-F-]{36})$")
SWAHILI = re.compile(r"\b(leo|kesho|ratiba|miadi|funga|fungua|ghairi|muhtasari|msaada|kiungo|chelewa|hamisha|sogeza|amana|jumatatu|jumanne|jumatano|alhamisi|ijumaa|jumamosi|jumapili)\b", re.I)
RANGE = re.compile(r"(\d{1,2}(?::\d{2})?\s*(?:am|pm)?)\s*(?:-|–|to|hadi|mpaka)\s*(\d{1,2}(?::\d{2})?\s*(?:am|pm)?)", re.I)


def is_owner(salon: dict, sender: str) -> bool:
    return bool(salon.get("owner_wa_id")) and salon["owner_wa_id"] == sender


def _clock(s: str) -> time | None:
    s = s.strip()
    return parse_time(s) or (parse_time("at " + s) if s.isdigit() else None)


def parse_range(text: str) -> tuple[time, time] | None | bool:
    """None: no range in the text. False: there is one but it is unusable. Otherwise (start, end)."""
    m = RANGE.search(re.sub(r"\d{4}-\d{2}-\d{2}", " ", text))
    if not m:
        return None
    a, b = _clock(m[1]), _clock(m[2])
    return (a, b) if a and b and b > a else False


def _label(day: date, today: date, lang: str) -> str:
    if day == today:
        return "leo" if lang == "sw" else "today"
    if day == today + timedelta(days=1):
        return "kesho" if lang == "sw" else "tomorrow"
    return day.strftime("%a %d %b")


async def _say(ctx, salon: dict, to: str, body: str) -> None:
    await outbound.send_text(ctx, salon, to, body)


async def _bookings(ctx, salon: dict, day: date, tz: ZoneInfo) -> list[dict]:
    start = datetime.combine(day, time(0), tz)
    rows = await ctx.pool.fetch(
        "SELECT b.id::text, b.starts_at, b.status, sv.name AS service, c.name AS client, c.wa_id AS phone, "
        "(SELECT sum(amount_kes) FROM payments p WHERE p.booking_id=b.id AND p.status='paid') AS paid "
        "FROM bookings b JOIN services sv ON sv.id=b.service_id JOIN clients c ON c.id=b.client_id "
        "WHERE b.salon_id=$1::uuid AND b.status IN ('confirmed','held') AND b.starts_at >= $2 AND b.starts_at < $3 "
        "AND (b.status='confirmed' OR b.hold_expires_at > now()) ORDER BY b.starts_at", salon["id"], start, start + timedelta(days=1))
    return [dict(r) for r in rows]


def _lines(rows: list[dict], tz: ZoneInfo, lang: str) -> str:
    out = []
    for i, r in enumerate(rows, 1):
        pay = t(lang, "owner_paid", amount=r["paid"]) if r["paid"] else t(lang, "owner_unpaid")
        out.append(t(lang, "owner_line", n=i, time=r["starts_at"].astimezone(tz).strftime("%H:%M"), service=r["service"],
                     client=r["client"] or "customer", phone="+" + r["phone"], pay=pay))
    return "\n".join(out)


async def _remember_list(ctx, salon: dict, owner: str, rows: list[dict]) -> None:
    await ctx.pool.execute(
        "INSERT INTO conversations (salon_id, wa_id, state) VALUES ($1::uuid,$2,$3::jsonb) "
        "ON CONFLICT (salon_id, wa_id) DO UPDATE SET state=EXCLUDED.state, updated_at=now()",
        salon["id"], owner, json.dumps({"owner_list": [r["id"] for r in rows]}))


async def handle(ctx, salon: dict, to: str, text: str, tz: ZoneInfo, now: datetime) -> bool:
    """Run an owner command. Returns False if `text` is not one (then it is treated as a customer message)."""
    if not is_owner(salon, to):
        return False
    today, lang = now.date(), "sw" if SWAHILI.search(text) else "en"
    say = lambda body: _say(ctx, salon, to, body)  # noqa: E731

    if HELP.match(text):
        await say(t(lang, "owner_help"))
        return True
    if KEEP.match(text):
        await ctx.pool.execute("UPDATE salons SET inactivity_warned_at=NULL, inactivity_final_at=NULL WHERE id=$1::uuid", salon["id"])
        await say(t(lang, "owner_kept", salon=salon["name"]))
        return True
    if m := DEPOSIT.match(text):
        await _deposit(ctx, salon, to, m[2], lang)
        return True
    if INVITE.match(text):
        from . import binding as _binding
        from .channels import invites as _invites
        slug = await _binding.ensure_slug(ctx.pool, salon["id"], salon["name"])
        channel = _invites.channel_of(to)
        from .channels.telegram import bot_username as _bot_username
        username = await _bot_username(getattr(ctx, "wa", None)) if channel == "tg" else None
        number = getattr(ctx.settings, "central_whatsapp_number", "") if channel == "wa" else ""
        await say(t(lang, "owner_invite", link=_invites.invite_for(channel, slug, username=username, number=number)))
        return True
    if SUMMARY.match(text):
        await say(await summary_text(ctx, salon, tz, now, lang))
        return True
    if m := NOSHOW.match(text):
        await _noshow(ctx, salon, to, int(m[2]), tz, lang)
        return True
    if m := CANCEL.match(text):
        await _cancel(ctx, salon, to, int(m[2]), tz, lang)
        return True
    if m := CANCEL_WHO.match(text):
        await _cancel_who(ctx, salon, to, m[2], tz, lang)
        return True
    if m := MOVE.match(text):
        await _move_who(ctx, salon, to, m[2], m[3], tz, today, lang)
        return True
    if m := LATE.match(text):
        await _late(ctx, salon, to, m[2], m[3], tz, today, lang)
        return True
    if m := BLOCK.match(text):
        await _block(ctx, salon, to, m[2], tz, today, lang)
        return True
    if m := UNBLOCK.match(text):
        await _unblock(ctx, salon, to, m[2], tz, today, lang)
        return True
    if LIST.match(text):
        day = parse_day(text, today) or today
        rows = await _bookings(ctx, salon, day, tz)
        label = _label(day, today, lang)
        await _remember_list(ctx, salon, to, rows)
        await say(t(lang, "owner_day", label=label, lines=_lines(rows, tz, lang)) if rows else t(lang, "owner_none", label=label))
        return True
    return False


async def handle_tap(ctx, salon: dict, sender: str, reply_id: str) -> bool:
    """An owner's tap on a manual-claim confirm request. Only the owner's tap counts;
    anything else falls through to the customer flow."""
    m = TILL_TAP.match(reply_id or "")
    if not m or not is_owner(salon, sender):
        return False
    answer, pay_id = m[1].lower(), m[2]
    if answer == "yes":
        status, bid = await payments.confirm_manual_claim(ctx.pool, pay_id)
        if status == "confirmed":
            await _announce_manual(ctx, bid, pay_id)
        elif status == "already":
            await _say(ctx, salon, sender, t("en", "till_owner_already"))
        else:
            await _say(ctx, salon, sender, t("en", "till_owner_closed"))
    else:
        status, bid = await payments.decline_manual_claim(ctx.pool, pay_id)
        if status == "declined":
            await _say(ctx, salon, sender, t("en", "till_owner_recorded"))
            await _tell_declined(ctx, salon, bid)
        elif status == "already":
            await _say(ctx, salon, sender, t("en", "till_owner_already"))
        else:
            await _say(ctx, salon, sender, t("en", "till_owner_closed"))
    return True


async def _announce_manual(ctx, booking_id: str, pay_id: str) -> None:
    """The owner confirmed the money arrived: reuse the paid announcement path (customer booked,
    owner notified, reminders scheduled, exactly once)."""
    p = await ctx.pool.fetchrow("SELECT * FROM payments WHERE id=$1::uuid", pay_id)
    d = await ctx.pool.fetchrow(payments._DETAIL, booking_id)
    s = payments.Settlement("paid", "", str(p["id"]), booking_id, p["amount_kes"], p["phone"], p["mpesa_receipt"],
                            p["result_code"], settled_via="manual", **dict(d))
    await payments.announce(ctx, s)


async def _tell_declined(ctx, salon: dict, booking_id: str | None) -> None:
    if not booking_id:
        return
    row = await ctx.pool.fetchrow("SELECT c.wa_id, c.lang FROM bookings b JOIN clients c ON c.id=b.client_id WHERE b.id=$1::uuid", booking_id)
    if row is None:
        return
    await outbound.send_text(ctx, salon, row["wa_id"], t(row["lang"], "till_declined", salon=salon["name"]))


async def _block(ctx, salon, to, rest: str, tz, today: date, lang: str) -> None:
    day = parse_day(rest, today)
    if day is None:
        await _say(ctx, salon, to, t(lang, "owner_ask_day"))
        return
    rng = parse_range(rest)
    if rng is False:
        await _say(ctx, salon, to, t(lang, "owner_bad_range"))
        return
    start = datetime.combine(day, rng[0] if rng else time(0), tz)
    end = datetime.combine(day, rng[1], tz) if rng else datetime.combine(day + timedelta(days=1), time(0), tz)
    await ctx.pool.execute("INSERT INTO time_off (salon_id, starts_at, ends_at, note) VALUES ($1::uuid,$2,$3,'owner')", salon["id"], start, end)
    what = _label(day, today, lang) + (f" {rng[0]:%H:%M}-{rng[1]:%H:%M}" if rng else "")
    rows = await ctx.pool.fetch(
        "SELECT b.id::text, b.starts_at, b.status, sv.name AS service, c.name AS client, c.wa_id AS phone, c.lang, "
        "(SELECT sum(amount_kes) FROM payments p WHERE p.booking_id=b.id AND p.status='paid') AS paid "
        "FROM bookings b JOIN services sv ON sv.id=b.service_id JOIN clients c ON c.id=b.client_id "
        "WHERE b.salon_id=$1::uuid AND b.status IN ('confirmed','held') AND b.starts_at < $3 AND b.ends_at > $2 ORDER BY b.starts_at",
        salon["id"], start, end)
    msg = t(lang, "owner_blocked", what=what)
    if rows:
        rows = [dict(r) for r in rows]
        await _remember_list(ctx, salon, to, rows)
        msg += "\n" + t(lang, "owner_overlap", n=len(rows), lines=_lines(rows, tz, lang))
        for r in rows:
            await outbound.send_text(ctx, salon, r["phone"],
                                     t(r["lang"] or "en", "closed_day", salon=salon["name"], when=what))
        msg += "\n" + t(lang, "owner_block_told", n=len(rows))
    await _say(ctx, salon, to, msg)


async def _unblock(ctx, salon, to, rest: str, tz, today: date, lang: str) -> None:
    day = parse_day(rest, today)
    if day is None:
        await _say(ctx, salon, to, t(lang, "owner_ask_day"))
        return
    start = datetime.combine(day, time(0), tz)
    n = int((await ctx.pool.execute("DELETE FROM time_off WHERE salon_id=$1::uuid AND starts_at < $3 AND ends_at > $2",
                                    salon["id"], start, start + timedelta(days=1))).split()[-1])
    what = _label(day, today, lang)
    await _say(ctx, salon, to, t(lang, "owner_unblocked" if n else "owner_no_blocks", what=what))


async def _deposit(ctx, salon, to, rest: str, lang: str) -> None:
    """Set the booking deposit in KES. Safe without a till: with no till configured the amount is stored and bookings stay free."""
    m = re.fullmatch(r"\d[\d,]*", (rest or "").strip())
    amount = int(m.group(0).replace(",", "")) if m else -1
    if not 0 <= amount <= 100000:
        await _say(ctx, salon, to, t(lang, "owner_deposit_bad"))
        return
    await ctx.pool.execute("UPDATE salons SET deposit_kes=$2 WHERE id=$1::uuid", salon["id"], amount)
    if amount == 0:
        await _say(ctx, salon, to, t(lang, "owner_deposit_off"))
        return
    acct = await payments.account(ctx.pool, salon["id"])
    note = t(lang, "owner_deposit_till", kind=acct["kind"], party=acct["party_b"]) if acct else t(lang, "owner_deposit_notill")
    await _say(ctx, salon, to, t(lang, "owner_deposit_set", amount=amount, note=note))


async def cancel_booking(ctx, salon: dict, booking_id: str, tz: ZoneInfo) -> dict | None:
    """Cancel a held or confirmed booking of this salon and tell the customer. None if there was nothing to cancel.
    Used by the WhatsApp command and by the web/app API, so both behave identically."""
    row = await ctx.pool.fetchrow(
        "UPDATE bookings b SET status='cancelled' FROM services sv, clients c WHERE b.id=$1::uuid AND b.salon_id=$2::uuid "
        "AND b.status IN ('held','confirmed') AND sv.id=b.service_id AND c.id=b.client_id "
        "RETURNING b.id::text, b.starts_at, sv.name AS service, c.name AS client, c.wa_id AS phone, c.lang, "
        "(SELECT sum(amount_kes) FROM payments p WHERE p.booking_id=b.id AND p.status='paid') AS paid", booking_id, salon["id"])
    if row is None:
        return None
    row = dict(row)
    await payments.park_for_cancellation(ctx.pool, row["id"])
    await cancel_reminders(ctx.pool, row["id"])
    when = row["starts_at"].astimezone(tz).strftime("%a %d %b, %H:%M")
    cust_refund = t(row["lang"], "cancelled_by_salon_refund", amount=row["paid"]) if row["paid"] else ""
    await _say(ctx, salon, row["phone"], t(row["lang"], "cancelled_by_salon", salon=salon["name"], when=when, refund=cust_refund))
    log.info("owner cancelled a booking for %s", mask(row["phone"]))
    row["when"] = when
    return row


async def _noshow(ctx, salon, to, n: int, tz, lang: str) -> None:
    raw = await ctx.pool.fetchval("SELECT state FROM conversations WHERE salon_id=$1::uuid AND wa_id=$2", salon["id"], to)
    state = raw if isinstance(raw, dict) else __import__("json").loads(raw or "{}")
    ids = state.get("owner_list") or []
    if not 1 <= n <= len(ids):
        await _say(ctx, salon, to, t(lang, "owner_bad_number", n=n))
        return
    bid = ids[n - 1]
    row = await ctx.pool.fetchrow("UPDATE bookings SET status='no_show' WHERE id=$1::uuid AND salon_id=$2::uuid AND status='confirmed' RETURNING id::text, starts_at", bid, salon["id"])
    if row is None:
        held = await ctx.pool.fetchval("SELECT 1 FROM bookings WHERE id=$1::uuid AND status='held'", bid)
        await _say(ctx, salon, to, t(lang, "owner_noshow_held" if held else "owner_bad_number", n=n))
        return
    await ctx.pool.execute("UPDATE reminder_jobs SET status='cancelled' WHERE booking_id=$1::uuid AND status='pending'", bid)
    when = row["starts_at"].astimezone(tz).strftime("%a %d %b, %H:%M")
    await _say(ctx, salon, to, t(lang, "owner_noshow", when=when))
    log.info("owner marked no-show %s", bid)

def _normalize_customer(spec: str) -> str | None:
    """A typed phone number to the wa_id shape the database uses, or None when it is not a number at all."""
    digits = re.sub(r"\D", "", spec or "")
    if digits.startswith("254") and len(digits) == 12:
        return digits
    if len(digits) == 10 and digits.startswith("0"):
        return "254" + digits[1:]
    if len(digits) == 9 and digits[0] in "17":
        return "254" + digits
    return None


async def _customer_bookings(ctx, salon: dict, spec: str, owner: str) -> list[dict] | None:
    """Upcoming bookings for a phone number, or the one entry a bare number points at in the owner's last list.
    None means the spec is neither."""
    if re.fullmatch(r"\d{1,2}", (spec or "").strip()):
        raw = await ctx.pool.fetchval("SELECT state FROM conversations WHERE salon_id=$1::uuid AND wa_id=$2", salon["id"], owner)
        state = raw if isinstance(raw, dict) else json.loads(raw or "{}")
        ids = state.get("owner_list") or []
        n = int(spec)
        if not 1 <= n <= len(ids):
            return []
        rows = await ctx.pool.fetch(
            "SELECT b.id::text, sv.name AS service, b.starts_at, c.name AS client, c.wa_id AS phone "
            "FROM bookings b JOIN services sv ON sv.id=b.service_id JOIN clients c ON c.id=b.client_id "
            "WHERE b.id=$1::uuid AND b.salon_id=$2::uuid AND b.status IN ('held','confirmed')", ids[n - 1], salon["id"])
        return [dict(r) for r in rows]
    phone = _normalize_customer(spec)
    if phone is None:
        return None
    rows = await ctx.pool.fetch(
        "SELECT b.id::text, sv.name AS service, b.starts_at, c.name AS client, c.wa_id AS phone "
        "FROM bookings b JOIN services sv ON sv.id=b.service_id JOIN clients c ON c.id=b.client_id "
        "WHERE b.salon_id=$1::uuid AND c.wa_id=$2 AND b.status IN ('held','confirmed') AND b.starts_at > now() "
        "ORDER BY b.starts_at", salon["id"], phone)
    return [dict(r) for r in rows]


async def _report_cancel(ctx, salon, to, row: dict | None, tz, lang: str, n: int = 0) -> None:
    if row is None:
        await _say(ctx, salon, to, t(lang, "owner_bad_number", n=n))
        return
    refund = t(lang, "owner_refund_hint", amount=row["paid"], phone="+" + row["phone"]) if row["paid"] else ""
    await _say(ctx, salon, to, t(lang, "owner_cancelled", service=row["service"], when=row["when"], client=row["client"] or "customer", refund=refund))


async def _cancel_who(ctx, salon, to, spec: str, tz, lang: str) -> None:
    rows = await _customer_bookings(ctx, salon, spec, to)
    if rows is None or not rows:
        await _say(ctx, salon, to, t(lang, "owner_cust_none", phone=spec.strip()))
        return
    if len(rows) > 1:
        await _remember_list(ctx, salon, to, rows)
        lines = "\n".join(t(lang, "owner_cust_line", n=i, service=r["service"],
                                          when=r["starts_at"].astimezone(tz).strftime("%a %d %b, %H:%M")) for i, r in enumerate(rows, 1))
        await _say(ctx, salon, to, t(lang, "owner_cust_pick", phone="+" + rows[0]["phone"], lines=lines))
        return
    await _report_cancel(ctx, salon, to, await cancel_booking(ctx, salon, rows[0]["id"], tz), tz, lang)


async def _move_who(ctx, salon, to, spec: str, rest: str, tz, today: date, lang: str) -> None:
    rows = await _customer_bookings(ctx, salon, spec, to)
    if rows is None or not rows:
        await _say(ctx, salon, to, t(lang, "owner_cust_none", phone=spec.strip()))
        return
    if len(rows) > 1:
        await _remember_list(ctx, salon, to, rows)
        lines = "\n".join(t(lang, "owner_cust_line", n=i, service=r["service"],
                                          when=r["starts_at"].astimezone(tz).strftime("%a %d %b, %H:%M")) for i, r in enumerate(rows, 1))
        await _say(ctx, salon, to, t(lang, "owner_cust_pick", phone="+" + rows[0]["phone"], lines=lines))
        return
    day, now = parse_day(rest, today), datetime.now(tz)
    at = parse_time(rest)
    if day is None or at is None or datetime.combine(day, at, tz) <= now:
        await _say(ctx, salon, to, t(lang, "owner_move_usage"))
        return
    from .availability import DAY_KEYS
    try:
        hours = (salon.get("open_hours") or {})[DAY_KEYS[day.weekday()]]
        opens, closes = time.fromisoformat(hours[0]), time.fromisoformat(hours[1])
    except (ValueError, TypeError, IndexError, KeyError):
        hours, opens, closes = None, None, None
    if not hours:
        await _say(ctx, salon, to, t(lang, "owner_move_closed", label=_label(day, today, lang), hours=t(lang, "closed")))
        return
    b = await ctx.pool.fetchrow(
        "SELECT b.staff_id::text AS staff_id, b.service_id::text AS service_id, sv.name AS service, sv.duration_min, "
        "c.wa_id AS phone, c.lang, st.name AS stylist FROM bookings b JOIN services sv ON sv.id=b.service_id "
        "JOIN clients c ON c.id=b.client_id JOIN staff st ON st.id=b.staff_id "
        "WHERE b.id=$1::uuid AND b.status IN ('held','confirmed')", rows[0]["id"])
    if b is None:
        await _say(ctx, salon, to, t(lang, "owner_bad_number", n=0))
        return
    want = datetime.combine(day, at, tz)
    want_end = want + timedelta(minutes=b["duration_min"])
    if at < opens or want_end > datetime.combine(day, closes, tz):
        await _say(ctx, salon, to, t(lang, "owner_move_closed", label=_label(day, today, lang),
                                     hours=f"{opens:%H:%M}-{closes:%H:%M}"))
        return
    own_start = await ctx.pool.fetchval("SELECT starts_at FROM bookings WHERE id=$1::uuid", rows[0]["id"])
    async with ctx.pool.acquire() as con:
        busy = await busy_times(con, [b["staff_id"]], want - timedelta(hours=12), want_end + timedelta(hours=12))
    clash = any(s < want_end and want < e and s != own_start
                for s, e in busy.get(b["staff_id"], []))
    if clash:
        slots = day_slots(day, salon["open_hours"], tz, timedelta(minutes=b["duration_min"]),
                          [{"id": b["staff_id"], "name": b["stylist"]}], busy, want)
        lines = "\n".join(s.starts_at.astimezone(tz).strftime("%H:%M") for s in slots[:3])
        await _say(ctx, salon, to, t(lang, "owner_move_busy", service=b["service"], when=want.strftime("%a %d %b, %H:%M"), lines=lines or "-"))
        return
    try:
        await ctx.pool.execute("UPDATE bookings SET starts_at=$2, ends_at=$3 WHERE id=$1::uuid AND status IN ('held','confirmed')", rows[0]["id"], want, want_end)
    except asyncpg.ExclusionViolationError:
        await _say(ctx, salon, to, t(lang, "owner_move_busy", service=b["service"], when=want.strftime("%a %d %b, %H:%M"), lines="-"))
        return
    await cancel_reminders(ctx.pool, rows[0]["id"])
    try:
        await schedule_reminders(ctx.pool, rows[0]["id"])
    except Exception:
        log.exception("could not reschedule reminders for moved booking %s", rows[0]["id"])
    clang = b["lang"] or "en"
    await outbound.send_text(ctx, salon, b["phone"], t(clang, "moved", salon=salon["name"], service=b["service"],
                                                       when=want.strftime("%a %d %b, %H:%M"), stylist=b["stylist"]))
    await _say(ctx, salon, to, t(lang, "owner_moved", client=b["phone"], service=b["service"],
                                 when=want.strftime("%a %d %b, %H:%M"), stylist=b["stylist"]))
    log.info("owner moved booking %s to %s", rows[0]["id"], want.isoformat())


async def _cancel(ctx, salon, to, n: int, tz, lang: str) -> None:
    raw = await ctx.pool.fetchval("SELECT state FROM conversations WHERE salon_id=$1::uuid AND wa_id=$2", salon["id"], to)
    state = raw if isinstance(raw, dict) else json.loads(raw or "{}")
    ids = state.get("owner_list") or []
    row = await cancel_booking(ctx, salon, ids[n - 1], tz) if 1 <= n <= len(ids) else None
    if row is None:
        await _say(ctx, salon, to, t(lang, "owner_bad_number", n=n))
        return
    refund = t(lang, "owner_refund_hint", amount=row["paid"], phone="+" + row["phone"]) if row["paid"] else ""
    await _say(ctx, salon, to, t(lang, "owner_cancelled", service=row["service"], when=row["when"], client=row["client"] or "customer", refund=refund))


def _match_staff(name: str, staff: list[dict]) -> list[dict]:
    want = " ".join(name.split()).lower()
    exact = [s for s in staff if s["name"].lower() == want]
    if exact:
        return exact
    starts = [s for s in staff if s["name"].lower().startswith(want)]
    if starts:
        return starts
    return [s for s in staff if want in s["name"].lower()]


async def _late(ctx, salon, to, name: str, minutes: str, tz: ZoneInfo, today: date, lang: str) -> None:
    """Push one stylist's remaining bookings today later, tell each customer with options, and hand the owner the new day schedule."""
    mins = int(minutes)
    if not 1 <= mins <= 480:
        await _say(ctx, salon, to, t(lang, "owner_late_range"))
        return
    staff = [{"id": r["id"], "name": r["name"]} for r in await ctx.pool.fetch(
        "SELECT id::text, name FROM staff WHERE salon_id=$1::uuid AND active ORDER BY name", salon["id"])]
    found = _match_staff(name, staff)
    if not found:
        await _say(ctx, salon, to, t(lang, "owner_late_unknown", name=name.strip()))
        return
    if len(found) > 1:
        await _say(ctx, salon, to, t(lang, "owner_late_ambiguous", names=", ".join(s["name"] for s in found)))
        return
    stylist = found[0]
    start = datetime.combine(today, time(0), tz)
    end = start + timedelta(days=1)
    now = datetime.now(tz)
    async with ctx.pool.acquire() as con, con.transaction():
        fresh = await con.fetchval(
            "INSERT INTO delays (salon_id, staff_id, day, minutes) VALUES ($1::uuid,$2::uuid,$3::date,$4::int) "
            "ON CONFLICT DO NOTHING RETURNING 1", salon["id"], stylist["id"], today, mins)
        moved = []
        if fresh:
            moved = [dict(r) for r in await con.fetch(
                "UPDATE bookings SET starts_at=starts_at + make_interval(mins => $3), ends_at=ends_at + make_interval(mins => $3) "
                "WHERE salon_id=$1::uuid AND staff_id=$2::uuid AND status IN ('held','confirmed') "
                "AND starts_at > $4 AND starts_at >= $5 AND starts_at < $6 "
                "RETURNING id::text, client_id::text, starts_at", salon["id"], stylist["id"], mins, now, start, end)]
            if moved:
                await con.execute(
                    "UPDATE reminder_jobs SET run_at=run_at + make_interval(mins => $2) "
                    "WHERE booking_id = ANY($1::uuid[]) AND status='pending'", [m["id"] for m in moved], mins)
    if fresh and not moved:
        await _say(ctx, salon, to, t(lang, "owner_late_none", stylist=stylist["name"]))
        return
    for m in moved:
        row = await ctx.pool.fetchrow(
            "SELECT sv.name AS service, c.wa_id AS phone, c.lang FROM bookings b "
            "JOIN services sv ON sv.id=b.service_id JOIN clients c ON c.id=b.client_id WHERE b.id=$1::uuid", m["id"])
        when = m["starts_at"].astimezone(tz).strftime("%a %d %b, %H:%M")
        body = t(row["lang"], "late_notice", stylist=stylist["name"], mins=mins, when=when, service=row["service"], salon=salon["name"])
        buttons = [(f"late-keep:{m['id']}", t(row["lang"], "late_btn_keep")[:20]),
                   (f"late-cancel:{m['id']}", t(row["lang"], "late_btn_cancel")[:20]),
                   (f"late-move:{m['id']}", t(row["lang"], "late_btn_move")[:20])]
        await outbound.send_buttons(ctx, salon, row["phone"], body, buttons)
    rows = await _bookings(ctx, salon, today, tz)
    label = _label(today, today, lang)
    await _say(ctx, salon, to, t(lang, "owner_late_done", stylist=stylist["name"], mins=mins, label=label,
                                   lines=_lines(rows, tz, lang) if rows else t(lang, "owner_none", label=label)))
    log.info("owner pushed %s by %d minutes: %d booking(s)", stylist["name"], mins, len(moved))


async def summary_text(ctx, salon: dict, tz: ZoneInfo, now: datetime, lang: str = "en") -> str:
    today = now.date()
    start = datetime.combine(today, time(0), tz)
    money, count = await ctx.pool.fetchrow(
        "SELECT COALESCE(sum(amount_kes),0), count(*) FROM payments WHERE salon_id=$1::uuid AND status='paid' "
        "AND settled_at >= $2 AND settled_at < $3", salon["id"], start, start + timedelta(days=1))
    tomorrow = today + timedelta(days=1)
    rows = await _bookings(ctx, salon, tomorrow, tz)
    label = _label(tomorrow, today, lang)
    body = t(lang, "owner_day", label=label, lines=_lines(rows, tz, lang)) if rows else t(lang, "owner_none", label=label)
    return t(lang, "owner_summary", today=today.strftime("%a %d %b"), money=money, count=count, tomorrow=body)


async def daily_summaries(ctx) -> int:
    """Once per local day, after SUMMARY_HOUR, send each owner tomorrow's bookings and today's deposits.
    WhatsApp only delivers free text within 24 hours of the owner's last message, so a delivery failure is logged, not fatal."""
    sent = 0
    for salon in await ctx.pool.fetch(
            "SELECT id::text, name, wa_phone_number_id, timezone, owner_wa_id FROM salons WHERE active AND owner_wa_id IS NOT NULL"):
        salon = dict(salon)
        tz = ZoneInfo(salon["timezone"])
        now = datetime.now(tz)
        if now.hour < SUMMARY_HOUR:
            continue
        claimed = await ctx.pool.fetchval(
            "UPDATE salons SET last_summary_on=$2 WHERE id=$1::uuid AND (last_summary_on IS NULL OR last_summary_on < $2) RETURNING 1",
            salon["id"], now.date())
        if claimed:
            await _say(ctx, salon, salon["owner_wa_id"], await summary_text(ctx, salon, tz, now))
            sent += 1
    return sent
