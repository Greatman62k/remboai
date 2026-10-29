"""Turning how Kenyans say dates and times into code. Swahili counts hours from dawn: 'saa mbili' is 08:00, 'saa nane' is 14:00."""
from __future__ import annotations

import re
from datetime import date, time, timedelta

from . import lexicon

_LX = lexicon.load()
_DAY_NAMES = ("monday", "tuesday", "wednesday", "thursday", "friday", "saturday", "sunday")
WEEKDAYS = {word: i for i, name in enumerate(_DAY_NAMES) for word in _LX.words(name)}     # from app/lexicon.json
_TODAY, _TOMORROW, _DAY_AFTER = (_LX.pattern(c) for c in ("today", "tomorrow", "day_after_tomorrow"))
_MORNING, _AFTERNOON, _EVENING = (_LX.pattern(c) for c in ("morning", "afternoon", "evening"))
_AM, _PM = _LX.pattern("morning"), _LX.pattern("afternoon", "evening")
SW_HOURS = {"moja": 1, "mbili": 2, "tatu": 3, "nne": 4, "tano": 5, "sita": 6, "saba": 7, "nane": 8, "tisa": 9, "kumi": 10, "kumi na moja": 11, "kumi na mbili": 12}


def swahili_hour(n: int) -> int:
    """Swahili hour n (1-12, counted from 06:00) -> 12-hour clock number: 1->7, 2->8, ... 6->12, 7->1, ... 12->6."""
    return (n + 5) % 12 + 1


def parse_day(text: str, today: date) -> date | None:
    t = text.lower()
    if _DAY_AFTER.search(t):                      # before "tomorrow": "kesho kutwa" contains "kesho" but means the day after
        return today + timedelta(days=2)
    if _TODAY.search(t):
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


def parse_time(text: str) -> time | None:
    """'2pm', '14:30', 'at 4', 'saa nane mchana', 'saa mbili asubuhi'. Returns None when no clock time is stated."""
    t = text.lower()
    m = re.search(r"\b(\d{1,2})[:.](\d{2})\s*(am|pm)?\b", t)
    if m:
        h, mi, ap = int(m[1]), int(m[2]), m[3]
        if ap == "pm" and h < 12:
            h += 12
        if ap == "am" and h == 12:
            h = 0
        return time(h, mi) if h < 24 and mi < 60 else None
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
        return time(h12) if 7 <= h12 <= 11 else time(h12 % 12 + 12)  # no qualifier: assume business hours
    m = re.search(r"\b(?:at|around|ama)\s+(\d{1,2})\b", t)
    if m and 1 <= int(m[1]) <= 12:
        h = int(m[1])
        return time(h) if 7 <= h <= 11 else time(h % 12 + 12)
    return None
