"""The owner's assistant reads bookings and payment totals through tools, prepares a verified daily summary, and generates a requested business invite.
It can cancel a booking only when the owner clearly asks, through the same code path as `cancel` (the customer is told and refunds are queued).
Everything it says about bookings and money must come from live context or a tool result."""
from __future__ import annotations

import json
import logging
import re
from datetime import date, datetime, time, timedelta
from zoneinfo import ZoneInfo

from app import owner
from app.contact import display_contact
from app.messages import t

from .grounding import Facts
from .prompt import hours_lines
from .tools import Outcome, _fn

log = logging.getLogger("rembo.agent.admin")

ASKS = re.compile(r"\b(who|nani|how (?:many|much)|ngapi|unpaid|not paid|hajalipa|haijalipa|paid|walilipa|deposits?|amana|income|earned|earnings|revenue|pesa|"
                  r"cancel|ghairi|bookings?|appointments?|miadi|ratiba|schedule|diary|customers?|wateja|clients?|busy|no.?shows?|total|week|wiki|yesterday|jana|today|leo)\b", re.I)
BOOKING_TALK = re.compile(r"\b(nataka|naomba|i want|i'?d like|book me|weka miadi)\b", re.I)
FOLLOWUP = re.compile(r"^\s*(?:yes|yeah|yep|yup|no|nope|nah|ok(?:ay)?|sawa|ndiyo|ndio|hapana|sio|why|kwa nini|mbona|the (?:first|second|third|last) one|(?:first|second|third|last) one)\s*[?.!]*$", re.I)


def looks_like_admin(text: str) -> bool:
    return bool(ASKS.search(text or "")) and not BOOKING_TALK.search(text or "")


def looks_like_admin_followup(text: str) -> bool:
    """Only short confirmations, refusals and clarification questions inherit owner context."""
    return bool(FOLLOWUP.fullmatch(text or "")) and not BOOKING_TALK.search(text or "")


ADMIN_TOOLS = [
    _fn("bookings_on", {"date": {"type": "string"}}, ["date"]),
    _fn("deposits", {"from_date": {"type": "string"}, "to_date": {"type": "string"}}, ["from_date", "to_date"]),
    _fn("daily_summary", {}, []),
    _fn("invite_link", {}, []),
    _fn("cancel_booking", {"booking_ref": {"type": "string"}}, ["booking_ref"]),
]
ADMIN_NAMES = {x["function"]["name"] for x in ADMIN_TOOLS}


def admin_prompt(salon: dict, now: datetime, policy: dict, services: list[dict] | None = None,
                 *, lang: str = "en", first_reply: bool = False) -> str:
    cal = "\n".join(f"{(now + timedelta(days=i - 7)).strftime('%a')} = {(now + timedelta(days=i - 7)).date().isoformat()}" for i in range(22))
    persona = policy["salon_owner_assistant"]
    menu = "\n".join(
        f"- {service['name']}: KES {int(service['price_kes'])}, {int(service['duration_min'])} minutes"
        for service in services or []
    ) or "No active services are listed."
    first_reply_rule = (
        f'This is your first reply in this chat. End with: "{t(lang, "help_hint")}"'
        if first_reply else "The owner already received the help invitation in this chat."
    )
    return f"""{persona['identity']}
{persona['purpose']}
{"\n".join(policy["prompt_wrapper"])}
{json.dumps(persona, ensure_ascii=False, indent=2)}

LIVE BUSINESS CONTEXT
Business: {salon['name']}
Authenticated sender: verified owner or manager for this business.
First-reply guidance: {first_reply_rule}
Today is {now.strftime('%A')} {now.date().isoformat()}, time {now.strftime('%H:%M')} (Africa/Nairobi). Calendar:
{cal}
Opening hours:
{hours_lines(salon.get('open_hours') or {})}
Booking deposit: KES {int(salon.get('deposit_kes') or 0)} per booking; zero means no deposit is required.
Services and prices:
{menu}

OWNER HELP GUIDE
{t(lang, "owner_help")}
Explain any item in simpler words if asked. A question about what a command does is not a request to run it.
"""


