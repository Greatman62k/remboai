"""Chat onboarding for salon owners, handled by plain code, not the model.

A Telegram sender types /start register and the machine walks them through
name, prices, durations, and hours, then creates the salon and hands back an
invite link. Every decision (parsing, validation, limits) is deterministic so
it works with no model budget and behaves the same in tests.
"""
from __future__ import annotations

import difflib
import json
import logging
import re
from datetime import datetime, timedelta
from zoneinfo import ZoneInfo

from . import binding
from .db import clear_salon_cache
from .messages import has as has_text, t
from .smalltalk import FILLER_EXTRA as _FILLER_EXTRA
from .smalltalk import THANKS_FIRST_WORDS as _THANKS_FIRST_WORDS

log = logging.getLogger("rembo.onboard_chat")

SESSION_TTL = timedelta(hours=24)
MAX_SERVICES = 30
MIN_PRICE, MAX_PRICE = 50, 100000
MIN_NAME, MAX_NAME = 2, 60

CANCEL_WORDS = {"cancel", "ghairi", "stop", "acha"}
BACK_WORDS = {"back", "rudi"}
_NEVER_A_NAME = {"yes", "yeah", "yep", "no", "nope", "ok", "okay", "hapana"}
_GREETING_WORDS = {"hi", "hello", "hey", "habari", "hujambo", "jambo", "shikamoo", "mambo", "vipi", "sasa"}
YES_WORDS = {"yes", "yeah", "yep", "ndiyo", "ndio", "sawa", "ok", "okay"}
NO_WORDS = {"no", "nope", "hapana", "la"}
SW_HINTS = re.compile(r"\b(habari|niaje|mambo|nataka|tafadhali|asante|bei|sawa|ndiyo|hapana|ghairi|hamisha|sogeza)\b", re.I)

DURATION_HINTS = (
    ("braid", 180), ("dread", 180), ("locs", 180), ("weav", 150), ("wig", 120),
    ("nail", 60), ("mani", 60), ("pedi", 60), ("makeup", 90), ("facial", 60),
    ("massage", 60), ("fade", 30), ("cut", 30), ("shave", 20), ("line", 15),
    ("wash", 30), ("treatment", 45), ("color", 90), ("colour", 90), ("bleach", 90),
)

SERVICE_LINE = re.compile(
    r"^\s*(?P<name>.+?)\s*[:,]?\s+(?:(?:ksh|kes|/=)\s+)?(?P<price>\d[\d,]*)\s*"
    r"(?:(?:ksh|kes|/=)\s*)?"
    r"(?:(?P<dur>\d+)\s*(?P<unit>h(?:(?:ou)?rs?|r)?|m(?:in(?:ute)?s?)?)?)?\s*$", re.I)
TIME_PAIR = re.compile(
    r"(?P<h1>\d{1,2})(?::(?P<m1>\d{2}))?\s*(?P<ap1>[ap]\.?m\.?)?\s*[-\u2013to]+\s*"
    r"(?P<h2>\d{1,2})(?::(?P<m2>\d{2}))?\s*(?P<ap2>[ap]\.?m\.?)?", re.I)


def detect_lang(text: str) -> str:
    return "sw" if SW_HINTS.search(text or "") else "en"


def default_minutes(name: str) -> int:
    low = name.lower()
    for hint, mins in DURATION_HINTS:
        if hint in low:
            return mins
    return 45


def parse_price(value: str) -> int | None:
    try:
        price = int(value.replace(",", ""))
    except (TypeError, ValueError):
        return None
    return price if MIN_PRICE <= price <= MAX_PRICE else None


MAX_DEPOSIT = 100000
_NO_DEPOSIT = re.compile(r"\b(no|hapana|free|bure|none|bila|sifuna|zero|sifuri)\b", re.I)


