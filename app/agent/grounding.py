"""The checker between the model's words and the customer.

The model writes the reply; code decides whether it may be sent. A reply is grounded when every number, time, price and
link in it can be found in what the model was actually given: the salon facts, the conversation, and tool results.
Anything else (an invented price, a slot that was never offered, a made-up phone number, a claim that money arrived)
is rejected, and the turn falls back to the code-written templates built from the same tool results."""
from __future__ import annotations

import re
from dataclasses import dataclass, field

_URL = re.compile(r"https?://|www\.|\bt\.me/|\bwa\.me/|\b[\w-]+\.(?:com|co\.ke|ke|me|io)\b", re.I)
_TIME_24 = re.compile(r"(?<![\d:])([01]?\d|2[0-3]):([0-5]\d)(?!\d)")
_TIME_12 = re.compile(r"(?<![\d:])(1[0-2]|0?[1-9])(?:[:.]([0-5]\d))?\s*(am|pm)\b", re.I)
_MONEY_BEFORE = re.compile(r"\b(?:kes|ksh|sh|bob)\.?\s*([\d,]+)\b", re.I)
_MONEY_AFTER = re.compile(r"([\d,]+)\s*(?:/=|kes|ksh|bob|shillings?|shilingi)\b", re.I)
_DIGITS = re.compile(r"\d+")

# Words that state a result. They are only allowed when a tool in this very turn produced that result.
_PAID = re.compile(r"\b(paid|payment (?:received|confirmed|successful)|umelipa|malipo (?:yamepokelewa|yamethibitishwa)|nimepokea malipo|received your (?:deposit|payment))\b", re.I)
_BOOKED = re.compile(r"\b(confirmed|you'?re booked|you are booked|booking is (?:done|complete)|imethibitishwa|umethibitishiwa|umepata nafasi|miadi yako imekamilika)\b", re.I)
_CANCELLED = re.compile(r"\b(cancell?ed|nimeghairi|imeghairiwa|nimefuta|umeghairi)\b", re.I)
_POLICY = re.compile(r"\b(waiv\w*|discount\w*|free of charge|no deposit|punguzo|msamaha|bila amana|hakuna amana|bure)\b", re.I)
_LEAK = re.compile(r"\b(tool call|function call|system prompt|developer message|find_slots|reserve_slot|cancel_booking|my_bookings|join_waitlist|hand_to_owner|list_businesses|platform_totals|business_status|json)\b|<think>|```", re.I)

MAX_REPLY = 700


@dataclass
class Facts:
    """Everything the model was allowed to know this turn."""
    text: list[str] = field(default_factory=list)       # salon facts, earlier replies, tool results
    user: list[str] = field(default_factory=list)       # what the customer wrote: may be echoed as a time, never as a number or a link
    deposit_none: bool = False                            # the salon really asks for no deposit
    reporting: bool = False                               # owner-facing: tool results may be reported as paid/confirmed
    prices: set[int] = field(default_factory=set)        # every KES amount the model may mention
    booked: bool = False                                  # a tool confirmed a booking this turn
    held: bool = False                                    # a tool held a slot this turn
    cancelled: bool = False                               # a tool cancelled a booking this turn

    def add(self, *parts: str) -> None:
        self.text.extend(p for p in parts if p)

    def add_user(self, *parts: str) -> None:
        self.user.extend(p for p in parts if p)

    def blob(self) -> str:
        return "\n".join(self.text)

    def blob_with_user(self) -> str:
        return "\n".join(self.text + self.user)


def _times(text: str) -> set[str]:
    out = {f"{int(h):02d}:{m}" for h, m in _TIME_24.findall(text)}
    for h, m, ap in _TIME_12.findall(text):
        hh = int(h) % 12 + (12 if ap.lower() == "pm" else 0)
        out.add(f"{hh:02d}:{m or '00'}")
    return out


def _amounts(text: str) -> set[int]:
    got: set[int] = set()
    for rx in (_MONEY_BEFORE, _MONEY_AFTER):
        for m in rx.findall(text):
            digits = m.replace(",", "")
            if digits.isdigit():
                got.add(int(digits))
    return got


def check(reply: str, facts: Facts) -> list[str]:
    """Reasons the reply may not be sent. Empty means grounded."""
    problems: list[str] = []
    text = (reply or "").strip()
    if not text:
        return ["empty"]
    if len(text) > MAX_REPLY:
        problems.append("too long")
    if _LEAK.search(text):
        problems.append("mentions internals")
    blob = facts.blob()
    if _URL.search(text) and not _URL.search(blob):
        problems.append("link that was never provided")
    known_digits = set(_DIGITS.findall(blob))
    # a time must be one the model was given (slot times, opening hours, or what the customer said)
    given_times = _times(facts.blob_with_user())
    for tm in _times(text):
        if tm not in given_times:
            problems.append(f"time {tm} was not in the facts")
    # a price must be a real price or deposit
    for amount in _amounts(text):
        if amount not in facts.prices and str(amount) not in known_digits:
            problems.append(f"amount {amount} was not in the facts")
        elif amount not in facts.prices:
            problems.append(f"amount {amount} is not a price or deposit")
    # long digit runs are phone numbers, tills or receipts: never invented
    for run in _DIGITS.findall(text):
        if len(run) >= 6 and run not in known_digits:
            problems.append("a phone, till or receipt number that was never provided")
            break
    if _POLICY.search(text) and not facts.deposit_none and not facts.reporting:
        problems.append("states a deposit or discount policy that is not in the facts")
    if _PAID.search(text) and not facts.reporting:
        problems.append("claims a payment (only the payment system may)")
    if _BOOKED.search(text) and not facts.booked and not facts.reporting:
        problems.append("claims a confirmed booking that no tool confirmed")
    if _CANCELLED.search(text) and not facts.cancelled:
        problems.append("claims a cancellation that no tool made")
    return sorted(set(problems))
