"""Turning how Kenyans say dates and times into code. Swahili counts hours from dawn: 'saa mbili' is 08:00, 'saa nane' is 14:00."""
from __future__ import annotations

import re
from datetime import date, time, timedelta

WEEKDAYS = {
    "monday": 0, "mon": 0, "jumatatu": 0, "tuesday": 1, "tue": 1, "jumanne": 1, "wednesday": 2, "wed": 2, "jumatano": 2,
    "thursday": 3, "thu": 3, "alhamisi": 3, "friday": 4, "fri": 4, "ijumaa": 4, "saturday": 5, "sat": 5, "jumamosi": 5,
    "sunday": 6, "sun": 6, "jumapili": 6,
}
SW_HOURS = {"moja": 1, "mbili": 2, "tatu": 3, "nne": 4, "tano": 5, "sita": 6, "saba": 7, "nane": 8, "tisa": 9, "kumi": 10, "kumi na moja": 11, "kumi na mbili": 12}


def swahili_hour(n: int) -> int:
    """Swahili hour n (1-12, counted from 06:00) -> 12-hour clock number: 1->7, 2->8, ... 6->12, 7->1, ... 12->6."""
    return (n + 5) % 12 + 1


def parse_day(text: str, today: date) -> date | None:
    t = text.lower()
    if re.search(r"\b(leo|today|2day)\b", t):
        return today
    if re.search(r"\b(kesho|tomorrow|tmrw|2morrow)\b", t):
        return today + timedelta(days=1)
    for word, wd in WEEKDAYS.items():
        if re.search(rf"\b{word}\b", t):
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
    if re.search(r"\b(asubuhi|morning)\b", t):
        return "morning"
    if re.search(r"\b(mchana|afternoon|after lunch|lunch)\b", t):
        return "afternoon"
    if re.search(r"\b(jioni|evening|usiku|night)\b", t):
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
        if re.search(r"\b(asubuhi)\b", t):
            return time(h12 % 12 if h12 != 12 else 12)
        if re.search(r"\b(mchana|jioni|usiku)\b", t):
            return time(h12 % 12 + 12)
        return time(h12) if 7 <= h12 <= 11 else time(h12 % 12 + 12)  # no qualifier: assume business hours
    m = re.search(r"\b(?:at|around|ama)\s+(\d{1,2})\b", t)
    if m and 1 <= int(m[1]) <= 12:
        h = int(m[1])
        return time(h) if 7 <= h <= 11 else time(h % 12 + 12)
    return None