class AdminToolbox:
    def __init__(self, ctx, salon: dict, to: str, tz: ZoneInfo, now: datetime, user_text: str = ""):
        self.ctx, self.salon, self.to, self.tz, self.now = ctx, salon, to, tz, now
        self.user_text = user_text
        self.outcomes: list[Outcome] = []
        self.facts = Facts()
        self.facts.reporting = True         # the owner is told what the diary says, including paid and confirmed; customers are not
        self._refs: dict[str, dict] = {}
        self._cancelled = False

    async def run(self, name: str, args: dict) -> dict:
        if name not in ADMIN_NAMES:
            return {"error": "unknown_tool", "tools": sorted(ADMIN_NAMES)}
        outcome_start = len(self.outcomes)
        try:
            result = await getattr(self, "t_" + name)(args if isinstance(args, dict) else {})
        except Exception:
            log.exception("admin tool %s failed", name)
            result = {"error": "tool_failed"}
        verified = [outcome.text for outcome in self.outcomes[outcome_start:] if outcome.text]
        if verified and isinstance(result, dict):
            result = {**result, "verified_owner_outcome": verified}
        packed = json.dumps(result, ensure_ascii=False, default=str)
        self.facts.add(packed, *verified)
        self.facts.prices.update(int(n) for n in re.findall(r"\d+", packed) if n.isdigit() and len(n) < 8)
        return result

    def _day(self, raw) -> date | None:
        try:
            return date.fromisoformat(str(raw))
        except ValueError:
            return None

    async def t_bookings_on(self, a: dict) -> dict:
        day = self._day(a.get("date"))
        if day is None:
            return {"error": "bad_date", "hint": "use YYYY-MM-DD from the calendar"}
        rows = await owner._bookings(self.ctx, self.salon, day, self.tz)
        self._refs = {f"o{i + 1}": r for i, r in enumerate(rows)}
        return {"date": day.isoformat(), "count": len(rows), "bookings": [
            {"booking_ref": f"o{i + 1}", "time": r["starts_at"].astimezone(self.tz).strftime("%H:%M"), "service": r["service"],
             "customer": r["client"] or "customer", "contact": display_contact(r["phone"]), "status": r["status"],
             "deposit_status": "paid" if r["paid"] else "waiting" if r["status"] == "held" else "no_deposit_recorded",
             "deposit_paid_kes": int(r["paid"]) if r["paid"] else 0} for i, r in enumerate(rows)]}

    async def t_deposits(self, a: dict) -> dict:
        d1, d2 = self._day(a.get("from_date")), self._day(a.get("to_date"))
        if d1 is None or d2 is None or d2 < d1 or (d2 - d1).days > 92:
            return {"error": "bad_dates", "hint": "YYYY-MM-DD, from <= to, at most 92 days"}
        start = datetime.combine(d1, time(0), self.tz)
        end = datetime.combine(d2 + timedelta(days=1), time(0), self.tz)
        row = await self.ctx.pool.fetchrow(
            "SELECT COALESCE(sum(amount_kes),0) AS total, count(*) AS n FROM payments WHERE salon_id=$1::uuid AND status='paid' AND settled_at >= $2 AND settled_at < $3",
            self.salon["id"], start, end)
        return {"from": d1.isoformat(), "to": d2.isoformat(), "total_kes": int(row["total"]), "payments": int(row["n"])}

    async def t_daily_summary(self, _a: dict) -> dict:
        summary = await owner.summary_text(
            self.ctx, self.salon, self.tz, self.now,
            "sw" if owner.SWAHILI.search(self.user_text) else "en",
        )
        return {"summary": summary}

    async def t_invite_link(self, _a: dict) -> dict:
        if not re.search(r"\b(?:invite|invitation|mwaliko|share|qr|msimbo)\b", self.user_text, re.I):
            return {"error": "the owner did not ask for an invite link"}
        link = await owner.invite_link(self.ctx, self.salon, self.to)
        self.facts.required_literals.add(link)
        return {"invite_link": link}

    async def t_cancel_booking(self, a: dict) -> dict:
        if self._cancelled:
            return {"error": "already_handled_this_turn"}
        row = self._refs.get(str(a.get("booking_ref") or "").strip().lower())
        if row is None:
            return {"error": "unknown_booking_ref", "hint": "call bookings_on first", "valid": sorted(self._refs)}
        self._cancelled = True
        done = await owner.cancel_booking(self.ctx, self.salon, row["id"], self.tz)
        if done is None:
            return {"status": "nothing_to_cancel"}
        refund = t("en", "owner_refund_hint", amount=done["paid"], phone=display_contact(done.get("refund_phone") or done["phone"])) if done["paid"] else ""
        self.outcomes.append(Outcome("cancelled", t("en", "owner_cancelled", service=done["service"], when=done["when"], client=done["client"] or "customer", refund=refund)))
        self.facts.cancelled = True
        return {"status": "cancelled", "note": "the system sends the confirmation and tells the customer"}
