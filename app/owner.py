"""The salon owner's side of WhatsApp. A small fixed vocabulary matched by code (no model): an owner is giving
orders that change real bookings, so the words are exact and the effect is predictable.

An owner message that is not a command falls through to the customer conversation, so the owner can also try
the booking flow from his own phone."""
from __future__ import annotations

import hashlib
import json
import logging
import re
import secrets
from datetime import date, datetime, time, timedelta
from zoneinfo import ZoneInfo

from .messages import t
from .timeparse import parse_day, parse_time
from .whatsapp import mask

log = logging.getLogger("rembo.owner")
SUMMARY_HOUR = 20   # local time after which the end-of-day summary goes out

HELP = re.compile(r"^\s*(help|menu|msaada|\?)\W*$", re.I)
LIST = re.compile(r"^\s*(today|tomorrow|leo|kesho|bookings?|schedule|ratiba|miadi)\b", re.I)
NOSHOW = re.compile(r"^\s*(noshow|no.show|no_show|missed|mhudhurio)\s+#?(\d{1,2})\W*$", re.I)
BLOCK = re.compile(r"^\s*(block|close|funga)\b(.*)$", re.I | re.S)
UNBLOCK = re.compile(r"^\s*(unblock|reopen|open|fungua)\b(.*)$", re.I | re.S)
CANCEL = re.compile(r"^\s*(cancel|ghairi)\s+#?(\d{1,2})\W*$", re.I)
LINK = re.compile(r"^\s*(link|web|dashboard|kiungo)\W*$", re.I)
SUMMARY = re.compile(r"^\s*(summary|muhtasari)\W*$", re.I)
SWAHILI = re.compile(r"\b(leo|kesho|ratiba|miadi|funga|fungua|ghairi|muhtasari|msaada|kiungo|jumatatu|jumanne|jumatano|alhamisi|ijumaa|jumamosi|jumapili)\b", re.I)
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
    await ctx.wa.send_text(salon["wa_phone_number_id"], ctx.settings.wa_access_token, to, body)


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
    today, lang = now.date(), "sw" if SWAHILI.search(text) else "en"
    say = lambda body: _say(ctx, salon, to, body)  # noqa: E731

    if HELP.match(text):
        await say(t(lang, "owner_help"))
        return True
    if LINK.match(text):
        base = ctx.settings.public_base_url.rstrip("/")
        if not base:
            await say(t(lang, "owner_no_web"))
        else:
            await say(t(lang, "owner_link", url=f"{base}/owner/login/{await create_link(ctx, salon['id'])}"))
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
        "SELECT b.id::text, b.starts_at, b.status, sv.name AS service, c.name AS client, c.wa_id AS phone, "
        "(SELECT sum(amount_kes) FROM payments p WHERE p.booking_id=b.id AND p.status='paid') AS paid "
        "FROM bookings b JOIN services sv ON sv.id=b.service_id JOIN clients c ON c.id=b.client_id "
        "WHERE b.salon_id=$1::uuid AND b.status IN ('confirmed','held') AND b.starts_at < $3 AND b.ends_at > $2 ORDER BY b.starts_at",
        salon["id"], start, end)
    msg = t(lang, "owner_blocked", what=what)
    if rows:
        rows = [dict(r) for r in rows]
        await _remember_list(ctx, salon, to, rows)
        msg += "\n" + t(lang, "owner_overlap", n=len(rows), lines=_lines(rows, tz, lang))
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
        await _say(ctx, salon, to, t(lang, "owner_bad_number", n=n))
        return
    await ctx.pool.execute("UPDATE reminder_jobs SET status='cancelled' WHERE booking_id=$1::uuid AND status='pending'", bid)
    when = row["starts_at"].astimezone(tz).strftime("%a %d %b, %H:%M")
    await _say(ctx, salon, to, t(lang, "owner_noshow", when=when))
    log.info("owner marked no-show %s", bid)

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


async def create_link(ctx, salon_id: str, minutes: int = 15) -> str:
    """A single-use sign-in token for the owner's web page or app. Only its hash is stored."""
    token = secrets.token_urlsafe(32)
    await ctx.pool.execute("INSERT INTO owner_links (token_hash, salon_id, expires_at) VALUES ($1,$2::uuid, now() + make_interval(mins => $3))",
                           hashlib.sha256(token.encode()).hexdigest(), salon_id, minutes)
    return token


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
