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

from . import binding, guard, mpesa, smalltalk
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


_SPOKEN_HOURS = {
    "one": 1, "two": 2, "three": 3, "four": 4, "five": 5, "six": 6,
    "seven": 7, "eight": 8, "nine": 9, "ten": 10, "eleven": 11,
    "twelve": 12, "thirteen": 13, "fourteen": 14, "fifteen": 15,
    "sixteen": 16, "seventeen": 17, "eighteen": 18, "nineteen": 19,
    "twenty one": 21, "twenty two": 22, "twenty three": 23,
}
_SPOKEN_HOUR_PATTERN = "|".join(
    re.escape(hour).replace(r"\ ", r"\s+")
    for hour in sorted(_SPOKEN_HOURS, key=len, reverse=True)
)
_SPOKEN_EQUAL_HOURS = re.compile(
    rf"^\s*(?:(?:we(?:'re| are)\s+open\s+)?from\s+)?"
    rf"(?P<hour>\d{{1,2}}|{_SPOKEN_HOUR_PATTERN})\s+(?:to|until|till)\s+(?P=hour)"
    r"(?:\s+daily)?\s*[.!?]*$", re.I)
_SWAHILI_HOURS = {
    "moja": 1, "mbili": 2, "mvili": 2, "tatu": 3, "nne": 4, "tano": 5,
    "sita": 6, "saba": 7, "nane": 8, "tisa": 9, "kumi": 10,
    "kumi na moja": 11, "kumi na mbili": 12,
}
_SWAHILI_TIME = re.compile(
    r"\bsaa\s+(?P<number>kumi\s+na\s+(?:moja|mbili)|kumi|moja|mbili|mvili|tatu|nne|tano|sita|saba|nane|tisa|\d{1,2})"
    r"\s+(?P<period>asubuhi|mchana|jioni|usiku)\b", re.I)


def _normalize_spoken_hours(text: str) -> str:
    """Turn clear spoken clock hours such as 'eight in the morning' into numeric clock tokens."""
    raw = text or ""
    raw = re.sub(r"\b(?:in|at)\s+(?:the\s+)?(morning|afternoon|evening|night)\b", r"\1", raw, flags=re.I)
    raw = re.sub(r"\bmorning\b", "am", raw, flags=re.I)
    raw = re.sub(r"\b(?:afternoon|evening|night)\b", "pm", raw, flags=re.I)
    for phrase, value in sorted(_SPOKEN_HOURS.items(), key=lambda pair: len(pair[0]), reverse=True):
        pattern = r"\b" + re.escape(phrase).replace(r"\ ", r"\s+") + r"\b"
        raw = re.sub(pattern, str(value), raw, flags=re.I)
    return raw


def _parse_swahili_time_pair(text: str):
    matches = list(_SWAHILI_TIME.finditer(text or ""))
    if len(matches) != 2:
        return None
    hours = []
    for match in matches:
        number_text = " ".join(match.group("number").casefold().split())
        number = int(number_text) if number_text.isdigit() else _SWAHILI_HOURS.get(number_text)
        if number is None or not 1 <= number <= 12:
            return None
        period = match.group("period").casefold()
        traditional_hour = (number + 6) % 12 or 12
        if period == "asubuhi":
            hour = 12 if number == 6 else traditional_hour
        elif period == "mchana":
            if number < 6:
                return None
            hour = 12 if number == 6 else traditional_hour + 12
        else:
            hour = (traditional_hour + 12) % 24
        hours.append(hour)
    from datetime import time as _time
    opening, closing = (_time(hour) for hour in hours)
    return (opening, closing) if opening < closing and opening.hour >= 5 else None


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
_DEPOSIT_AMOUNT = re.compile(
    r"\b(?:deposit|depos\w*|amana)\b\D{0,20}(\d[\d,]*)|"
    r"(\d[\d,]*)\D{0,20}\b(?:deposit|depos\w*|amana)\b", re.I)