def parse_deposit(words: str) -> int | None:
    """A typed deposit amount in KES, 0 for none, or None when the line is not usable.
    A denial next to a number ('no 200') contradicts itself, so it asks again instead of guessing."""
    text = (words or "").strip()
    if not text:
        return None
    m = re.search(r"\d[\d,]*", text)
    if m:
        if _NO_DEPOSIT.search(text):
            return None
        try:
            amount = int(m.group(0).replace(",", ""))
        except ValueError:
            return None
        return amount if 0 <= amount <= MAX_DEPOSIT else None
    return 0 if _NO_DEPOSIT.search(text) else None


def parse_service_line(line: str) -> dict | None:
    """One typed price line to a service dict, or None when the line is not usable."""
    m = SERVICE_LINE.match(line or "")
    if not m:
        return None
    name = " ".join(m.group("name").split())
    if not MIN_NAME <= len(name) <= MAX_NAME:
        return None
    price = parse_price(m.group("price"))
    if price is None:
        return None
    minutes, explicit = None, False
    if m.group("dur"):
        num = int(m.group("dur"))
        unit = (m.group("unit") or "m").lower()
        minutes = num * 60 if unit.startswith("h") else num
        if not 5 <= minutes <= 480:
            return None
        explicit = True
    return {"name": name, "price": price, "minutes": minutes or default_minutes(name), "explicit": explicit}


def parse_time_pair(text: str):
    """'09:00-18:00' to (open, close) time objects, or None. The whole line must be the pair: a partial
    match like '0900-1800' is rejected, never guessed at. A bare evening close ('9-5') rolls past noon."""
    m = TIME_PAIR.fullmatch((text or "").strip())
    if not m:
        return None
    from datetime import time as _time

    def fix(h: int, mi: int, ap: str | None) -> int:
        ap = (ap or "").lower().replace(".", "")
        if ap == "pm" and h < 12:
            return h + 12
        if ap == "am" and h == 12:
            return 0
        return h

    try:
        o = _time(fix(int(m.group("h1")), 0, m.group("ap1")), int(m.group("m1") or 0))
        c = _time(fix(int(m.group("h2")), 0, m.group("ap2")), int(m.group("m2") or 0))
    except ValueError:
        return None
    if m.group("ap1") is None and m.group("ap2") is None and c < o and c.hour + 12 <= 20:
        c = c.replace(hour=c.hour + 12)
    if o.hour < 5:
        return None
    return (o, c) if o < c else None


async def _session(pool, sender: str) -> dict | None:
    row = await pool.fetchrow("SELECT state FROM onboarding_sessions WHERE sender=$1 AND updated_at > now() - interval '24 hours'", sender)
    if row is None:
        return None
    state = row["state"]
    return state if isinstance(state, dict) else json.loads(state)


async def load_session(pool, sender: str) -> dict | None:
    """The live onboarding state for a sender, if they are mid-registration."""
    return await _session(pool, sender)


async def _save_session(pool, sender: str, state: dict) -> None:
    await pool.execute(
        "INSERT INTO onboarding_sessions (sender, state, updated_at) VALUES ($1,$2::jsonb,now()) "
        "ON CONFLICT (sender) DO UPDATE SET state=EXCLUDED.state, updated_at=now()", sender, json.dumps(state))


async def _clear_session(pool, sender: str) -> None:
    await pool.execute("DELETE FROM onboarding_sessions WHERE sender=$1", sender)


async def similar_salon(pool, name: str) -> str | None:
    """An existing salon name close enough that this may be a duplicate registration."""
    names = [r["name"] for r in await pool.fetch("SELECT name FROM salons WHERE active")]
    best, score = None, 0.0
    for other in names:
        s = difflib.SequenceMatcher(None, name.lower(), other.lower()).ratio()
        if s > score:
            best, score = other, s
    return best if score >= 0.8 else None


async def _say(ctx, pid: str, to: str, body: str) -> None:
    try:
        await ctx.wa.send_text(pid, "", to, body)
    except Exception:
        log.warning("onboarding send failed")


async def _say_buttons(ctx, pid: str, to: str, body: str, buttons: list[tuple[str, str]]) -> None:
    try:
        await ctx.wa.send_buttons(pid, "", to, body, buttons)
    except Exception:
        log.warning("onboarding send failed")


