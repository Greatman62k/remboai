"""Free slots for a service. Pure computation over data passed in, so it is easy to test."""
from __future__ import annotations

from dataclasses import dataclass
from datetime import date, datetime, time, timedelta
from zoneinfo import ZoneInfo

DAY_KEYS = ("mon", "tue", "wed", "thu", "fri", "sat", "sun")
WINDOWS = {"morning": (time(8), time(12)), "afternoon": (time(12), time(17)), "evening": (time(17), time(21))}
STEP = timedelta(minutes=30)


@dataclass(frozen=True)
class Slot:
    staff_id: str
    staff_name: str
    starts_at: datetime
    ends_at: datetime


def day_slots(day: date, open_hours: dict, tz: ZoneInfo, duration: timedelta, staff: list[dict],
              busy: dict[str, list[tuple[datetime, datetime]]], not_before: datetime) -> list[Slot]:
    """Every start time on `day` (30 minute grid) where some stylist is free for the whole service."""
    hours = open_hours.get(DAY_KEYS[day.weekday()])
    if not hours:
        return []
    opens = datetime.combine(day, time.fromisoformat(hours[0]), tz)
    closes = datetime.combine(day, time.fromisoformat(hours[1]), tz)
    out: list[Slot] = []
    t = opens
    while t + duration <= closes:
        if t >= not_before:
            for s in staff:
                end = t + duration
                if all(end <= b0 or t >= b1 for b0, b1 in busy.get(s["id"], [])):
                    out.append(Slot(s["id"], s["name"], t, end))
                    break  # one stylist per start time is enough
        t += STEP
    return out


def choose(slots: list[Slot], *, window: str | None = None, at: time | None = None, limit: int = 3) -> tuple[list[Slot], bool]:
    """Pick up to `limit` slots. Returns (slots, exact): exact=False means nothing matched and these are the nearest."""
    if at:
        exact = [s for s in slots if s.starts_at.timetz().replace(tzinfo=None) == at]
        if exact:
            return exact[:1], True
    if window:
        lo, hi = WINDOWS[window]
        inside = [s for s in slots if lo <= s.starts_at.timetz().replace(tzinfo=None) < hi]
        if inside:
            return inside[:limit], True
    if at:
        ref = at.hour * 60 + at.minute
        slots = sorted(slots, key=lambda s: (s.starts_at.date(), abs(s.starts_at.hour * 60 + s.starts_at.minute - ref)))
        return slots[:limit], False
    return slots[:limit], not (window or at)