def deposit_amount_mentions(words: str) -> list[int]:
    """Return numeric amounts explicitly attached to a deposit mention."""
    values = []
    for match in _DEPOSIT_AMOUNT.finditer(words or ""):
        raw = match.group(1) or match.group(2)
        try:
            values.append(int(raw.replace(",", "")))
        except ValueError:
            continue
    return values


def parse_deposit(words: str) -> int | None:
    """A typed deposit amount in KES, 0 for none, or None when the line is not usable.
    A denial next to a number ('no 200') contradicts itself, so it asks again instead of guessing."""
    text = (words or "").strip()
    if not text:
        return None
    matches = list(re.finditer(r"\d[\d,]*", text))
    if len(matches) > 1:
        return None
    if matches:
        if _NO_DEPOSIT.search(text):
            return None
        try:
            amount = int(matches[0].group(0).replace(",", ""))
        except ValueError:
            return None
        return amount if 0 <= amount <= MAX_DEPOSIT else None
    return 0 if _NO_DEPOSIT.search(text) else None


def parse_payment_method(words: str) -> str | None:
    """Choose the manual salon destination type without inferring a phone route."""
    text = " ".join((words or "").casefold().split())
    has_till = bool(re.search(r"\b(?:till|buy goods|lipa na m-?pesa)\b", text))
    has_phone = bool(re.search(r"\b(?:phone|mobile|pochi|airtel|send money|tuma pesa)\b", text))
    if has_till and not has_phone:
        return "till"
    if has_phone and not has_till:
        return "phone"
    return None


def parse_payment_phone_type(words: str) -> str | None:
    """Require the owner to identify which network and transfer route a phone uses."""
    text = " ".join((words or "").casefold().split())
    matches = set()
    if re.search(r"\bairtel(?:\s+money)?\b", text):
        matches.add("airtel_money")
    if re.search(r"\bpochi(?:\s+la\s+biashara)?\b", text):
        matches.add("pochi")
    if re.search(r"\b(?:send\s+money|direct(?:\s+send\s+money)?|tuma\s+pesa)\b", text):
        matches.add("send_money")
    return next(iter(matches)) if len(matches) == 1 else None


def parse_manual_till(words: str) -> str | None:
    """Accept only the bare seven-digit till destination."""
    value = (words or "").strip()
    return value if re.fullmatch(r"\d{7}", value) else None


def parse_payment_phone(words: str) -> str | None:
    """Accept exactly one valid Kenyan mobile number, allowing a short natural sentence."""
    text = words or ""
    matches = list(mpesa._EXTRACT.finditer(text))
    if len(matches) != 1:
        normalized = mpesa.normalize_msisdn(text)
        return normalized
    return mpesa.normalize_msisdn(matches[0].group())


def mpesa_display(number: str) -> str:
    normalized = mpesa.normalize_msisdn(number)
    if normalized:
        local = "0" + normalized[3:]
        return f"{local[:4]} {local[4:7]} {local[7:]}"
    return number


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
    swahili_pair = _parse_swahili_time_pair(text)
    if swahili_pair:
        return swahili_pair
    spoken_equal = _SPOKEN_EQUAL_HOURS.fullmatch((text or "").strip())
    if spoken_equal:
        raw_hour = " ".join(spoken_equal.group("hour").casefold().split())
        hour = int(raw_hour) if raw_hour.isdigit() else _SPOKEN_HOURS.get(raw_hour)
        if hour is not None and 5 <= hour <= 11:
            from datetime import time as _time
            return _time(hour), _time(hour + 12)
    raw = _normalize_spoken_hours((text or "").strip())
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
_NAME_SENTENCE = re.compile(
    r"\b(?:is|are)\s+(?:do|does|doing|a|an|the|open|located|called)\b|"
    r"\b(?:i|we|my|our)\s+(?:am|are|do|run|own|offer|provide|operate)\b|"
    r"\b(?:does|offers?|provides?|operates?|speciali[sz]es)\b", re.I)


