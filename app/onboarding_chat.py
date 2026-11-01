"""Chat onboarding for salon owners.

A model may make the conversation natural and extract values, while code
validates the data, enforces limits, and controls salon creation and invites.
The deterministic flow remains available whenever the agent is disabled or
unavailable.
"""
from __future__ import annotations

import difflib
import json
import logging
import re
from datetime import datetime, timedelta
from zoneinfo import ZoneInfo

from . import binding, guard, smalltalk
from .channels import outbound
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
_CANCEL_REGISTRATION = re.compile(
    r"^(?:i\s+(?:do\s+not|don't|dont)\s+want\s+to\s+register|i\s+don't\s+want\s+to\s+sign\s+up|"
    r"not\s+interested\s+in\s+registering|stop\s+(?:the\s+)?registration|cancel\s+(?:the\s+)?registration|"
    r"sitaki(?:\s+(?:kusajili|kujisajili|kuendelea))?|acha\s+kusajili|sahau\s+(?:hayo|hilo|hizi)|"
    r"forget\s+(?:it|that|this)|never\s+mind(?:\s+registration)?)\s*[.!?]*$", re.I)
_CUSTOMER_BOOKING_REQUEST = re.compile(
    r"^\s*(?:i\s+(?:want|wanted)\s+(?:(?:to\s+)?(?:get|have)\s+)?(?:a\s+)?(?:haircut|shave|cut|fade|trim)|"
    r"i\s+was\s+trying\s+to\s+get\s+(?:a\s+)?(?:haircut|shave|cut|fade|trim)|"
    r"book\s+me(?:\s+for)?\s+(?:a\s+)?(?:haircut|shave|cut|fade|trim)|"
    r"nilikuwa\s+nataka\s+.{0,40}\b(?:kunyolewa|kunyoa|kukata\s+nywele|nywele|fade)\b|"
    r"nataka\s+.{0,30}\b(?:kunyolewa|kunyoa\s+nywele|kukata\s+nywele)\b)\s*[.!?]*$", re.I)
BACK_WORDS = {"back", "rudi"}
_NEVER_A_NAME = {"yes", "yeah", "yep", "yup", "no", "nope", "ok", "okay", "hapana"}
_GREETING_WORDS = {"hi", "hello", "hey", "habari", "hujambo", "jambo", "shikamoo", "mambo", "vipi", "sasa"}
YES_WORDS = {"yes", "yeah", "yep", "yup", "ndiyo", "ndio", "sawa", "ok", "okay"}
NO_WORDS = {"no", "nope", "hapana", "la"}
_AFFIRM_SENTENCE = re.compile(
    r"^(?:(?:yes|yeah|yep|yup|ndiyo|ndio|sawa|ok|okay)(?:[,! .]+(?:everything|all|that|those|the details|the services|the prices|the times)?(?:\s+(?:is|are|that's|it's|looks|look|seems|seem))?\s*(?:correct|right|good|fine|okay|ok|sawa|poa|mine|different))?|"
    r"(?:everything|all|that|those|the details|the services|the prices|the times)\s+(?:(?:is|are|looks|look|seems|seem)\s+)?(?:correct|right|good|fine|okay|ok|sawa|poa)|"
    r"(?:it\s+)?(?:looks|sounds|seems)\s+(?:correct|right|good|fine|okay|ok))$", re.I)
_AFFIRM_ACTION = re.compile(r"^(?:yes|yeah|yep|yup|ndiyo|ndio)[,! ]+(?:please\s+)?(?:go ahead|do it|proceed|create it|set it up)$", re.I)
_NEGATIVE_SENTENCE = re.compile(r"^(?:no|nope|hapana|la)(?:\W+(?:that is wrong|that's wrong|not correct|not right|si sawa|sio sawa|badilisha|change it))?$", re.I)
SW_HINTS = re.compile(r"\b(habari|niaje|mambo|nataka|tafadhali|asante|bei|sawa|ndiyo|hapana|ghairi|hamisha|sogeza|sajili|kusajili|biashara|saluni|huduma|wahudumu|jina|muda|amana|kinyozi)\b", re.I)

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