def _service_lines(services: list[dict], mark_guessed: bool = False) -> str:
    """One line per service. With mark_guessed, times the owner never gave are visibly marked (~)."""
    lines = []
    for s in services:
        mins = f"~{s['minutes']} min?" if mark_guessed and not s.get("explicit") else f"{s['minutes']} min"
        lines.append(f"- {s['name']}: {s['price']} ({mins})")
    return "\n".join(lines)


_FILLERS = _THANKS_FIRST_WORDS | _FILLER_EXTRA | {"thank you"}
_BOOKING_MARKERS = re.compile(r"\b(nataka|naomba|book|booking|bei|price|fungua|miadi|appointment)\b", re.I)
_EXPLAIN_MARKERS = re.compile(r"\b(sorry|pole|samahani|actually|kumbe|maana|yaani|oops|tengo|badili|rejea)\b", re.I)


def looks_off_topic(text: str) -> bool:
    """True when a message at a name step is chatter, a question, or an explanation, not the name itself."""
    words = (text or "").strip()
    if not words:
        return True
    low = words.lower()
    if low in _FILLERS or words.endswith("?") or len(words) > 60:
        return True
    return bool(_BOOKING_MARKERS.search(words) or _EXPLAIN_MARKERS.search(words))


EXAMPLES = {
    "kinyozi": {"prices": "Fade 500 30\nClean shave 300 20", "duration": "Fade 30", "fix": "Fade 600", "bad": "Fade 500 30"},
    "salon": {"prices": "Braids 1500 3h\nSilk press 2500 90", "duration": "Braids 180", "fix": "Braids 1600", "bad": "Braids 1500 3h"},
}

KINYOZI_WORDS = {"1", "barbershop", "barber", "kinyozi"}
SALON_WORDS = {"2", "salon", "saloon", "saluni", "beauty"}

MAX_OWNERS = 10


def examples(kind: str | None) -> dict:
    return EXAMPLES.get(kind) or EXAMPLES["salon"]


def parse_owners(words: str) -> list[str] | None:
    """One stylist name, or many separated by commas or new lines. None when no usable name."""
    parts = [" ".join(p.split()) for p in re.split(r"[\n,;]+", words or "") if p.strip()]
    if not parts or len(parts) > MAX_OWNERS:
        return None
    seen, out = set(), []
    for p in parts:
        if not MIN_NAME <= len(p) <= MAX_NAME:
            return None
        if p.lower() not in seen:
            seen.add(p.lower())
            out.append(p)
    return out or None


_CHANGE = re.compile(r"\b(change|changing|fix|correct|edit|update|wrong|mistake|badili|badilisha|sahihisha|rekebisha|kosea)\b", re.I)
_REDIRECTS = (
    ("ask_prices", re.compile(r"\b(price|prices|bei|gharama|service|services|huduma)\b", re.I)),
    ("ask_hours", re.compile(r"\b(hours|hour|open|opening|fungua|kufungua)\b", re.I)),
    ("ask_deposit", re.compile(r"\b(deposit|deposits|amana)\b", re.I)),
    ("ask_durations", re.compile(r"\b(time|times|duration|durations|muda|dakika)\b", re.I)),
    ("ask_type", re.compile(r"\b(type|kinyozi|barbershop|barber|salon|saluni)\b", re.I)),
    ("ask_name", re.compile(r"\b(name|salon|saluni|jina)\b", re.I)),
    ("ask_owner", re.compile(r"\b(owner|stylist|stylists|mhudumu|wahudumu)\b", re.I)),
)


_REGISTER_VERB = re.compile(r"\b(register|registration|sajili|kusajili|anzisha|kuanzisha|fungua|kufungua|open|opening|add|ongeza|create|tengeneza|andikisha|set\s?up|sign\s?up|join|jiunge|kujiunga)\b", re.I)
_REGISTER_NOUN = re.compile(r"\b(business|biashara|salon|saloon|saluni|kinyozi|barbershop|barber|shop|duka|company|kampuni)\b", re.I)


