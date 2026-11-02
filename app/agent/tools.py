"""The agent's hands. The model chooses a tool and its arguments; this file checks them against the real world and does the work.

What the model can never do: set a price or a deposit amount, mark anything paid, pick a slot code never offered, touch another
customer's bookings, or send a message of its own. Each tool wraps the booking engine that already existed (holds, the exclusion
constraint, idempotent cancellation, the payment prompt), so a model mistake can waste a turn but cannot damage a booking or money.

Tools that change something return `Outcome.text`, written by code from the same templates the keyword flow uses. That text is always
shown to the customer, whatever the model says around it."""
from __future__ import annotations

import json
import logging
import re
from dataclasses import dataclass, field
from datetime import date, datetime, time, timedelta

from app import booking_flow as bf
from app import guard, payments, waitlist
from app.channels import outbound
from app.messages import t

from .grounding import Facts

log = logging.getLogger("rembo.agent.tools")
MUTATING = {"held", "booked", "not_booked", "cancelled", "nothing_to_cancel", "waitlist", "handoff"}
HANDOFF = {"complaint": "complaint", "payment_dispute": "payment_dispute", "medical": "medical", "human_request": "human_request", "other": "default"}


@dataclass
class Outcome:
    kind: str                       # offered | no_slots | far_date | held | booked | not_booked | cancelled | nothing_to_cancel | waitlist | handoff
    text: str                       # what code says for this outcome (always the truth)
    buttons: list[tuple[str, str]] = field(default_factory=list)

    @property
    def mutating(self) -> bool:
        return self.kind in MUTATING


class CaptureChat:
    """Stands in for the customer's chat while an existing flow runs, so its words become data instead of a message."""

    def __init__(self, to: str):
        self.to = to
        self.texts: list[str] = []
        self.buttons_: list[tuple[str, str]] = []

    async def text(self, body: str) -> None:
        self.texts.append(body)

    async def buttons(self, body: str, buttons: list[tuple[str, str]]) -> None:
        self.texts.append(body)
        self.buttons_ = list(buttons)

    @property
    def joined(self) -> str:
        return "\n\n".join(self.texts)


def _fn(name: str, properties: dict, required: list[str] | None = None) -> dict:
    return {"type": "function", "function": {"name": name, "description": "",
                                              "parameters": {"type": "object", "properties": properties, "required": required or []}}}


TOOLS = [
    _fn("find_slots",
        {"service": {"type": "string"},
         "date": {"type": "string"},
         "window": {"type": "string", "enum": ["morning", "afternoon", "evening"]},
         "time": {"type": "string"},
         "stylist": {"type": "string"}},
        ["service", "date"]),
    _fn("reserve_slot", {"slot_id": {"type": "string"}}, ["slot_id"]),
    _fn("my_bookings", {}),
    _fn("cancel_booking", {"booking_ref": {"type": "string"}}),
    _fn("join_waitlist", {"service": {"type": "string"}, "date": {"type": "string"}}, ["service", "date"]),
    _fn("hand_to_owner", {"reason": {"type": "string", "enum": list(HANDOFF)}, "note": {"type": "string"}}, ["reason"]),
]
TOOL_NAMES = {t_["function"]["name"] for t_ in TOOLS}


def _hhmm(raw) -> time | None:
    m = re.fullmatch(r"([01]?\d|2[0-3]):([0-5]\d)", str(raw or "").strip())
    return time(int(m.group(1)), int(m.group(2))) if m else None