_TIME_TOKEN = re.compile(r"(?<![\w])(?:[01]?\d|2[0-3])(?::[0-5]\d)?\s*(?:[ap]\.?m\.?)?(?![\w])", re.I)


def detect_lang(text: str) -> str:
    return "sw" if SW_HINTS.search(text or "") else "en"


def is_affirmative(text: str) -> bool:
    """A clear yes suitable for a registration confirmation."""
    words = " ".join((text or "").strip().lower().split()).strip(" .,!?")
    return words in YES_WORDS or bool(_AFFIRM_SENTENCE.fullmatch(words) or _AFFIRM_ACTION.fullmatch(words))


def is_negative(text: str) -> bool:
    words = " ".join((text or "").strip().lower().split()).strip(" .!?")
    return words in NO_WORDS or bool(_NEGATIVE_SENTENCE.fullmatch(words))


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
    """Parse a stated opening-hours pair. Four-digit '0900-1800' is read as 09:00-18:00;
    three-digit '800-1900' is rejected, never guessed at. A bare evening close ('9-5') rolls past noon."""
    raw = (text or "").strip()
    mil = re.fullmatch(r"(\d{2})(\d{2})\s*(?:-|–|to)\s*(\d{2})(\d{2})", raw, re.I)
    if mil and all(int(mil.group(i)) <= 23 for i in (1, 3)) and all(int(mil.group(i)) <= 59 for i in (2, 4)):
        raw = f"{mil.group(1)}:{mil.group(2)}-{mil.group(3)}:{mil.group(4)}"
    m = TIME_PAIR.fullmatch(raw)
    if not m:
        matches = list(_TIME_TOKEN.finditer(raw))
        if len(matches) == 2:
            first, second = matches
            prefix = raw[:first.start()].strip().casefold()
            between = raw[first.end():second.start()].strip().casefold()
            suffix = raw[second.end():].strip().casefold().strip(".,!?")
            allowed_prefix = {"we", "we're", "we're", "we are", "i", "i'm", "i am", "our", "the",
                              "open", "opening", "hour", "hours", "are", "is", "from", "at", "between"}
            prefix_words = prefix.replace("we're", "we").replace("i'm", "i").split()
            allowed_between = {"to", "until", "till", "through", "and", "we", "we're", "close", "closes",
                               "closing", "at", "are", "is"}
            between_words = between.replace("-", " ").replace("–", " ").replace(",", " ").split()
            allowed_suffix = {"daily", "every", "day"}
            prefix_ok = not prefix_words or all(word in allowed_prefix for word in prefix_words)
            explicit_clock = ":" in first.group() or ":" in second.group() or bool(re.search(r"[ap]\.?m", first.group() + second.group(), re.I))
            between_ok = (bool(between_words) or (between in ("-", "–") and explicit_clock)) and all(word in allowed_between for word in between_words)
            suffix_ok = not suffix or all(word in allowed_suffix for word in suffix.split())
            if prefix_ok and between_ok and suffix_ok:
                raw = f"{first.group().strip()}-{second.group().strip()}"
                m = TIME_PAIR.fullmatch(raw)
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


async def clear_session(pool, sender: str) -> None:
    """End a registration draft when the owner explicitly cancels or switches to booking."""
    await _clear_session(pool, sender)


def wants_cancel_registration(text: str) -> bool:
    low = " ".join(guard.clean(text or "").casefold().split()).strip(" .,!?")
    return low in CANCEL_WORDS or bool(_CANCEL_REGISTRATION.fullmatch(low))


def wants_customer_booking(text: str) -> bool:
    return bool(_CUSTOMER_BOOKING_REQUEST.fullmatch(guard.clean(text or "")))


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
        await outbound.send_unbound_text(ctx, pid, to, body)
    except Exception:
        log.warning("onboarding send failed")