def wants_register(text: str) -> bool:
    """A plain sentence asking to register a business, in English or Swahili."""
    t = text or ""
    return bool(_REGISTER_VERB.search(t) and _REGISTER_NOUN.search(t))


def redirect_step(words: str) -> str | None:
    """A plain-language change request ('i want to change time') names its step, else None."""
    if not _CHANGE.search(words or ""):
        return None
    for step, pat in _REDIRECTS:
        if pat.search(words):
            return step
    return None


_KEEP_FULL = re.compile(r"\b(they are|they're|it is|it's|that is|that's|all|everything)\b.*\b(ok|okay|fine|good|correct|right|sawa|poa)\b", re.I)
_KEEP_WORDS = re.compile(r"\b(keep|same|no change|unchanged|leave|maintain|as before|bado|bila mabadiliko|wachana)\b", re.I)
_KEEP_AFFIRM = {"ok", "okay", "sawa", "ndiyo", "ndio", "yes", "yeah", "yep"}


def wants_keep(words: str, redirected: bool) -> bool:
    """'they are ok' / 'keep them' keeps the earlier answer. A bare 'ok' only counts
    when the owner was sent back to fix something (otherwise it is steered)."""
    text = (words or "").strip()
    if _KEEP_FULL.search(text) or _KEEP_WORDS.search(text):
        return True
    return redirected and text.lower() in _KEEP_AFFIRM


def _keep_data_present(state: dict) -> bool:
    """Whether the current step already has an answer worth keeping."""
    data = state.get("data", {})
    step = state.get("step", "ask_name")
    if step == "ask_name":
        return bool(data.get("salon"))
    if step == "ask_type":
        return bool(data.get("kind"))
    if step == "ask_owner":
        return bool(data.get("owners"))
    if step in ("ask_prices", "ask_durations", "confirm_services"):
        return bool(data.get("services"))
    if step == "ask_hours":
        return bool(data.get("open") and data.get("close"))
    if step == "ask_deposit":
        return "deposit" in data
    return False


def find_service(services: list[dict], line: str) -> dict | None:
    """An already-listed service this line refers to: exact name first, then a name inside the line."""
    low = (line or "").lower()
    for s in services:
        if s["name"].lower() == low:
            return s
    return next((s for s in services if s["name"].lower() in low), None)


DURATION_ONLY = re.compile(r"^\s*(\d+)\s*(h(?:(?:ou)?rs?|r)?|m(?:in(?:ute)?s?)?)?\s*$", re.I)
DURATION_NAMED = re.compile(r"^\s*(.+?)\s+(\d+)\s*(h(?:(?:ou)?rs?|r)?|m(?:in(?:ute)?s?)?)?\s*$", re.I)


def apply_durations(services: list[dict], words: str) -> tuple[int, list[str]]:
    """Fold typed times into the service list. A bare time (just '45' or '2h')
    belongs to the only service; otherwise each line names its service.
    Returns (fixed, bad lines)."""
    fixed, bad = 0, []
    lines = [ln for ln in (words or "").splitlines() if ln.strip()]
    if len(lines) == 1 and len(services) == 1:
        m = DURATION_ONLY.match(lines[0])
        if m:
            mins = int(m.group(1)) * (60 if (m.group(2) or "").lower().startswith("h") else 1)
            if 5 <= mins <= 480:
                services[0]["minutes"] = mins
                return 1, []
            return 0, [lines[0].strip()]
    for ln in lines:
        m = DURATION_NAMED.match(ln)
        target = find_service(services, ln)
        mins = int(m.group(2)) * (60 if (m.group(3) or "").lower().startswith("h") else 1) if m else 0
        if m and target and 5 <= mins <= 480:
            target["minutes"] = mins
            fixed += 1
        else:
            bad.append(ln.strip())
    return fixed, bad