def looks_off_topic(text: str) -> bool:
    """True when a message at a name step is chatter, a question, or an explanation, not the name itself."""
    words = guard.clean(text)
    if not words:
        return True
    low = words.lower()
    if low in _FILLERS or words.endswith("?") or len(words) > 60:
        return True
    return bool(_BOOKING_MARKERS.search(words) or _EXPLAIN_MARKERS.search(words))


def looks_like_name_sentence(text: str) -> bool:
    """Reject a sentence fragment at the business-name step so details are clarified, not saved as the name."""
    return bool(_NAME_SENTENCE.search(guard.clean(text)))


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
    ("ask_phone_type", re.compile(r"\b(pochi|airtel|send\s+money|payment\s+type|phone\s+type)\b", re.I)),
    ("ask_payment_phone", re.compile(r"\b(phone\s+number|mobile\s+number|payment\s+number|destination\s+number)\b", re.I)),
    ("ask_till_number", re.compile(r"\b(till\s+(?:number|no\.?|digits?)?|buy\s+goods)\b", re.I)),
    ("ask_payment_method", re.compile(r"\b(payment\s+(?:method|destination|option)|where\s+(?:the\s+)?deposit|payment)\b", re.I)),
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
_REGISTER_ONLY = re.compile(
    r"^\s*(?:(?:ok(?:ay)?|yes|yeah|yep|sure|please|alright|all\s+right)[,!\s]+)*"
    r"(?:/start\s+(?:register|sajili|anzisha)|start\s+(?:register|sajili|anzisha)|"
    r"register|sajili|anzisha|(?:register|sajili|anzisha)\s+(?:(?:a|my|the)\s+)?"
    r"(?:business|biashara|salon|saloon|saluni|kinyozi|barbershop|barber|shop|duka|company|kampuni)|"
    r"(?:i\s+(?:want|need)\s+to|i(?:'d|\s+would)\s+like\s+to)\s+(?:register|sign\s?up)"
    r"(?:\s+(?:(?:a|my|the)\s+)?(?:business|biashara|salon|saloon|saluni|kinyozi|barbershop|barber|shop|duka|company|kampuni))?|"
    r"please\s+(?:register|sign\s?up)(?:\s+(?:(?:a|my|the)\s+)?(?:business|biashara|salon|saloon|saluni|kinyozi|barbershop|barber|shop|duka|company|kampuni))?|"
    r"nataka\s+kusajili(?:\s+(?:biashara|saluni|kinyozi))?|naomba\s+kusajili(?:\s+(?:biashara|saluni|kinyozi))?)\s*[.!?]*$", re.I)


def registration_start_only(text: str) -> bool:
    """True for a start command that does not contain any registration details."""
    return bool(_REGISTER_ONLY.fullmatch(" ".join(guard.clean(text or "").split())))


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


_DURATION_CONFIRM_PROMPT = re.compile(
    r"\b(?:estimate(?:d)?|default)\b.{0,120}\b(?:keep|confirm|accept|change|different)\b|"
    r"\b(?:keep|confirm|accept|change)\b.{0,120}\b(?:estimate(?:d)?|default|minutes?|hours?|duration|time)\b", re.I)
_AFFIRMATIVE_PREFIX = re.compile(r"^(?:yes|yeah|yep|yup|ndiyo|ndio|sawa|ok|okay)\b", re.I)
_STATED_DURATION = re.compile(r"\b\d+\s*(?:m|min|mins|minute|minutes|h|hr|hrs|hour|hours)\b", re.I)


def awaiting_duration_approval(state: dict) -> bool:
    """True only when the latest assistant turn explicitly asked to keep or change an estimate."""
    history = state.get("agent_history")
    if not isinstance(history, list):
        return False
    last = next((row.get("text", "") for row in reversed(history)
                 if isinstance(row, dict) and row.get("role") == "assistant"), "")
    return bool(_DURATION_CONFIRM_PROMPT.search(str(last)))