async def _say_buttons(ctx, pid: str, to: str, body: str, buttons: list[tuple[str, str]]) -> None:
    try:
        await outbound.send_unbound_buttons(ctx, pid, to, body, buttons)
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
    words = guard.clean(text)
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


def registration_format() -> dict[str, str]:
    """User-facing placeholders; parser examples stay separate so no fictional menu looks real."""
    return {
        "prices": "[service name] | [your price in KES] | [duration]",
        "duration": "[service name] | [duration]",
        "fix": "[service name] | [updated price] | [new duration, if changing it]",
    }


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
                services[0]["explicit"] = True
                return 1, []
            return 0, [lines[0].strip()]
    for ln in lines:
        m = DURATION_NAMED.match(ln)
        target = find_service(services, ln)
        mins = int(m.group(2)) * (60 if (m.group(3) or "").lower().startswith("h") else 1) if m else 0
        if m and target and 5 <= mins <= 480:
            target["minutes"] = mins
            target["explicit"] = True
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
        target = next((s for s in data.get("services", []) if s["name"].casefold() == p["name"].casefold()), None)
        if target is None:
            added.append(p)
        else:
            target["price"] = p["price"]
            if p["explicit"]:
                target["minutes"] = p["minutes"]
            updated.append(f"{target['name']}: {target['price']} ({target['minutes']} min)")
    data.setdefault("services", []).extend(added)
    return updated, added, bad


def registration_complete(state: dict) -> bool:
    """Validate the full persisted draft before any salon row is created."""
    data = state.get("data") or {}
    if state.get("twin"):
        return False
    salon = data.get("salon")
    if not isinstance(salon, str) or not MIN_NAME <= len(salon.strip()) <= MAX_NAME:
        return False
    if data.get("kind") not in ("salon", "kinyozi"):
        return False
    owners = data.get("owners")
    if not isinstance(owners, list) or not 1 <= len(owners) <= MAX_OWNERS or not all(isinstance(name, str) and parse_owners(name) for name in owners):
        return False
    services = data.get("services")
    if not isinstance(services, list) or not 1 <= len(services) <= MAX_SERVICES:
        return False
    seen = set()
    for service in services:
        if not isinstance(service, dict) or not isinstance(service.get("name"), str):
            return False
        name = service["name"].strip()
        if not MIN_NAME <= len(name) <= MAX_NAME or name.casefold() in seen:
            return False
        seen.add(name.casefold())
        price, minutes = service.get("price"), service.get("minutes")
        if not isinstance(price, int) or isinstance(price, bool) or not MIN_PRICE <= price <= MAX_PRICE:
            return False
        if not isinstance(minutes, int) or isinstance(minutes, bool) or not 5 <= minutes <= 480:
            return False
    if not all(service.get("explicit") for service in services):
        # Older sessions reached these steps only after the owner accepted the estimates.
        if not state.get("duration_defaults_accepted"):
            if "duration_defaults_accepted" in state or state.get("step") not in ("ask_hours", "ask_deposit", "confirm_all"):
                return False
    if not state.get("services_confirmed") and state.get("step") not in ("ask_durations", "ask_hours", "ask_deposit", "confirm_all"):
        return False
    if not data.get("open") or not data.get("close") or parse_time_pair(f"{data['open']}-{data['close']}") is None:
        return False
    deposit = data.get("deposit")
    return isinstance(deposit, int) and not isinstance(deposit, bool) and 0 <= deposit <= MAX_DEPOSIT