def apply_corrections(data: dict, words: str) -> tuple[list[str], list[dict], list[str]]:
    """Fold typed lines into the service list: lines naming a listed service update it
    (a bare price keeps the old time), new names are added, the rest is reported back."""
    updated, added, bad = [], [], []
    for ln in (words or "").splitlines():
        if not ln.strip():
            continue
        p = parse_service_line(ln)
        if p is None:
            bad.append(ln.strip())
            continue
        target = find_service(data.get("services", []), p["name"])
        if target is None:
            added.append(p)
        else:
            target["price"] = p["price"]
            if p["explicit"]:
                target["minutes"] = p["minutes"]
            updated.append(f"{target['name']}: {target['price']} ({target['minutes']} min)")
    data.setdefault("services", []).extend(added)
    return updated, added, bad


async def _go_live(ctx, pid: str, sender: str, state: dict) -> None:
    """Create the salon, its stylists, and its services, then hand back the invite link."""
    from .channels import invites
    from .availability import DAY_KEYS
    data, lang = state["data"], state.get("lang", "en")
    wa_pid = None if pid.startswith("tg:") else pid
    tg_id = pid if pid.startswith("tg:") else None
    async with ctx.pool.acquire() as con, con.transaction():
        salon = await con.fetchval(
            "INSERT INTO salons (name, owner_wa_id, timezone, deposit_kes, open_hours, active, wa_phone_number_id, tg_bot_id) "
            "VALUES ($1,$2,'Africa/Nairobi',$3,$4,true,$5,$6) RETURNING id::text",
            data["salon"], sender, int(data.get("deposit", 0)), json.dumps({d: [data["open"], data["close"]] for d in DAY_KEYS}), wa_pid, tg_id)
        staff_ids = []
        for own in data.get("owners") or [data.get("owner", "Owner")]:
            staff_ids.append(await con.fetchval(
                "INSERT INTO staff (salon_id, name, active) VALUES ($1::uuid,$2,true) RETURNING id::text", salon, own))
        for s in data["services"]:
            sid = await con.fetchval(
                "INSERT INTO services (salon_id, name, price_kes, duration_min, active) VALUES ($1::uuid,$2,$3,$4,true) RETURNING id::text",
                salon, s["name"], s["price"], s["minutes"])
            for staff in staff_ids:
                await con.execute("INSERT INTO staff_services (staff_id, service_id) VALUES ($1::uuid,$2::uuid)", staff, sid)
    slug = await binding.ensure_slug(ctx.pool, salon, data["salon"])
    clear_salon_cache()
    from .channels.telegram import bot_username
    username = await bot_username(getattr(ctx, "wa", None))
    channel = invites.channel_of(sender)
    number = getattr(ctx.settings, "central_whatsapp_number", "") if channel == "wa" else ""
    link = invites.invite_for(channel, slug, username=username, number=number)
    await _clear_session(ctx.pool, sender)
    await _say(ctx, pid, sender, t(lang, "ob_live", salon=data["salon"], link=link))


def _question_for(state: dict) -> str:
    """The prompt that belongs to the state's step, rebuilt from what is known so far."""
    lang, data = state.get("lang", "en"), state.get("data", {})
    ex = examples(data.get("kind"))
    step = state.get("step", "ask_name")
    if step == "confirm_name":
        return t(lang, "ob_name_twin", name=state.get("twin", ""))
    if step == "ask_type":
        return t(lang, "ob_ask_type", salon=data.get("salon", ""))
    if step == "ask_owner":
        return t(lang, "ob_ask_owner")
    if step == "ask_prices":
        return t(lang, "ob_ask_prices", example=ex["prices"])
    if step == "confirm_services":
        return t(lang, "ob_confirm_services", lines=_service_lines(data.get("services", []), mark_guessed=True), fix=ex["fix"])
    if step == "ask_durations":
        lines = "\n".join(f"- {s['name']}: {s['minutes']} min" for s in data.get("services", []))
        return t(lang, "ob_ask_durations", example=ex["duration"], lines=lines)
    if step == "ask_hours":
        return t(lang, "ob_ask_hours")
    if step == "ask_deposit":
        return t(lang, "ob_ask_deposit")
    if step == "confirm_all":
        owners = ", ".join(data.get("owners", []))
        return t(lang, "ob_confirm_all", salon=data.get("salon", ""), owners=owners,
                 lines=_service_lines(data.get("services", [])), fix=ex["fix"],
                 hours=f"{data.get('open', '')}-{data.get('close', '')}", deposit=data.get("deposit", 0))
    return t(lang, "ob_ask_name")