def is_duration_approval(text: str) -> bool:
    """Recognize a clear yes to an estimated duration, including 'keep it' and 'yes, 30 min'."""
    words = " ".join((text or "").strip().lower().split())
    if re.search(r"\b(?:don't|dont|do not|not|different|instead|change it|change to)\b", words):
        return False
    if wants_keep(words, False) or is_affirmative(words):
        return True
    return bool(_AFFIRMATIVE_PREFIX.search(words) and _STATED_DURATION.search(words))


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
    if step == "ask_payment_method":
        return data.get("payment_method") in ("till", "phone")
    if step == "ask_till_number":
        return parse_manual_till(data.get("payment_destination", "")) is not None
    if step == "ask_phone_type":
        return data.get("payment_method") == "phone" and data.get("payment_type") in ("pochi", "send_money", "airtel_money")
    if step == "ask_payment_phone":
        return data.get("payment_method") == "phone" and parse_payment_phone(data.get("payment_destination", "")) is not None
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
            if "duration_defaults_accepted" in state or state.get("step") not in (
                    "ask_hours", "ask_deposit", "ask_payment_method", "ask_till_number",
                    "ask_phone_type", "ask_payment_phone", "confirm_all"):
                return False
    if not state.get("services_confirmed") and state.get("step") not in (
            "ask_durations", "ask_hours", "ask_deposit", "ask_payment_method", "ask_till_number",
            "ask_phone_type", "ask_payment_phone", "confirm_all"):
        return False
    if not data.get("open") or not data.get("close") or parse_time_pair(f"{data['open']}-{data['close']}") is None:
        return False
    deposit = data.get("deposit")
    if not isinstance(deposit, int) or isinstance(deposit, bool) or not 0 <= deposit <= MAX_DEPOSIT:
        return False
    if deposit == 0:
        return True
    method = data.get("payment_method")
    if method == "till":
        return parse_manual_till(data.get("payment_destination", "")) is not None
    if method == "phone":
        return (data.get("payment_type") in ("pochi", "send_money", "airtel_money")
                and parse_payment_phone(data.get("payment_destination", "")) is not None)
    return False


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
    if deposit > 0:
        method = data.get("payment_method")
        if method not in ("till", "phone"):
            return "ask_payment_method"
        if method == "till" and parse_manual_till(data.get("payment_destination", "")) is None:
            return "ask_till_number"
        if method == "phone":
            if data.get("payment_type") not in ("pochi", "send_money", "airtel_money"):
                return "ask_phone_type"
            if parse_payment_phone(data.get("payment_destination", "")) is None:
                return "ask_payment_phone"
    return "confirm_all"


async def _go_live(ctx, pid: str, sender: str, state: dict) -> bool:
    """Create the salon, its stylists, and its services, then hand back the invite link."""
    if not state.get("final_confirmation_pending"):
        await _present_final_confirmation(ctx, pid, sender, state)
        return False
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
        if int(data.get("deposit", 0)) > 0:
            kind = "till" if data["payment_method"] == "till" else "phone"
            await con.execute(
                "INSERT INTO salon_payment (salon_id, kind, shortcode, party_b, account_ref, manual, phone_type) "
                "VALUES ($1::uuid,$2,NULL,$3,'RemboAI',true,$4)",
                salon, kind, data["payment_destination"], data.get("payment_type"))
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
    if step == "ask_payment_method":
        return t(lang, "ob_ask_payment_method")
    if step == "ask_till_number":
        return t(lang, "ob_ask_till_number")
    if step == "ask_phone_type":
        return t(lang, "ob_ask_phone_type")
    if step == "ask_payment_phone":
        return t(lang, "ob_ask_payment_phone")
    if step == "confirm_all":
        owners = ", ".join(data.get("owners", []))
        payment = "No deposit" if not data.get("deposit") else (
            f"M-PESA Till {data.get('payment_destination')}" if data.get("payment_method") == "till" else
            f"{'Airtel Money' if data.get('payment_type') == 'airtel_money' else 'M-PESA Pochi la Biashara' if data.get('payment_type') == 'pochi' else 'M-PESA Send Money'} {mpesa_display(data.get('payment_destination', ''))}")
        return t(lang, "ob_confirm_all", salon=data.get("salon", ""), owners=owners,
                 lines=_service_lines(data.get("services", [])), fix=ex["fix"],
                 hours=f"{data.get('open', '')}-{data.get('close', '')}", deposit=data.get("deposit", 0), payment=payment)
    return t(lang, "ob_ask_name")


