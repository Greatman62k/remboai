"""Turning how Kenyans say dates and times into code. Swahili counts hours from dawn: 'saa mbili' is 08:00, 'saa nane' is 14:00."""
from __future__ import annotations

import re
from datetime import date, time, timedelta

from . import lexicon

_LX = lexicon.load()
_DAY_NAMES = ("monday", "tuesday", "wednesday", "thursday", "friday", "saturday", "sunday")
WEEKDAYS = {word: i for i, name in enumerate(_DAY_NAMES) for word in _LX.words(name)}
_TODAY, _TOMORROW, _DAY_AFTER = (_LX.pattern(c) for c in ("today", "tomorrow", "day_after_tomorrow"))
_MORNING, _AFTERNOON, _EVENING = (_LX.pattern(c) for c in ("morning", "afternoon", "evening"))
_AM, _PM = _LX.pattern("morning"), _LX.pattern("afternoon", "evening")
SW_HOURS = {"moja": 1, "mbili": 2, "tatu": 3, "nne": 4, "tano": 5, "sita": 6, "saba": 7, "nane": 8, "tisa": 9, "kumi": 10, "kumi na moja": 11, "kumi na mbili": 12}


def swahili_hour(n: int) -> int:
    """Swahili hour n (1-12, counted from 06:00) -> 12-hour clock number: 1->7, 2->8, ... 6->12, 7->1, ... 12->6."""
    return (n + 5) % 12 + 1


_TODAY_ALT = _LX.alternation("today")
_CANT_SW = r"(?:siwezi|sitaweza|sitoweza|sitakuja|sipo|haiwezekani|haifai)"
_CANT_EN = r"(?:can'?t|cannot|can\s+not|won'?t|wont|unable|not\s+able)"
_DECLINED_TODAY = re.compile(
    rf"\b(?:{_TODAY_ALT})\W+{_CANT_SW}\b"
    rf"|\b{_CANT_SW}\W+(?:kuja\W+)?(?:siku\W+ya\W+)?(?:{_TODAY_ALT})\b"
    rf"|\b{_CANT_EN}\W+(?:\w+\W+){{0,2}}?(?:{_TODAY_ALT})\b"
    rf"|\bnot\W+(?:{_TODAY_ALT})\b"
    rf"|\b(?:{_TODAY_ALT})\W+(?:won'?t|doesn'?t|does\s+not)\W+work\b"
    rf"|\b(?:{_TODAY_ALT})\W+(?:is\W+)?(?:not\W+possible|no\W+good)\b", re.I)


def parse_day(text: str, today: date) -> date | None:
    t = text.lower()
    if _DAY_AFTER.search(t):
        return today + timedelta(days=2)
    if _TODAY.search(_DECLINED_TODAY.sub(" ", t)):
        return today
    if _TOMORROW.search(t):
        return today + timedelta(days=1)
    for word, wd in WEEKDAYS.items():
        if re.search(rf"\b{re.escape(word)}\b", t):
            return today + timedelta(days=(wd - today.weekday()) % 7 or 7)
    m = re.search(r"\b(\d{4})-(\d{2})-(\d{2})\b", t)
    if m:
        try:
            return date(int(m[1]), int(m[2]), int(m[3]))
        except ValueError:
            return None
    return None


def parse_window(text: str) -> str | None:
    t = text.lower()
    if _MORNING.search(t):
        return "morning"
    if _AFTERNOON.search(t):
        return "afternoon"
    if _EVENING.search(t):
        return "evening"
    return None


_CLOCK = re.compile(r"(?<![\d.:])(\d{1,2})([:.])(\d{2})(?!\d|\.\d)\s*(am|pm)?\b")
_CLOCK_WORD = re.compile(r"(?:\bat|\bsaa|\baround|\bama|\bby|@)\s*$")
_MONEY_WORD = re.compile(r"(?:\bksh|\bkes|\bkshs|\bshs?|\busd|\$)\.?\s*$")
_WINDOW_HOURS = {"morning": range(5, 12), "afternoon": range(12, 17), "evening": range(17, 22)}


def _hour_in_window(n: int, window: str | None) -> int | None:
    """The 24-hour hour that number n means, given 'asubuhi/mchana/jioni' next to it. Both ways of counting are tried, the clock face first
    ('saa 8 jioni' is 20:00) then Swahili counting from dawn ('saa 8 mchana' is 14:00), and the first that lands inside the named part of the day wins."""
    if window not in _WINDOW_HOURS or n < 1:
        return None
    if n > 12:
        return n if n < 24 and n in _WINDOW_HOURS[window] else None
    sw = swahili_hour(n)
    for h in (n % 12, n % 12 + 12, sw % 12, sw % 12 + 12):
        if h in _WINDOW_HOURS[window]:
            return h
    return None


def _business_hour(n: int) -> int:
    """No am/pm and no part of the day: 7-11 are mornings, 12 is noon, 1-6 are afternoons (a salon is not open at 3am)."""
    return n if 7 <= n <= 11 else n % 12 + 12


def parse_time(text: str) -> time | None:
    """'2pm', '14:30', 'at 4', 'saa nane mchana', 'saa 8 mchana', 'saa mbili asubuhi'. Returns None when no clock time is stated.
    A dotted number is a time only with am/pm or a word like 'at'/'saa' in front: '15.11' is a date and 'Ksh 1.50' is a price."""
    t = text.lower()
    window = parse_window(t)
    for m in _CLOCK.finditer(t):
        h, sep, mi, ap = int(m[1]), m[2], int(m[3]), m[4]
        before = t[: m.start()]
        if sep == "." and not ap and not _CLOCK_WORD.search(before):
            continue
        if _MONEY_WORD.search(before) or mi > 59:
            continue
        if ap == "pm" and h < 12:
            h += 12
        elif ap == "am" and h == 12:
            h = 0
        elif not ap and h <= 12:
            h = _hour_in_window(h, window) or h
        if h < 24:
            return time(h, mi)
    m = re.search(r"\b(\d{1,2})\s*(am|pm)\b", t)
    if m:
        h = int(m[1]) % 12 + (12 if m[2] == "pm" else 0)
        return time(h)
    m = re.search(r"\bsaa\s+(kumi na moja|kumi na mbili|moja|mbili|tatu|nne|tano|sita|saba|nane|tisa|kumi)\b", t)
    if m:
        h12 = swahili_hour(SW_HOURS[m[1]])
        if _AM.search(t):
            return time(h12 % 12 if h12 != 12 else 12)
        if _PM.search(t):
            return time(h12 % 12 + 12)
        return time(h12) if 7 <= h12 <= 11 else time(h12 % 12 + 12)
    m = re.search(r"\bsaa\s+(\d{1,2})\b(?!\s*[:.]\d)", t)
    if m and 1 <= int(m[1]) < 24:
        n = int(m[1])
        if n > 12:
            return time(n)
        return time(_hour_in_window(n, window) or _business_hour(n))
    m = re.search(r"\b(?:at|around|ama)\s+(\d{1,2})\b", t)
    if m and 1 <= int(m[1]) <= 12:
        return time(_business_hour(int(m[1])))
    return None