async def _resend(ctx, pid: str, sender: str, state: dict, prefix: str | None = None) -> None:
    body = _question_for(state)
    if prefix:
        body = prefix + "\n\n" + body
    await _say(ctx, pid, sender, body)


async def _maybe_return(ctx, pid: str, sender: str, state: dict) -> bool:
    """After a redirected fix, jump back to the summary it came from instead of walking every step again."""
    dest = state.get("return_to")
    if not dest or dest == state.get("step"):
        return False
    data = state.get("data", {})
    if not data.get("services"):
        return False
    if dest == "confirm_all" and not (data.get("open") and data.get("close") and data.get("owners")):
        return False
    state["return_to"] = None
    state["stack"] = [state.get("step", "ask_hours")]
    state["step"] = dest
    await _save_session(ctx.pool, sender, state)
    await _resend(ctx, pid, sender, state)
    return True


async def _redirect(ctx, pid: str, sender: str, state: dict, words: str) -> bool:
    """A plain-language change request ('i want to change time') jumps to its step and remembers the way back."""
    target = redirect_step(words)
    if target is None:
        return False
    state["return_to"] = state.get("step")
    state["stack"] = [state.get("step")]
    state["step"] = target
    await _save_session(ctx.pool, sender, state)
    await _resend(ctx, pid, sender, state)
    return True