def registration_next_step(state: dict) -> str:
    """Return the first incomplete or unconfirmed registration stage."""
    data = state.get("data") or {}
    salon = data.get("salon")
    if state.get("step") == "confirm_name" and state.get("twin"):
        return "confirm_name"
    if not isinstance(salon, str) or not MIN_NAME <= len(salon.strip()) <= MAX_NAME:
        return "ask_name"
    if data.get("kind") not in ("salon", "kinyozi"):
        return "ask_type"
    owners = data.get("owners")
    if not isinstance(owners, list) or not owners or not all(isinstance(name, str) and parse_owners(name) for name in owners):
        return "ask_owner"
    services = data.get("services")
    if not isinstance(services, list) or not services or len(services) > MAX_SERVICES:
        return "ask_prices"
    service_names = set()
    for service in services:
        if not isinstance(service, dict) or not isinstance(service.get("name"), str):
            return "ask_prices"
        name = service["name"].strip()
        price, minutes = service.get("price"), service.get("minutes")
        if (not MIN_NAME <= len(name) <= MAX_NAME or name.casefold() in service_names
                or not isinstance(price, int) or isinstance(price, bool) or not MIN_PRICE <= price <= MAX_PRICE
                or not isinstance(minutes, int) or isinstance(minutes, bool) or not 5 <= minutes <= 480):
            return "ask_prices"
        service_names.add(name.casefold())
    if not state.get("services_confirmed"):
        return "confirm_services"
    if not all(isinstance(s, dict) and s.get("explicit") for s in services) and not state.get("duration_defaults_accepted"):
        return "ask_durations"
    if not data.get("open") or not data.get("close") or parse_time_pair(f"{data.get('open', '')}-{data.get('close', '')}") is None:
        return "ask_hours"
    deposit = data.get("deposit")
    if not isinstance(deposit, int) or isinstance(deposit, bool) or not 0 <= deposit <= MAX_DEPOSIT:
        return "ask_deposit"
    return "confirm_all"


async def _go_live(ctx, pid: str, sender: str, state: dict) -> bool:
    """Create the salon, its stylists, and its services, then hand back the invite link."""
    if not registration_complete(state):
        log.error("refusing to create an incomplete salon registration for %s", sender[-4:])
        state["step"] = registration_next_step(state)
        await _save_session(ctx.pool, sender, state)
        await _resend(ctx, pid, sender, state, t(state.get("lang", "en"), "ob_steer"))
        return False
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
    await outbound.set_conversation_salon(ctx, salon)
    slug = await binding.ensure_slug(ctx.pool, salon, data["salon"])
    clear_salon_cache()
    from .channels.telegram import bot_username
    username = await bot_username(getattr(ctx, "wa", None))
    channel = invites.channel_of(sender)
    number = getattr(ctx.settings, "central_whatsapp_number", "") if channel == "wa" else ""
    link = invites.invite_for(channel, slug, username=username, number=number)
    await _clear_session(ctx.pool, sender)
    await _say(ctx, pid, sender, t(lang, "ob_live", salon=data["salon"], link=link))
    return True