class Toolbox:
    def __init__(self, ctx, salon: dict, client: dict, st: dict, services: list[dict], tz, now: datetime, lang: str, to: str, user_text: str):
        self.ctx, self.salon, self.client, self.services, self.tz, self.now, self.lang, self.to = ctx, salon, client, services, tz, now, lang, to
        self.user_text = user_text
        self.base_history = list(st.get("history") or [])
        self.st = {**st, "history": list(self.base_history)}
        self.offered_at_start = st.get("state") == "offered" and bool(st.get("slots"))
        self.outcomes: list[Outcome] = []
        self.facts = Facts()
        self._refs: dict[str, str] = {}
        self._reserved = False
        self._cancelled = False

    # -- helpers -----------------------------------------------------------------------------------------------------------------------------
    async def _reload(self) -> None:
        raw = await self.ctx.pool.fetchval("SELECT state FROM conversations WHERE salon_id=$1::uuid AND wa_id=$2", self.salon["id"], self.to)
        st = raw if isinstance(raw, dict) else json.loads(raw or "{}")
        st = st if isinstance(st, dict) else {}
        st["history"] = list(self.base_history)
        self.st = st

    def _work_state(self) -> dict:
        return {**self.st, "history": list(self.base_history)}

    def _service(self, name) -> dict | None:
        return bf.match_service(str(name or ""), self.services)

    def view_slots(self) -> list[dict]:
        out = []
        for i, s in enumerate(self.st.get("slots") or []):
            out.append({"slot_id": f"s{i + 1}", "when": bf.when_label(datetime.fromisoformat(s["starts_at"]), self.tz), "staff_name": s["staff_name"]})
        return out

    async def run(self, name: str, args: dict) -> dict:
        if name not in TOOL_NAMES:
            return {"error": "unknown_tool", "tools": sorted(TOOL_NAMES)}
        try:
            result = await getattr(self, "t_" + name)(args if isinstance(args, dict) else {})
        except Exception:
            log.exception("tool %s failed", name)
            result = {"error": "tool_failed", "hint": "tell the customer you could not check that just now and ask them to try again"}
        self.facts.add(json.dumps(result, ensure_ascii=False, default=str))
        return result

    # -- read and offer ----------------------------------------------------------------------------------------------------------------------
    async def t_find_slots(self, a: dict) -> dict:
        svc = self._service(a.get("service"))
        if svc is None:
            return {"error": "unknown_service", "services": [s["name"] for s in self.services]}
        try:
            day = date.fromisoformat(str(a.get("date")))
        except ValueError:
            return {"error": "bad_date", "hint": "use YYYY-MM-DD from the calendar"}
        today = self.now.date()
        if day < today:
            return {"error": "date_in_past", "today": today.isoformat()}
        if day > today + timedelta(days=60):
            self.outcomes.append(Outcome("far_date", t(self.lang, "far_date")))
            return {"error": "too_far_ahead", "hint": "bookings open up to 60 days ahead"}
        window = a.get("window") if a.get("window") in ("morning", "afternoon", "evening") else None
        at = _hhmm(a.get("time")) if a.get("time") else None
        if a.get("time") and at is None:
            return {"error": "bad_time", "hint": "use HH:MM 24-hour"}
        pool_svc = svc
        if a.get("stylist"):
            rows = await self.ctx.pool.fetch("SELECT id::text, name FROM staff WHERE id = ANY($1::uuid[]) AND active ORDER BY name", svc["staff_ids"])
            q = str(a["stylist"]).strip().lower()
            hit = [r for r in rows if r["name"].lower() == q] or [r for r in rows if q and (r["name"].lower().startswith(q) or q in r["name"].lower())]
            if not hit:
                return {"error": "unknown_stylist", "stylists_for_this_service": [r["name"] for r in rows]}
            pool_svc = {**svc, "staff_ids": [r["id"] for r in hit]}
        slots, exact = await bf.find_slots(self.ctx, self.salon, pool_svc, day, window, at, self.tz, self.now)
        if not slots:
            state = {"state": "no_slots", "service_id": svc["id"], "date": day.isoformat(), "history": list(self.base_history)}
            await bf._save(self.ctx, self.salon["id"], self.to, state)
            self.st = state
            self.outcomes.append(Outcome("no_slots", t(self.lang, "no_slots", service=svc["name"]) + "\n" + t(self.lang, "waitlist_hint")))
            return {"slots": [], "note": "nothing free in the next 14 days for this request; offer the waitlist or another day"}
        state = {"state": "offered", "service_id": svc["id"], "date": day.isoformat(), "window": window, "time": at.strftime("%H:%M") if at else None,
                 "slots": [{"staff_id": s.staff_id, "staff_name": s.staff_name, "starts_at": s.starts_at.isoformat(), "ends_at": s.ends_at.isoformat()} for s in slots],
                 "history": list(self.base_history)}
        await bf._save(self.ctx, self.salon["id"], self.to, state)
        self.st = state
        lines = "\n".join(t(self.lang, "slot_line", n=i + 1, when=bf.when_label(s.starts_at, self.tz), stylist=s.staff_name) for i, s in enumerate(slots))
        body = t(self.lang, "offer", service=svc["name"], note="" if exact else t(self.lang, "nearest"), lines=lines)
        buttons = [(f"slot:{i}", bf.when_label(s.starts_at, self.tz).replace(",", "")[:20]) for i, s in enumerate(slots)]
        self.outcomes.append(Outcome("offered", body, buttons))
        return {"service": svc["name"], "slots": self.view_slots(), "exact_match": exact,
                "note": "" if exact else "these are the closest alternatives to what was asked"}

    async def t_my_bookings(self, a: dict) -> dict:
        rows = await self.ctx.pool.fetch(
            "SELECT b.id::text, b.status, b.starts_at, sv.name AS service, st.name AS stylist, "
            "EXISTS (SELECT 1 FROM payments p WHERE p.booking_id=b.id AND p.status='paid') AS deposit_paid "
            "FROM bookings b JOIN services sv ON sv.id=b.service_id JOIN staff st ON st.id=b.staff_id "
            "WHERE b.client_id=$1::uuid AND b.status IN ('held','confirmed') AND b.starts_at > now() ORDER BY b.starts_at LIMIT 5", self.client["id"])
        self._refs = {f"b{i + 1}": r["id"] for i, r in enumerate(rows)}
        return {"bookings": [{"booking_ref": f"b{i + 1}", "service": r["service"], "when": bf.when_label(r["starts_at"], self.tz), "stylist": r["stylist"],
                              "status": r["status"], "deposit_paid": bool(r["deposit_paid"])} for i, r in enumerate(rows)]}

    # -- changes (their words come from code) -------------------------------------------------------------------------------------------------
    async def t_reserve_slot(self, a: dict) -> dict:
        if self._reserved:
            return {"error": "already_handled_this_turn"}
        if not self.offered_at_start:
            return {"error": "customer_has_not_picked", "hint": "slots must be offered first; present them and let the customer choose"}
        m = re.fullmatch(r"s([1-3])", str(a.get("slot_id") or "").strip().lower())
        slots = self.st.get("slots") or []
        if not m or int(m.group(1)) > len(slots):
            return {"error": "unknown_slot_id", "valid": [v["slot_id"] for v in self.view_slots()]}
        self._reserved = True
        choice = int(m.group(1))
        pick = slots[choice - 1]
        cap = CaptureChat(self.to)
        await bf._choose(self.ctx, self.salon, self.client, self._work_state(), self.services, self.tz, self.now, cap, self.lang, choice)
        await self._reload()
        service = next((s for s in self.services if s["id"] == (self.st.get("service_id") or "")), None)
        if self.st.get("state") == "held":
            amount = int(self.st.get("amount") or 0)
            self.facts.prices.add(amount)
            self.facts.held = True
            self.outcomes.append(Outcome("held", cap.joined))
            return {"status": "held", "service": self.st.get("service_name"), "when": self.st.get("held_when"), "stylist": self.st.get("held_stylist"),
                    "deposit_kes": amount, "not_yet_confirmed": True, "note": "the system now sends the payment steps; do not describe them"}
        if self.st.get("state") == "offered":
            self.outcomes.append(Outcome("offered", cap.joined, cap.buttons_))
            return {"status": "slot_taken", "slots": self.view_slots(), "note": "someone else took it; these are the nearest alternatives"}
        booked = await self.ctx.pool.fetchval(
            "SELECT status FROM bookings WHERE client_id=$1::uuid AND staff_id=$2::uuid AND starts_at=$3 AND status IN ('held','confirmed') ORDER BY created_at DESC LIMIT 1",
            self.client["id"], pick["staff_id"], datetime.fromisoformat(pick["starts_at"]))
        if booked == "confirmed":
            self.facts.booked = True
            self.outcomes.append(Outcome("booked", cap.joined))
            return {"status": "confirmed", "service": service["name"] if service else None,
                    "when": bf.when_label(datetime.fromisoformat(pick["starts_at"]), self.tz), "stylist": pick["staff_name"]}
        self.outcomes.append(Outcome("not_booked", cap.joined))
        return {"status": "not_booked", "note": "the booking could not be made; the system explains why"}

    async def t_cancel_booking(self, a: dict) -> dict:
        if self._cancelled:
            return {"error": "already_handled_this_turn"}
        if not self._refs:
            await self.t_my_bookings({})
        ref = str(a.get("booking_ref") or "").strip().lower()
        if ref:
            bid = self._refs.get(ref)
            if bid is None:
                return {"error": "unknown_booking_ref", "valid": sorted(self._refs)}
        elif len(self._refs) == 1:
            bid = next(iter(self._refs.values()))
        elif not self._refs:
            self.outcomes.append(Outcome("nothing_to_cancel", t(self.lang, "nothing_to_cancel")))
            return {"status": "nothing_to_cancel"}
        else:
            return {"error": "which_booking", "hint": "ask which one, listing them", "valid": sorted(self._refs)}
        self._cancelled = True
        cap = CaptureChat(self.to)
        await bf._cancel(self.ctx, self.salon, self.client, self._work_state(), self.services, self.tz, self.now, cap, self.lang, booking_id=bid)
        await self._reload()
        if cap.joined == t(self.lang, "nothing_to_cancel"):
            self.outcomes.append(Outcome("nothing_to_cancel", cap.joined))
            return {"status": "nothing_to_cancel"}
        self.facts.cancelled = True
        self.outcomes.append(Outcome("cancelled", cap.joined))
        return {"status": "cancelled", "note": "the system sends the cancellation details and any refund note"}

    async def t_join_waitlist(self, a: dict) -> dict:
        svc = self._service(a.get("service"))
        if svc is None:
            return {"error": "unknown_service", "services": [s["name"] for s in self.services]}
        try:
            day = date.fromisoformat(str(a.get("date")))
        except ValueError:
            return {"error": "bad_date"}
        if not self.now.date() <= day <= self.now.date() + timedelta(days=60):
            return {"error": "date_out_of_range"}
        outcome = await waitlist.join(self.ctx.pool, self.salon["id"], self.client["id"], svc["id"], day)
        key = "waitlist_joined" if outcome == "joined" else "waitlist_already"
        self.outcomes.append(Outcome("waitlist", t(self.lang, key, service=svc["name"], day=day.isoformat())))
        return {"status": outcome}

    async def t_hand_to_owner(self, a: dict) -> dict:
        cat = HANDOFF.get(str(a.get("reason")), "default")
        await guard.hand_off(self.ctx, self.salon, self.client, self.lang, cat, self.user_text)
        self.st = {"state": "handoff", "category": cat, "history": list(self.base_history)}
        self.outcomes.append(Outcome("handoff", guard.reply_for(cat, self.lang)))
        return {"status": "owner_notified", "note": "the system tells the customer the owner will reply"}