async def advance(ctx, sender: str, pid: str, text: str, media: dict | None = None) -> bool:
    """One owner message through the machine. True means the message was consumed here."""
    state = await _session(ctx.pool, sender)
    if state is None:
        return False
    lang, data = state.get("lang", "en"), state.setdefault("data", {})
    words = (text or "").strip()
    low = words.lower()
    if low in CANCEL_WORDS:
        await _clear_session(ctx.pool, sender)
        await _say(ctx, pid, sender, t(lang, "ob_cancelled"))
        return True
    if low in BACK_WORDS:
        state["return_to"] = None
        if state.get("stack"):
            state["step"] = state["stack"].pop()
            await _save_session(ctx.pool, sender, state)
        await _resend(ctx, pid, sender, state)
        return True
    if wants_keep(words, bool(state.get("return_to"))):
        if _keep_data_present(state) and await _maybe_return(ctx, pid, sender, state):
            return True
        await _resend(ctx, pid, sender, state, t(lang, "ob_steer"))
        return True
    step = state.get("step", "ask_name")

    if step == "ask_name":
        if low in _NEVER_A_NAME or looks_off_topic(words):
            await _resend(ctx, pid, sender, state, t(lang, "ob_steer"))
            return True
        name = " ".join(words.split())
        if not MIN_NAME <= len(name) <= MAX_NAME:
            await _say(ctx, pid, sender, t(lang, "ob_name_bad"))
            return True
        data["salon"] = name
        twin = await similar_salon(ctx.pool, name)
        state["stack"] = ["ask_name"]
        if twin:
            state["step"], state["twin"] = "confirm_name", twin
            await _save_session(ctx.pool, sender, state)
            await _say(ctx, pid, sender, t(lang, "ob_name_twin", name=twin))
            return True
        state["stack"] = ["ask_name"]
        state["step"] = "ask_type"
        await _save_session(ctx.pool, sender, state)
        if await _maybe_return(ctx, pid, sender, state):
            return True
        await _say(ctx, pid, sender, t(lang, "ob_ask_type", salon=name))
        return True

    if step == "confirm_name":
        if low in YES_WORDS:
            state["step"] = "ask_type"
            await _save_session(ctx.pool, sender, state)
            if await _maybe_return(ctx, pid, sender, state):
                return True
            await _say(ctx, pid, sender, t(lang, "ob_ask_type", salon=data["salon"]))
        elif low in NO_WORDS:
            state["return_to"] = None
            state["step"] = "ask_name"
            await _save_session(ctx.pool, sender, state)
            await _say(ctx, pid, sender, t(lang, "ob_ask_name"))
        else:
            await _say(ctx, pid, sender, t(lang, "ob_yes_no"))
        return True

    if step == "ask_type":
        if low in KINYOZI_WORDS:
            data["kind"] = "kinyozi"
        elif low in SALON_WORDS:
            data["kind"] = "salon"
        else:
            await _say(ctx, pid, sender, t(lang, "ob_type_bad"))
            return True
        state["stack"] = ["ask_type"]
        state["step"] = "ask_owner"
        await _save_session(ctx.pool, sender, state)
        await _say(ctx, pid, sender, t(lang, "ob_ask_owner"))
        return True

    if step == "ask_owner":
        if low in _NEVER_A_NAME or low in _GREETING_WORDS or looks_off_topic(words):
            await _resend(ctx, pid, sender, state, t(lang, "ob_steer"))
            return True
        owners = parse_owners(words)
        if owners is None:
            await _say(ctx, pid, sender, t(lang, "ob_owners_bad"))
            return True
        data["owners"] = owners
        state["stack"] = ["ask_owner"]
        state["step"] = "ask_prices"
        data["services"] = []
        await _save_session(ctx.pool, sender, state)
        if await _maybe_return(ctx, pid, sender, state):
            return True
        await _say(ctx, pid, sender, t(lang, "ob_ask_prices", example=examples(data.get("kind"))["prices"]))
        return True

    if step == "ask_prices":
        ex = examples(data.get("kind"))
        if media and media.get("kind") == "image":
            await _say(ctx, pid, sender, t(lang, "ob_photo_type", example=ex["bad"]))
            return True
        bad: list[str] = []
        if low not in YES_WORDS or not data.get("services"):
            updated, added, bad = apply_corrections(data, words)
            if not updated and not added:
                await _say(ctx, pid, sender, t(lang, "ob_prices_bad", example=ex["bad"]))
                return True
            if len(data["services"]) > MAX_SERVICES:
                data["services"] = data["services"][:MAX_SERVICES]
                await _say(ctx, pid, sender, t(lang, "ob_too_many", n=MAX_SERVICES))
        state["stack"] = ["ask_prices"]
        state["step"] = "confirm_services"
        await _save_session(ctx.pool, sender, state)
        body = t(lang, "ob_confirm_services", lines=_service_lines(data["services"], mark_guessed=True), fix=ex["fix"])
        if bad:
            body += "\n" + t(lang, "ob_skipped", lines="\n".join(f"- {b}" for b in bad[:5]))
        await _say(ctx, pid, sender, body)
        return True

    if step == "confirm_services":
        ex = examples(data.get("kind"))
        if low in YES_WORDS:
            if await _maybe_return(ctx, pid, sender, state):
                return True
            state["stack"] = ["confirm_services"]
            if all(s.get("explicit") for s in data["services"]):
                state["step"] = "ask_hours"
            else:
                state["step"] = "ask_durations"
            await _save_session(ctx.pool, sender, state)
            await _resend(ctx, pid, sender, state)
        elif low in NO_WORDS:
            state["step"] = "ask_prices"
            data["services"] = []
            await _save_session(ctx.pool, sender, state)
            await _say(ctx, pid, sender, t(lang, "ob_ask_prices", example=ex["prices"]))
        else:
            if await _redirect(ctx, pid, sender, state, words):
                return True
            updated, added, bad = apply_corrections(data, words)
            if not updated and not added:
                await _say(ctx, pid, sender, t(lang, "ob_yes_no_add"))
                return True
            if len(data["services"]) > MAX_SERVICES:
                data["services"] = data["services"][:MAX_SERVICES]
                await _say(ctx, pid, sender, t(lang, "ob_too_many", n=MAX_SERVICES))
            await _save_session(ctx.pool, sender, state)
            body = ""
            if updated:
                body += t(lang, "ob_updated", what="; ".join(updated)) + "\n\n"
            body += t(lang, "ob_confirm_services", lines=_service_lines(data["services"], mark_guessed=True), fix=ex["fix"])
            if bad:
                body += "\n" + t(lang, "ob_skipped", lines="\n".join(f"- {b}" for b in bad[:5]))
            await _say(ctx, pid, sender, body)
        return True

    if step == "ask_durations":
        if low in YES_WORDS:
            if await _maybe_return(ctx, pid, sender, state):
                return True
            state["stack"] = ["ask_durations"]
            state["step"] = "ask_hours"
            await _save_session(ctx.pool, sender, state)
            await _say(ctx, pid, sender, t(lang, "ob_ask_hours"))
            return True
        fixed, bad = apply_durations(data["services"], words)
        if not fixed:
            ex = examples(data.get("kind"))
            await _say(ctx, pid, sender, t(lang, "ob_durations_bad", example=ex["duration"]))
            return True
        if await _maybe_return(ctx, pid, sender, state):
            return True
        state["stack"] = ["ask_durations"]
        state["step"] = "ask_hours"
        await _save_session(ctx.pool, sender, state)
        body = t(lang, "ob_ask_hours")
        if bad:
            body += "\n\n" + t(lang, "ob_skipped", lines="\n".join(f"- {b}" for b in bad[:5]))
        await _say(ctx, pid, sender, body)
        return True

    if step == "ask_hours":
        pair = parse_time_pair(words)
        if pair is None:
            await _say(ctx, pid, sender, t(lang, "ob_hours_bad"))
            return True
        data["open"], data["close"] = pair[0].strftime("%H:%M"), pair[1].strftime("%H:%M")
        state["stack"] = ["ask_hours"]
        state["step"] = "ask_deposit"
        await _save_session(ctx.pool, sender, state)
        if await _maybe_return(ctx, pid, sender, state):
            return True
        await _say(ctx, pid, sender, t(lang, "ob_ask_deposit"))
        return True

    if step == "ask_deposit":
        amount = parse_deposit(words)
        if amount is None:
            await _say(ctx, pid, sender, t(lang, "ob_deposit_bad"))
            return True
        data["deposit"] = amount
        state["stack"] = ["ask_deposit"]
        state["step"] = "confirm_all"
        await _save_session(ctx.pool, sender, state)
        if await _maybe_return(ctx, pid, sender, state):
            return True
        await _resend(ctx, pid, sender, state)
        return True

    if step == "confirm_all":
        ex = examples(data.get("kind"))
        if low in YES_WORDS:
            await _go_live(ctx, pid, sender, state)
        elif low in NO_WORDS:
            state["return_to"] = None
            state["step"] = "ask_name"
            state["data"] = {}
            await _save_session(ctx.pool, sender, state)
            await _say(ctx, pid, sender, t(lang, "ob_ask_name"))
        else:
            if await _redirect(ctx, pid, sender, state, words):
                return True
            updated, added, bad = apply_corrections(data, words)
            if not updated and not added:
                await _say(ctx, pid, sender, t(lang, "ob_confirm_fix_bad", fix=ex["fix"]))
                return True
            if len(data["services"]) > MAX_SERVICES:
                data["services"] = data["services"][:MAX_SERVICES]
            await _save_session(ctx.pool, sender, state)
            ack = t(lang, "ob_updated", what="; ".join(updated)) if updated else None
            await _resend(ctx, pid, sender, state, ack)
        return True

    await _clear_session(ctx.pool, sender)
    return False


async def begin(ctx, sender: str, pid: str, text: str) -> bool:
    """Start onboarding for a /start register message. False when registration is not allowed."""
    if not await binding.registration_allowed(ctx.pool, sender):
        await _say(ctx, pid, sender, t(detect_lang(text), "ob_limited"))
        return True
    await _save_session(ctx.pool, sender, {"step": "ask_name", "data": {}, "lang": detect_lang(text), "stack": []})
    await _say(ctx, pid, sender, t(detect_lang(text), "ob_ask_name"))
    return True