async def _present_final_confirmation(ctx, pid: str, sender: str, state: dict) -> None:
    """Show the validated final summary and record that the next clear yes confirms this exact draft."""
    if not registration_complete(state):
        state["final_confirmation_pending"] = False
        state["step"] = registration_next_step(state)
        await _save_session(ctx.pool, sender, state)
        await _resend(ctx, pid, sender, state)
        return
    state["step"] = "confirm_all"
    state["final_confirmation_pending"] = True
    await _save_session(ctx.pool, sender, state)
    await _say(ctx, pid, sender, _question_for(state))


async def _resend(ctx, pid: str, sender: str, state: dict, prefix: str | None = None) -> None:
    if state.get("step") == "confirm_all" and registration_complete(state):
        state["final_confirmation_pending"] = True
        await _save_session(ctx.pool, sender, state)
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
    if dest == "confirm_all" and not registration_complete(state):
        return False
    state["return_to"] = None
    state["stack"] = [state.get("step", "ask_hours")]
    state["step"] = dest
    await _save_session(ctx.pool, sender, state)
    await _resend(ctx, pid, sender, state)
    return True


async def _accept_duration_estimates(ctx, pid: str, sender: str, state: dict, words: str) -> bool:
    """Record an explicit yes to the displayed durations and keep any other clear details in that reply."""
    data = state.get("data", {})
    services = data.get("services", [])
    if not services:
        return False
    previous_step = state.get("step", "ask_durations")
    for service in services:
        if isinstance(service, dict):
            service["explicit"] = True
    state["services_confirmed"] = True
    state["duration_defaults_accepted"] = True

    hours = parse_time_pair(words)
    if hours:
        data["open"], data["close"] = hours[0].strftime("%H:%M"), hours[1].strftime("%H:%M")
    if re.search(r"\b(?:deposit|amana)\b", words, re.I):
        deposit = parse_deposit(words)
        if deposit is not None:
            data["deposit"] = deposit

    state["stack"] = [previous_step]
    state["step"] = registration_next_step(state)
    if await _maybe_return(ctx, pid, sender, state):
        return True
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
        # This only acknowledges the ownership notice; salon creation still
        # requires the separate, explicit final confirmation below.
        if low in YES_WORDS or low.strip(" .,!? ") == "ye":
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
    duration_prompt = step == "confirm_services" and awaiting_duration_approval(state)
    duration_reply = is_duration_approval(words) and (
        step == "ask_durations" or (step == "confirm_services" and duration_prompt))
    if (step not in ("confirm_services", "confirm_all")
            and wants_keep(words, bool(state.get("return_to"))) and not duration_reply):
        if _keep_data_present(state) and await _maybe_return(ctx, pid, sender, state):
            return True
        await _resend(ctx, pid, sender, state, t(lang, "ob_steer"))
        return True
    step = state.get("step", "ask_name")

    if step == "confirm_all":
        if is_affirmative(words):
            if state.get("final_confirmation_pending"):
                await _go_live(ctx, pid, sender, state)
            else:
                await _present_final_confirmation(ctx, pid, sender, state)
            return True
        if state.get("final_confirmation_pending"):
            state["final_confirmation_pending"] = False
            await _save_session(ctx.pool, sender, state)

    # A model conversation runs only when enabled. The parser and state machine
    # below remain the authoritative fallback for outages and model refusals.
    if media is None and getattr(ctx, "agent", None) is not None and getattr(ctx.settings, "agent_mode", "off") == "on":
        from .onboarding_agent import advance as agent_advance
        if await agent_advance(ctx, sender, pid, words, state):
            return True

    if step == "ask_name":
        if low in _NEVER_A_NAME or registration_start_only(words) or looks_off_topic(words) or looks_like_name_sentence(words):
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
        if duration_prompt and is_duration_approval(words):
            await _accept_duration_estimates(ctx, pid, sender, state, words)
        elif low in YES_WORDS:
            state["stack"] = ["confirm_services"]
            state["services_confirmed"] = True
            if all(s.get("explicit") for s in data["services"]):
                state["step"] = "ask_hours"
            else:
                state["step"] = "ask_durations"
            await _save_session(ctx.pool, sender, state)
            if await _maybe_return(ctx, pid, sender, state):
                return True
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
        if is_duration_approval(words):
            await _accept_duration_estimates(ctx, pid, sender, state, words)
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
        state["step"] = "ask_payment_method" if amount > 0 and data.get("payment_method") not in ("till", "phone") else registration_next_step(state)
        await _save_session(ctx.pool, sender, state)
        if await _maybe_return(ctx, pid, sender, state):
            return True
        await _resend(ctx, pid, sender, state)
        return True

    if step == "ask_payment_method":
        method = parse_payment_method(words)
        if method is None:
            await _say(ctx, pid, sender, t(lang, "ob_payment_method_bad"))
            return True
        if method != data.get("payment_method"):
            data.pop("payment_destination", None)
            data.pop("payment_type", None)
        data["payment_method"] = method
        state["stack"] = ["ask_payment_method"]
        state["step"] = "ask_till_number" if method == "till" else "ask_phone_type"
        await _save_session(ctx.pool, sender, state)
        if await _maybe_return(ctx, pid, sender, state):
            return True
        await _resend(ctx, pid, sender, state)
        return True

    if step == "ask_till_number":
        till = parse_manual_till(words)
        if till is None:
            await _say(ctx, pid, sender, t(lang, "ob_till_bad"))
            return True
        data["payment_destination"] = till
        state["stack"] = ["ask_till_number"]
        state["step"] = "confirm_all"
        await _save_session(ctx.pool, sender, state)
        if await _maybe_return(ctx, pid, sender, state):
            return True
        await _resend(ctx, pid, sender, state)
        return True

    if step == "ask_phone_type":
        phone_type = parse_payment_phone_type(words)
        if phone_type is None:
            await _say(ctx, pid, sender, t(lang, "ob_phone_type_bad"))
            return True
        if phone_type != data.get("payment_type"):
            data.pop("payment_destination", None)
        data["payment_type"] = phone_type
        state["stack"] = ["ask_phone_type"]
        state["step"] = "ask_payment_phone"
        await _save_session(ctx.pool, sender, state)
        if await _maybe_return(ctx, pid, sender, state):
            return True
        await _resend(ctx, pid, sender, state)
        return True

    if step == "ask_payment_phone":
        phone = parse_payment_phone(words)
        if phone is None:
            await _say(ctx, pid, sender, t(lang, "ob_payment_phone_bad"))
            return True
        data["payment_destination"] = phone
        state["stack"] = ["ask_payment_phone"]
        state["step"] = "confirm_all"
        await _save_session(ctx.pool, sender, state)
        if await _maybe_return(ctx, pid, sender, state):
            return True
        await _resend(ctx, pid, sender, state)
        return True

    if step == "confirm_all":
        ex = registration_format()
        if low in YES_WORDS:
            if state.get("final_confirmation_pending"):
                await _go_live(ctx, pid, sender, state)
            else:
                await _present_final_confirmation(ctx, pid, sender, state)
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
    if (not registration_start_only(text) and getattr(ctx, "agent", None) is not None
            and getattr(ctx.settings, "agent_mode", "off") == "on"):
        from .onboarding_agent import advance as agent_advance
        if await agent_advance(ctx, sender, pid, text, state):
            return True
    await _say(ctx, pid, sender, t(state["lang"], "ob_ask_name"))
    return True