def _question_for(state: dict) -> str:
    """The prompt that belongs to the state's step, rebuilt from what is known so far."""
    lang, data = state.get("lang", "en"), state.get("data", {})
    ex = registration_format()
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
    words = guard.clean(text)
    low = words.lower()
    if wants_cancel_registration(words):
        await clear_session(ctx.pool, sender)
        await _say(ctx, pid, sender, t(smalltalk.language(words, lang), "ob_cancelled"))
        return True
    step = state.get("step", "ask_name")
    if step == "confirm_platform_owner":
        if low in YES_WORDS:
            state["step"] = "ask_name"
            await _save_session(ctx.pool, sender, state)
            await _say(ctx, pid, sender, t(lang, "ob_ask_name"))
        elif low in NO_WORDS:
            await clear_session(ctx.pool, sender)
            await _say(ctx, pid, sender, t(lang, "ob_cancelled"))
        else:
            await _say(ctx, pid, sender, t(lang, "ob_platform_owner_confirm"))
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

    if step == "confirm_all" and is_affirmative(words):
        await _go_live(ctx, pid, sender, state)
        return True

    # A model conversation runs only when enabled. The parser and state machine
    # below remain the authoritative fallback for outages and model refusals.
    if media is None and getattr(ctx, "agent", None) is not None and getattr(ctx.settings, "agent_mode", "off") == "on":
        from .onboarding_agent import advance as agent_advance
        # Keep compact yes/no controls deterministic; natural confirmations are
        # interpreted by the model but still checked against the current state.
        simple_control = low in NO_WORDS and step in {"confirm_name", "confirm_services", "ask_durations", "confirm_all"}
        if not simple_control:
            if await agent_advance(ctx, sender, pid, words, state):
                return True

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
            state.pop("twin", None)
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
        await _say(ctx, pid, sender, t(lang, "ob_ask_prices", example=registration_format()["prices"]))
        return True

    if step == "ask_prices":
        ex = registration_format()
        if media and media.get("kind") == "image":
            await _say(ctx, pid, sender, t(lang, "ob_photo_type", example=ex["prices"]))
            return True
        bad: list[str] = []
        if low not in YES_WORDS or not data.get("services"):
            updated, added, bad = apply_corrections(data, words)
            if not updated and not added:
                await _say(ctx, pid, sender, t(lang, "ob_prices_bad", example=ex["prices"]))
                return True
            if len(data["services"]) > MAX_SERVICES:
                data["services"] = data["services"][:MAX_SERVICES]
                await _say(ctx, pid, sender, t(lang, "ob_too_many", n=MAX_SERVICES))
        state["stack"] = ["ask_prices"]
        state["step"] = "confirm_services"
        state["services_confirmed"] = False
        await _save_session(ctx.pool, sender, state)
        body = t(lang, "ob_confirm_services", lines=_service_lines(data["services"], mark_guessed=True), fix=ex["fix"])
        if bad:
            body += "\n" + t(lang, "ob_skipped", lines="\n".join(f"- {b}" for b in bad[:5]))
        await _say(ctx, pid, sender, body)
        return True

    if step == "confirm_services":
        ex = registration_format()
        if low in YES_WORDS:
            if await _maybe_return(ctx, pid, sender, state):
                return True
            state["stack"] = ["confirm_services"]
            state["services_confirmed"] = True
            if all(s.get("explicit") for s in data["services"]):
                state["step"] = "ask_hours"
            else:
                state["step"] = "ask_durations"
            await _save_session(ctx.pool, sender, state)
            await _resend(ctx, pid, sender, state)
        elif low in NO_WORDS:
            state["step"] = "ask_prices"
            data["services"] = []
            state["services_confirmed"] = False
            state.pop("duration_defaults_accepted", None)
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
            state["services_confirmed"] = False
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
            state["duration_defaults_accepted"] = True
            for service in data.get("services", []):
                service["explicit"] = True
            state["stack"] = ["ask_durations"]
            state["step"] = "ask_hours"
            await _save_session(ctx.pool, sender, state)
            await _say(ctx, pid, sender, t(lang, "ob_ask_hours"))
            return True
        if low in NO_WORDS:
            ex = registration_format()
            await _say(ctx, pid, sender, t(lang, "ob_durations_bad", example=ex["duration"]))
            return True
        fixed, bad = apply_durations(data["services"], words)
        if not fixed:
            ex = registration_format()
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
        ex = registration_format()
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
        await _say(ctx, pid, sender, t(smalltalk.language(text, detect_lang(text)), "ob_limited"))
        return True
    text = guard.clean(text)
    from . import platform
    is_platform_owner = platform.is_platform(ctx.settings, sender)
    step = "confirm_platform_owner" if is_platform_owner else "ask_name"
    state = {"step": step, "data": {}, "lang": smalltalk.language(text, detect_lang(text)), "stack": []}
    await _save_session(ctx.pool, sender, state)
    if is_platform_owner:
        await _say(ctx, pid, sender, t(state["lang"], "ob_platform_owner_confirm"))
        return True
    if getattr(ctx, "agent", None) is not None and getattr(ctx.settings, "agent_mode", "off") == "on":
        from .onboarding_agent import advance as agent_advance
        if await agent_advance(ctx, sender, pid, text, state):
            return True
    await _say(ctx, pid, sender, t(state["lang"], "ob_ask_name"))
    return True
