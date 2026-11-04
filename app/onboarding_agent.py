"""Model-assisted business registration with evidence-backed field capture.

The model handles natural conversation and identifies values in the owner's
message. This module validates every extracted value against that message and
the existing onboarding parsers. Only the deterministic onboarding code can
create the salon, and it still requires an explicit final confirmation.
"""
from __future__ import annotations

import copy
import json
import logging
import re
from datetime import datetime
from zoneinfo import ZoneInfo

from app import guard
from app.agent import drive, enabled, policy as agent_policy
from app.agent.client import AgentUnavailable, request_evidence
from app.agent.grounding import Facts, _times, check
from app.channels import outbound
from app.channels.whatsapp import mask
from app.messages import ensure_help_hint

from . import onboarding_chat as ob, smalltalk

log = logging.getLogger("rembo.onboarding_agent")


def _fn(name: str, properties: dict, required: list[str] | None = None) -> dict:
    return {
        "type": "function",
        "function": {
            "name": name,
            "description": "",
            "parameters": {
                "type": "object",
                "properties": properties,
                "required": required or [],
            },
        },
    }


_EVIDENCE = {"type": "string"}
_OWNER = {
    "type": "object",
    "properties": {"name": {"type": "string"}, "evidence": _EVIDENCE},
    "required": ["name", "evidence"],
}
_SERVICE = {
    "type": "object",
    "properties": {
        "name": {"type": "string"},
        "price_kes": {"type": "integer"},
        "duration_minutes": {"type": "integer"},
        "evidence": _EVIDENCE,
    },
    "required": ["name", "price_kes", "evidence"],
}

TOOLS = [
    _fn(
        "capture_registration_details",
        {
            "salon_name": {
                "type": "object",
                "properties": {"name": {"type": "string"}, "evidence": _EVIDENCE},
                "required": ["name", "evidence"],
            },
            "business_type_evidence": _EVIDENCE,
            "owners": {"type": "array", "items": _OWNER},
            "services": {"type": "array", "items": _SERVICE},
            "durations_evidence": {"type": "array", "items": _EVIDENCE},
            "hours_evidence": _EVIDENCE,
            "deposit_evidence": _EVIDENCE,
            "payment_method_evidence": _EVIDENCE,
            "payment_type_evidence": _EVIDENCE,
            "payment_destination_evidence": _EVIDENCE,
            "name_confirmation_evidence": _EVIDENCE,
            "services_confirmation_evidence": _EVIDENCE,
            "duration_defaults_accepted_evidence": _EVIDENCE,
        },
    )
]

_NUMBER = re.compile(r"\d[\d,]*")
_WORD_NUMBER_TOKENS = re.compile(r"[a-z]+|\d[\d,]*", re.I)
_SPOKEN_VALUES = {
    "zero": 0, "sifuri": 0,
    "one": 1, "moja": 1, "two": 2, "mbili": 2, "three": 3, "tatu": 3,
    "four": 4, "nne": 4, "five": 5, "tano": 5, "six": 6, "mvili": 2,
    "seven": 7, "saba": 7, "eight": 8, "nane": 8, "nine": 9, "tisa": 9,
    "ten": 10, "kumi": 10, "eleven": 11, "twelve": 12, "thirteen": 13,
    "fourteen": 14, "fifteen": 15, "sixteen": 16, "seventeen": 17,
    "eighteen": 18, "nineteen": 19,
    "twenty": 20, "ishirini": 20, "thirty": 30, "thelathini": 30,
    "forty": 40, "arobaini": 40, "fifty": 50, "hamsini": 50,
    "sixty": 60, "sitini": 60, "seventy": 70, "sabini": 70,
    "eighty": 80, "themanini": 80, "ninety": 90, "tisini": 90,
}
_AMOUNT_FILLERS = {
    "a", "an", "the", "for", "at", "is", "are", "i", "we",
    "charge", "charges", "cost", "costs", "price", "priced", "only", "just", "each",
    "kwa", "ya", "bei", "gharama", "kes", "ksh", "shilling", "shillings", "shilingi",
}
_DURATION = re.compile(r"\b(\d+)\s*(hours?|hrs?|h|minutes?|mins?|m)\b", re.I)
_TYPE = {
    "1": "kinyozi", "barber": "kinyozi", "barber shop": "kinyozi", "barbershop": "kinyozi", "kinyozi": "kinyozi",
    "2": "salon", "saloon": "salon", "saluni": "salon", "beauty": "salon", "beauty salon": "salon", "hair salon": "salon",
}


def _business_type(evidence: str) -> str | None:
    text = " ".join((evidence or "").casefold().split()).strip(" \t\n\"'`“”‘’.,!?;:")
    text = re.sub(r"^(?:(?:i|we)\s+(?:am|are|own|run|have|operate)|it\s+is|it's|we're)\s+", "", text)
    text = re.sub(r"^(?:a|an|the|my|our)\s+", "", text)
    return _TYPE.get(text.strip(" \t\n\"'`“”‘’.,!?;:"))


def _compact(text: str) -> str:
    return " ".join((text or "").split()).casefold()


def _has_evidence(source: str, quote) -> bool:
    return isinstance(quote, str) and bool(quote.strip()) and _compact(quote) in _compact(source)


def _int(value) -> int | None:
    return value if isinstance(value, int) and not isinstance(value, bool) else None


def _numbers(text: str) -> set[int]:
    return {int(n.replace(",", "")) for n in _NUMBER.findall(text or "")}


def _spoken_amount(text: str, subject: str | None = None) -> int | None:
    """Read one clearly spoken English or Swahili amount, optionally after removing a service name."""
    tokens = [token.casefold() for token in _WORD_NUMBER_TOKENS.findall(text or "")]
    if subject:
        name = [token.casefold() for token in _WORD_NUMBER_TOKENS.findall(subject)]
        if not name:
            return None
        kept = []
        index = 0
        found = False
        while index < len(tokens):
            if tokens[index:index + len(name)] == name:
                index += len(name)
                found = True
            else:
                kept.append(tokens[index])
                index += 1
        if not found:
            return None
        tokens = kept
    # In Swahili, the multiplier follows the scale ("mia moja" = 100).
    swahili_multipliers = {"moja", "mbili", "mvili", "tatu", "nne", "tano", "sita", "saba", "nane", "tisa", "kumi"}
    for index in range(len(tokens) - 1):
        if tokens[index] in {"mia", "elfu"} and tokens[index + 1] in swahili_multipliers:
            tokens[index], tokens[index + 1] = tokens[index + 1], tokens[index]
    tokens = [token for token in tokens if token not in _AMOUNT_FILLERS]
    if not tokens:
        return None

    total = group = 0
    last = None
    had_number = False
    for token in tokens:
        if token in {"and", "na"}:
            if last in (None, "connector"):
                return None
            last = "connector"
            continue
        if token in {"hundred", "mia"}:
            if last in ("unit", "tens"):
                base = group
            elif last in (None, "thousand"):
                base = 1
            else:
                return None
            if base > 99:
                return None
            group = base * 100
            last = "hundred"
            had_number = True
            continue
        if token in {"thousand", "elfu"}:
            if last in ("unit", "tens", "hundred"):
                base = group
            elif last is None:
                base = 1
            else:
                return None
            total += base * 1000
            group = 0
            last = "thousand"
            had_number = True
            continue

        if token.isdigit() or re.fullmatch(r"\d[\d,]*", token):
            try:
                value = int(token.replace(",", ""))
            except ValueError:
                return None
            if last not in (None, "thousand"):
                return None
            group += value
            last = "unit"
            had_number = True
            continue

        value = _SPOKEN_VALUES.get(token)
        if value is None:
            return None
        if value >= 20:
            if last not in (None, "hundred", "thousand", "connector"):
                return None
            group += value
            last = "tens"
        elif last == "tens" and 0 < value < 10:
            group += value
            last = "unit"
        elif last == "connector" and (group >= 10 or total > 0) and value < 20:
            group += value
            last = "unit"
        elif last in (None, "hundred", "thousand"):
            group += value
            last = "unit"
        else:
            return None
        had_number = True

    amount = total + group
    return amount if had_number and 0 <= amount <= ob.MAX_PRICE else None


def _price_is_evidenced(price: int | None, evidence: str, service_name: str) -> bool:
    return price is not None and (price in _numbers(evidence) or price == _spoken_amount(evidence, service_name))


def _standalone_spoken_amount(text: str) -> int | None:
    """Parse a price-only answer while registration is explicitly waiting for that price."""
    return _spoken_amount(text)


def _duration_is_evidenced(minutes: int, text: str) -> bool:
    parsed = ob.parse_service_line(text)
    if parsed and parsed["explicit"] and parsed["minutes"] == minutes:
        return True
    for match in _DURATION.finditer(text or ""):
        amount = int(match.group(1))
        unit = match.group(2).lower()
        actual = amount * 60 if unit.startswith("h") else amount
        if actual == minutes:
            return True
    return False


def _state_summary(state: dict) -> str:
    data = state.get("data") or {}
    safe = {
        "salon_name": data.get("salon"),
        "business_type": data.get("kind"),
        "owners": data.get("owners", []),
        "owner_count": len(data.get("owners", [])),
        "services": [
            {"name": s.get("name"), "price_kes": s.get("price"), "duration_minutes": s.get("minutes"),
             "duration_was_stated": bool(s.get("explicit"))}
            for s in data.get("services", [])
        ],
        "service_count": len(data.get("services", [])),
        "pending_service_name": (state.get("pending_service") or {}).get("name")
        if isinstance(state.get("pending_service"), dict) else None,
        "opening_hours": {"opens": data.get("open"), "closes": data.get("close")},
        "deposit_kes": data.get("deposit"),
        "payment_method": data.get("payment_method"),
        "payment_type": data.get("payment_type"),
        "payment_destination": data.get("payment_destination"),
        "current_step": state.get("step", "ask_name"),
        "possible_duplicate": state.get("twin") if state.get("step") == "confirm_name" else None,
    }
    return json.dumps(safe, ensure_ascii=False)


def _registration_reply_problems(reply: str, facts: Facts, policy: dict) -> list[str]:
    """Also reject unsupported bare numbers; registration replies have no need to invent them."""
    problems = check(reply, facts)
    for rule in ("unsupported_promise", "premature_creation"):
        issue = agent_policy.reply_check(policy, rule, reply)
        if issue:
            problems.append(issue)
    known = {int(n.replace(",", "")) for n in _NUMBER.findall(facts.blob())}
    for value in _times(facts.blob_with_user()):
        hour, minute = (int(part) for part in value.split(":"))
        known.update((hour, hour % 12 or 12, minute))
    missing = {
        int(match.group().replace(",", ""))
        for match in _NUMBER.finditer(reply or "")
        if int(match.group().replace(",", "")) not in known
    }
    if missing:
        problems.extend(f"number {number} was not in the registration facts" for number in sorted(missing))
    return sorted(set(problems))


def _agent_reply_problems(
    reply: str, facts: Facts, accepted: list[str], policy: dict, step: str | None = None
) -> list[str]:
    problems = _registration_reply_problems(reply, facts, policy)
    checks = ["generic_multi_detail_invite", "type_both", "per_service_deposit", "confirm_duration_ask"]
    if not (set(accepted) - {"name_rejected"}):
        checks.append("unsupported_capture_claim")
    checks.append("premature_registration_fields")
    for rule in checks:
        issue = agent_policy.reply_check(policy, rule, reply, step)
        if issue:
            problems.append(issue)
    return sorted(set(problems))


def _prompt(state: dict, lang: str, policy: dict) -> str:
    summary = _state_summary(state)
    registration = policy["registration"]
    instructions = {key: value for key, value in registration.items() if key != "reply_checks"}
    step = state.get("step", "ask_name")
    history = state.get("agent_history")
    first_reply = not state.get("help_hint_sent") and not (
        isinstance(history, list) and any(item.get("role") == "assistant" for item in history if isinstance(item, dict))
    )
    first_reply_rule = (
        f'This is the first reply in this registration chat. End with this short sentence: "{ob.t(lang, "help_hint")}"'
        if first_reply else "The owner has already received the help invitation; do not repeat it unless relevant."
    )
    step_instruction = registration["step_instructions"].get(step, "Follow the current registration step.")
    if step == "ask_prices" and isinstance(state.get("pending_service"), dict):
        step_instruction += " A service name is already recorded without a validated price. Ask only for that service's price; do not ask for its name again."
    return f"""{registration['identity']}
{registration['purpose']}
{"\n".join(policy["prompt_wrapper"])}
Current language hint: {lang}.
First-reply guidance: {first_reply_rule}
Current validated registration details (JSON):
{summary}
Current step: {step}
Instruction for this step: {step_instruction}
Configured registration policy (JSON):
{json.dumps(instructions, ensure_ascii=False, indent=2)}
"""


class RegistrationToolbox:
    def __init__(self, ctx, sender: str, text: str, state: dict):
        self.ctx, self.sender, self.text, self.state = ctx, sender, text, state
        self.facts = Facts()
        self.outcomes = []  # shared drive() interface
        self.changed = False
        self.accepted: list[str] = []
        self.rejected: list[str] = []
        self._was_final = state.get("step") == "confirm_all"
        self._captured = False
        step = state.get("step", "ask_name")
        if step in ("ask_durations", "ask_hours", "ask_deposit", "ask_payment_method", "ask_till_number",
                    "ask_phone_type", "ask_payment_phone", "confirm_all") and "services_confirmed" not in state:
            state["services_confirmed"] = True
            self.changed = True
        if step in ("ask_hours", "ask_deposit", "ask_payment_method", "ask_till_number", "ask_phone_type",
                    "ask_payment_phone", "confirm_all") and "duration_defaults_accepted" not in state:
            state["duration_defaults_accepted"] = True
            self.changed = True

    def _evidence(self, args: dict, key: str) -> str | None:
        value = args.get(key)
        if not _has_evidence(self.text, value):
            if value is not None:
                self.rejected.append(key.removesuffix("_evidence"))
            return None
        return value.strip()

    async def run(self, name: str, args: dict) -> dict:
        if name != "capture_registration_details":
            return {"error": "unknown_tool"}
        if self._captured:
            return {"error": "capture_already_used", "hint": "finish this turn using the validated result"}
        self._captured = True
        try:
            result = await self._capture(args if isinstance(args, dict) else {})
        except Exception:
            log.exception("registration detail capture failed")
            result = {"error": "capture_failed"}
        for service in self.state.get("data", {}).get("services", []):
            minutes = service.get("minutes") if isinstance(service, dict) else None
            if isinstance(minutes, int) and minutes > 0 and minutes % 60 == 0:
                self.facts.add(f"Duration equivalent: {minutes // 60} hours")
        packed = json.dumps(result, ensure_ascii=False)
        self.facts.add(packed)
        return result

    async def _capture(self, args: dict) -> dict:
        state = self.state
        data = state.setdefault("data", {})
        prior_step = state.get("step", "ask_name")

        # A duplicate-name decision applies only after the application raised it.
        confirmation = self._evidence(args, "name_confirmation_evidence")
        if prior_step == "confirm_name" and confirmation and ob.is_affirmative(confirmation):
            state.pop("twin", None)
            state["step"] = "ask_type"
            state["stack"] = ["ask_name"]
            self.accepted.append("name_confirmation")
            self.changed = True
        elif prior_step == "confirm_name" and confirmation and ob.is_negative(confirmation):
            data.pop("salon", None)
            state.pop("twin", None)
            state["step"] = "ask_name"
            self.accepted.append("name_rejected")
            self.changed = True

        name_item = args.get("salon_name")
        if name_item is not None:
            name = name_item.get("name") if isinstance(name_item, dict) else None
            name_quote = name_item.get("evidence") if isinstance(name_item, dict) else None
            valid = _has_evidence(self.text, name_quote) and isinstance(name, str) and _compact(name) in _compact(name_quote)
            name = " ".join(name.split()).strip(" \t\n.,!?\"'`“”‘’") if isinstance(name, str) else ""
            if (valid and ob.MIN_NAME <= len(name) <= ob.MAX_NAME
                    and not ob.registration_start_only(name) and not ob.looks_off_topic(name)
                    and not ob.looks_like_name_sentence(name)):
                data["salon"] = name
                state.pop("twin", None)
                twin = await ob.similar_salon(self.ctx.pool, name)
                if twin:
                    state["twin"] = twin
                    state["step"] = "confirm_name"
                self.accepted.append("salon_name")
                self.changed = True
            else:
                self.rejected.append("salon_name")

        kind_quote = self._evidence(args, "business_type_evidence")
        if kind_quote:
            kind = _business_type(kind_quote)
            if kind:
                data["kind"] = kind
                self.accepted.append("business_type")
                self.changed = True
            else:
                self.rejected.append("business_type")

        owner_items = args.get("owners")
        if owner_items is not None:
            if not isinstance(owner_items, list):
                self.rejected.append("owners")
            elif len(owner_items) > ob.MAX_OWNERS:
                self.rejected.append("owners")
            else:
                parsed_owners: list[str] = []
                for item in owner_items:
                    if not isinstance(item, dict):
                        self.rejected.append("owners")
                        continue
                    evidence = item.get("evidence")
                    name = item.get("name")
                    valid = _has_evidence(self.text, evidence) and isinstance(name, str) and _compact(name) in _compact(evidence)
                    one = ob.parse_owners(name) if valid else None
                    if one and len(one) == 1:
                        parsed_owners.append(one[0])
                    else:
                        self.rejected.append("owners")
                if parsed_owners:
                    # This field is a complete list when first supplied; later names are merged without duplicates.
                    existing = data.get("owners", []) if prior_step not in ("ask_owner", "ask_name") else []
                    combined = list(existing)
                    for person in parsed_owners:
                        if person.casefold() not in {x.casefold() for x in combined}:
                            combined.append(person)
                    if len(combined) <= ob.MAX_OWNERS:
                        data["owners"] = combined
                        self.accepted.append("owners")
                        self.changed = True
                    else:
                        self.rejected.append("owners")

        service_items = args.get("services")
        lines = []
        original_names = {
            s["name"].casefold() for s in data.get("services", [])
            if isinstance(s, dict) and isinstance(s.get("name"), str)
        }
        added_without_duration: set[str] = set()
        pending_candidates: list[dict] = []
        pending_service = state.get("pending_service")
        pending_name = pending_service.get("name") if isinstance(pending_service, dict) else None
        pending_resolved = False
        if service_items is not None:
            if not isinstance(service_items, list):
                self.rejected.append("services")
            elif len(service_items) > ob.MAX_SERVICES:
                self.rejected.append("services")
            else:
                for item in service_items:
                    if not isinstance(item, dict):
                        self.rejected.append("services")
                        continue
                    evidence = item.get("evidence")
                    service_name = item.get("name")
                    price = _int(item.get("price_kes"))
                    duration = item.get("duration_minutes")
                    has_duration = duration is not None
                    name_valid = (
                        _has_evidence(self.text, evidence)
                        and isinstance(service_name, str)
                        and ob.MIN_NAME <= len(service_name.strip()) <= ob.MAX_NAME
                        and _compact(service_name) in _compact(evidence)
                    )
                    spoken_price = _spoken_amount(evidence, service_name) if name_valid else None
                    if spoken_price is not None:
                        # Independently parse a complete spoken amount from the exact quoted evidence.
                        price = spoken_price
                    price_evidenced = name_valid and _price_is_evidenced(price, evidence or "", service_name)
                    valid = name_valid and price_evidenced
                    if has_duration:
                        valid = valid and _int(duration) is not None and 5 <= duration <= 480 and _duration_is_evidenced(duration, evidence or "")
                    if not valid:
                        self.rejected.append("services")
                        if name_valid and not price_evidenced:
                            normalized_name = " ".join(service_name.split()).strip()
                            pending_candidates.append({"name": normalized_name, "evidence": evidence.strip()})
                        continue
                    suffix = f" {duration}" if has_duration else ""
                    lines.append(f"{service_name.strip()}: {price}{suffix}")
                    if pending_name and service_name.casefold() == pending_name.casefold():
                        pending_resolved = True
                    if not has_duration and service_name.casefold() not in original_names:
                        added_without_duration.add(service_name.casefold())

        if pending_name and prior_step == "ask_prices" and not pending_resolved:
            reply_price = _standalone_spoken_amount(self.text)
            if reply_price is not None and ob.MIN_PRICE <= reply_price <= ob.MAX_PRICE:
                lines.append(f"{pending_name}: {reply_price}")
                added_without_duration.add(pending_name.casefold())
                pending_resolved = True

        if lines:
            candidate = copy.deepcopy(data)
            _updated, _added, bad = ob.apply_corrections(candidate, "\n".join(lines))
            if bad or len(candidate.get("services", [])) > ob.MAX_SERVICES:
                self.rejected.append("services")
            else:
                data["services"] = candidate["services"]
                self.facts.prices.update(int(s["price"]) for s in candidate["services"] if isinstance(s.get("price"), int))
                if not self._was_final:
                    state["services_confirmed"] = False
                if added_without_duration:
                    state["duration_defaults_accepted"] = False
                    if self._was_final:
                        state["services_confirmed"] = True
                self.accepted.append("services")
                self.changed = True
                if pending_resolved:
                    state.pop("pending_service", None)

        if pending_candidates:
            distinct = {item["name"].casefold(): item for item in pending_candidates}
            if len(distinct) == 1 and (not pending_name or next(iter(distinct)).casefold() == pending_name.casefold()):
                state["pending_service"] = next(iter(distinct.values()))
                self.changed = True

        duration_quotes = args.get("durations_evidence")
        if duration_quotes is not None:
            if not isinstance(duration_quotes, list):
                self.rejected.append("durations")
            else:
                valid_quotes = [q.strip() for q in duration_quotes[:ob.MAX_SERVICES] if _has_evidence(self.text, q)]
                if len(valid_quotes) != len(duration_quotes[:ob.MAX_SERVICES]):
                    self.rejected.append("durations")
                if valid_quotes and data.get("services"):
                    fixed, bad = ob.apply_durations(data["services"], "\n".join(valid_quotes))
                    if fixed:
                        self.accepted.append("durations")
                        self.changed = True
                    if bad:
                        self.rejected.append("durations")

        hours_quote = self._evidence(args, "hours_evidence")
        if hours_quote:
            pair = ob.parse_time_pair(hours_quote)
            if pair:
                data["open"], data["close"] = pair[0].strftime("%H:%M"), pair[1].strftime("%H:%M")
                self.accepted.append("hours")
                self.changed = True
            else:
                self.rejected.append("hours")

        deposit_quote = self._evidence(args, "deposit_evidence")
        if deposit_quote:
            amount = ob.parse_deposit(deposit_quote)
            source_numbers = [int(value.replace(",", "")) for value in _NUMBER.findall(self.text)]
            labelled_deposits = ob.deposit_amount_mentions(self.text)
            if len(labelled_deposits) > 1 or (labelled_deposits and amount not in labelled_deposits):
                amount = None
            elif len(source_numbers) > 1 and not labelled_deposits:
                amount = None
            elif (len(source_numbers) > 1 and len(labelled_deposits) == 1
                  and re.search(r"\b(?:or|either|versus)\b", self.text, re.I)):
                amount = None
            if amount is not None:
                data["deposit"] = amount
                self.facts.prices.add(amount)
                if amount == 0:
                    self.facts.deposit_none = True
                self.accepted.append("deposit")
                self.changed = True
            else:
                self.rejected.append("deposit")

        method_quote = self._evidence(args, "payment_method_evidence")
        if method_quote:
            method = ob.parse_payment_method(method_quote)
            if method:
                if method != data.get("payment_method"):
                    data.pop("payment_destination", None)
                    data.pop("payment_type", None)
                data["payment_method"] = method
                self.accepted.append("payment_method")
                self.changed = True
            else:
                self.rejected.append("payment_method")

        type_quote = self._evidence(args, "payment_type_evidence")
        if type_quote:
            payment_type = ob.parse_payment_phone_type(type_quote)
            if payment_type:
                if payment_type != data.get("payment_type"):
                    data.pop("payment_destination", None)
                data["payment_type"] = payment_type
                self.accepted.append("payment_type")
                self.changed = True
            else:
                self.rejected.append("payment_type")

        destination_quote = self._evidence(args, "payment_destination_evidence")
        if destination_quote:
            if data.get("payment_method") == "till":
                destination = ob.parse_manual_till(destination_quote)
            elif data.get("payment_method") == "phone" and data.get("payment_type") in ("pochi", "send_money", "airtel_money"):
                destination = ob.parse_payment_phone(destination_quote)
            else:
                destination = None
            if destination:
                data["payment_destination"] = destination
                self.accepted.append("payment_destination")
                self.changed = True
            else:
                self.rejected.append("payment_destination")

        services_confirmation = self._evidence(args, "services_confirmation_evidence")
        if prior_step == "confirm_services" and services_confirmation and ob.is_affirmative(services_confirmation):
            state["services_confirmed"] = True
            self.accepted.append("services_confirmation")
            self.changed = True

        duration_acceptance = self._evidence(args, "duration_defaults_accepted_evidence")
        awaiting_duration = prior_step == "ask_durations" or (
            prior_step == "confirm_services" and ob.awaiting_duration_approval(state))
        if awaiting_duration and ob.is_duration_approval(self.text):
            # The full current message is exact evidence even if the model failed to put it in the dedicated field.
            duration_acceptance = self.text
        if awaiting_duration and duration_acceptance and ob.is_duration_approval(duration_acceptance):
            for service in data.get("services", []):
                service["explicit"] = True
            state["duration_defaults_accepted"] = True
            self.accepted.append("duration_defaults")
            if prior_step == "confirm_services":
                state["services_confirmed"] = True
                self.accepted.append("services_confirmation")
            self.changed = True

        self._advance_step(prior_step)
        if self.changed:
            await ob._save_session(self.ctx.pool, self.sender, state)

        return {
            "accepted": sorted(set(self.accepted)),
            "needs_clarification": sorted(set(self.rejected)),
            "next_step": state.get("step", "ask_name"),
            "validated_draft": json.loads(_state_summary(state)),
        }

    def _advance_step(self, previous: str) -> None:
        state, data = self.state, self.state.get("data", {})
        if state.get("step") == "confirm_name" and state.get("twin"):
            return
        if previous == "confirm_name" and state.get("step") == "ask_name" and not data.get("salon"):
            return
        if isinstance(state.get("pending_service"), dict):
            state["step"] = "ask_prices"
            return
        if self._was_final and ob.registration_complete(state):
            state["step"] = "confirm_all"
            return
        if not data.get("salon"):
            state["step"] = "ask_name"
        elif not data.get("kind"):
            state["step"] = "ask_type"
        elif not data.get("owners"):
            state["step"] = "ask_owner"
        elif not data.get("services"):
            state["step"] = "ask_prices"
        elif not state.get("services_confirmed"):
            state["step"] = "confirm_services"
        elif not all(s.get("explicit") for s in data.get("services", [])) and not state.get("duration_defaults_accepted"):
            state["step"] = "ask_durations"
        elif not data.get("open") or not data.get("close"):
            state["step"] = "ask_hours"
        elif "deposit" not in data:
            state["step"] = "ask_deposit"
        elif int(data.get("deposit") or 0) > 0 and data.get("payment_method") not in ("till", "phone"):
            state["step"] = "ask_payment_method"
        elif int(data.get("deposit") or 0) > 0 and data.get("payment_method") == "till" and ob.parse_manual_till(data.get("payment_destination", "")) is None:
            state["step"] = "ask_till_number"
        elif int(data.get("deposit") or 0) > 0 and data.get("payment_method") == "phone" and data.get("payment_type") not in ("pochi", "send_money", "airtel_money"):
            state["step"] = "ask_phone_type"
        elif int(data.get("deposit") or 0) > 0 and data.get("payment_method") == "phone" and ob.parse_payment_phone(data.get("payment_destination", "")) is None:
            state["step"] = "ask_payment_phone"
        else:
            state["step"] = "confirm_all"


def _history(state: dict, text: str) -> list[dict]:
    history = state.get("agent_history")
    history = history if isinstance(history, list) else []
    messages = [{"role": "assistant" if item.get("role") == "assistant" else "user", "content": str(item.get("text", ""))[:500]}
                for item in history[-10:] if isinstance(item, dict) and str(item.get("text", "")).strip()]
    while messages and messages[0]["role"] != "user":
        messages.pop(0)
    messages.append({"role": "user", "content": (text or "")[:1000]})
    return messages[-11:]


async def _trace(ctx, *, mid, sender, text, steps, final_text, state_after, outcome, grounded, problems, model, calls, ms, model_requests=None):
    try:
        await ctx.pool.execute(
            "INSERT INTO agent_turns (message_id, salon_id, sender, user_text, steps, final_text, state_after, outcome, grounded, problems, backend, model, model_calls, ms, model_requests) "
            "VALUES ($1,NULL,$2,$3,$4::jsonb,$5,$6::jsonb,$7,$8,$9::jsonb,$10,$11,$12,$13,$14::jsonb) "
            "ON CONFLICT (message_id) DO UPDATE SET steps=EXCLUDED.steps, final_text=EXCLUDED.final_text, state_after=EXCLUDED.state_after, "
            "outcome=EXCLUDED.outcome, grounded=EXCLUDED.grounded, problems=EXCLUDED.problems, model_calls=EXCLUDED.model_calls, ms=EXCLUDED.ms, model_requests=EXCLUDED.model_requests",
            mid, mask(sender), (text or "")[:500], json.dumps(steps, ensure_ascii=False, default=str), final_text,
            json.dumps(state_after, ensure_ascii=False, default=str) if state_after else None, outcome, grounded,
            json.dumps(problems), model[0] if model else None, model[1] if model else None, calls, ms,
            json.dumps(model_requests or [], ensure_ascii=False, default=str),
        )
    except Exception:
        log.exception("could not write the registration agent trace")


async def _answer_help(ctx, sender: str, pid: str, words: str, state: dict, mid) -> bool:
    """Let the model explain the current setup step without giving it capture tools."""
    lang = smalltalk.language(words, state.get("lang", "en"))
    try:
        policy = agent_policy.load_policy(getattr(ctx.settings, "agent_policy_path", None))
        system = _prompt(state, lang, policy)
        facts = Facts()
        facts.add(system, _state_summary(state))
        history = state.get("agent_history")
        if isinstance(history, list):
            facts.add(*(row.get("text", "") for row in history if isinstance(row, dict) and row.get("role") == "assistant"))
            facts.add_user(*(row.get("text", "") for row in history if isinstance(row, dict) and row.get("role") == "user"))
        facts.add_user(words)
        await ctx.wa.typing(sender)
        step = await ctx.agent.step(system, _history(state, words), [])
    except Exception:
        log.exception("registration help explanation failed; using the step guide")
        return False

    body = (step.text or "").strip()
    problems = _registration_reply_problems(body, facts, policy) if body else ["empty help explanation"]
    if not body or len(body) > 500 or problems:
        log.warning("registration help explanation rejected: %s", "; ".join(problems))
        return False

    first_reply = not state.get("help_hint_sent") and not any(
        row.get("role") == "assistant" for row in (state.get("agent_history") or []) if isinstance(row, dict)
    )
    if first_reply:
        body = ensure_help_hint(body, lang)
    state["lang"] = lang
    state["help_hint_sent"] = True
    stamp = datetime.now(ZoneInfo("UTC")).isoformat()
    saved_history = list(state.get("agent_history") or [])
    saved_history.extend([
        {"role": "user", "text": words[:500], "ts": stamp},
        {"role": "assistant", "text": body[:500], "ts": stamp},
    ])
    state["agent_history"] = saved_history[-guard.MAX_HISTORY * 2:]
    await ob._save_session(ctx.pool, sender, state)
    await _trace(ctx, mid=mid, sender=sender, text=words, steps=[{
        "kind": "help_explanation", "accepted": True, "backend": step.backend, "model": step.model, "ms": step.ms,
    }], final_text=body, state_after=state, outcome="registration_help", grounded=True,
       problems=[], model=(step.backend, step.model), calls=1, ms=step.ms,
       model_requests=[request_evidence(step)])
    await ob._say(ctx, pid, sender, body)
    return True


async def advance(ctx, sender: str, pid: str, text: str, state: dict) -> bool:
    """Handle one registration turn; False asks the established deterministic flow to handle it."""
    if not enabled(ctx):
        return False
    mid = outbound.current_message_id()
    if mid:
        done = await ctx.pool.fetchrow(
            "SELECT final_text, state_after FROM agent_turns WHERE message_id=$1 AND final_text IS NOT NULL", mid)
        if done is not None:
            after = done["state_after"]
            if isinstance(after, str):
                after = json.loads(after)
            if isinstance(after, dict):
                await ob._save_session(ctx.pool, sender, after)
            await ob._say(ctx, pid, sender, done["final_text"])
            return True

    words = guard.clean(text)
    if guard.injection(words):
        await guard.strike(ctx.pool, sender, "registration_injection")
        await ob._resend(ctx, pid, sender, state, ob.t(state.get("lang", "en"), "ob_steer"))
        return True

    if smalltalk.asks_help(words):
        return await _answer_help(ctx, sender, pid, words, state, mid)

    if state.get("step") == "confirm_all" and ob.registration_complete(state) and ob.is_affirmative(words):
        return False  # onboarding_chat performs the only final create, after this guard check.

    lang = smalltalk.language(words, state.get("lang", ob.detect_lang(words)))
    state["lang"] = lang
    toolbox = RegistrationToolbox(ctx, sender, words, state)
    try:
        policy = agent_policy.load_policy(getattr(ctx.settings, "agent_policy_path", None))
    except agent_policy.PolicyError:
        log.exception("agent policy is unavailable; using the deterministic onboarding flow")
        return False
    system = _prompt(state, lang, policy)
    facts = toolbox.facts
    facts.add(system, _state_summary(state))
    history_rows = state.get("agent_history")
    if isinstance(history_rows, list):
        facts.add(*(row.get("text", "") for row in history_rows if isinstance(row, dict) and row.get("role") == "assistant"))
        facts.add_user(*(row.get("text", "") for row in history_rows if isinstance(row, dict) and row.get("role") == "user"))
    facts.add_user(words)
    facts.prices.update(
        int(service["price"]) for service in state.get("data", {}).get("services", [])
        if isinstance(service, dict) and isinstance(service.get("price"), int)
    )
    facts.add(*(
        f"Duration equivalent: {service['minutes'] // 60} hours"
        for service in state.get("data", {}).get("services", [])
        if isinstance(service, dict) and isinstance(service.get("minutes"), int) and service["minutes"] > 0 and service["minutes"] % 60 == 0
    ))
    deposit = state.get("data", {}).get("deposit")
    if isinstance(deposit, int):
        facts.prices.add(deposit)
    messages = _history(state, words)
    try:
        await ctx.wa.typing(sender)
    except Exception:
        pass
    try:
        model_tools = agent_policy.with_tool_descriptions(TOOLS, policy, "registration")
        run = await drive(ctx, system, messages, toolbox, model_tools,
                          policy["repair_prompts"]["registration"])
    except Exception:
        log.exception("registration agent failed; using the deterministic onboarding flow")
        return False

    if not run.lead:
        outcome = ("registration_unavailable" if run.unavailable else
                   "registration_no_capture" if not toolbox._captured else "registration_no_valid_capture")
        await _trace(ctx, mid=mid, sender=sender, text=words, steps=run.steps, final_text=None, state_after=state,
                     outcome=outcome, grounded=False if run.problems else None,
                     problems=run.problems or toolbox.rejected, model=run.first_model,
                     calls=run.calls, ms=run.ms, model_requests=run.model_requests)
        return False

    body = run.lead
    problems = list(run.problems)
    if body:
        problems = _agent_reply_problems(body, facts, toolbox.accepted, policy, state.get("step"))
        if problems:
            repair_messages = list(messages)
            repair_messages.extend([
                {"role": "assistant", "content": body},
                {"role": "user", "content": policy["repair_prompts"]["registration"].format(problems="; ".join(problems))},
            ])
            try:
                repaired = await ctx.agent.step(system, repair_messages, [])
                candidate = (repaired.text or "").strip()
                repair_problems = _agent_reply_problems(candidate, facts, toolbox.accepted, policy, state.get("step"))
                run.calls += 1
                run.ms += repaired.ms
                run.model_requests.append(request_evidence(repaired))
                run.steps.append({"kind": "grounding_repair", "accepted": bool(candidate) and not repair_problems,
                                  "rejected_for": problems, "repair_rejected_for": repair_problems,
                                  "backend": repaired.backend, "model": repaired.model, "ms": repaired.ms})
                if candidate and not repair_problems:
                    body, problems = candidate, []
            except AgentUnavailable:
                log.warning("registration reply repair unavailable; using the deterministic prompt")
                run.steps.append({"kind": "grounding_repair", "accepted": False,
                                  "rejected_for": problems, "error": "model_unavailable"})
            except Exception:
                log.exception("registration reply repair failed; using the deterministic prompt")
                run.steps.append({"kind": "grounding_repair", "accepted": False,
                                  "rejected_for": problems, "error": "repair_failed"})
    grounded = bool(body) and not problems
    if not body:
        body = ob._question_for(state)
    if body and not grounded and problems:
        body = ob._question_for(state)
        log.warning("registration agent reply rejected: %s", "; ".join(problems))

    if state.get("step") == "confirm_all" and ob.registration_complete(state):
        summary = ob._question_for(state)
        if toolbox.accepted and not toolbox.rejected:
            # A changed complete draft must be shown in full before a later yes can create it.
            body = summary
        state["final_confirmation_pending"] = body.strip() == summary.strip()
        run.steps.append({"kind": "final_confirmation", "summary_sent": state["final_confirmation_pending"]})
    else:
        state.pop("final_confirmation_pending", None)

    first_reply = not state.get("help_hint_sent") and not any(
        item.get("role") == "assistant" for item in (state.get("agent_history") or []) if isinstance(item, dict)
    )
    if first_reply:
        body = ensure_help_hint(body, lang)
    state["help_hint_sent"] = True
    history = state.get("agent_history")
    history = list(history) if isinstance(history, list) else []
    stamp = datetime.now(ZoneInfo("UTC")).isoformat()
    history.extend([
        {"role": "user", "text": words[:500], "ts": stamp},
        {"role": "assistant", "text": body[:500], "ts": stamp},
    ])
    state["agent_history"] = history[-guard.MAX_HISTORY * 2:]
    await ob._save_session(ctx.pool, sender, state)
    await _trace(ctx, mid=mid, sender=sender, text=words, steps=run.steps, final_text=body, state_after=state,
                 outcome="registration_agent" if grounded else "registration_template", grounded=grounded,
                 problems=problems, model=run.first_model, calls=run.calls, ms=run.ms,
                 model_requests=run.model_requests)
    await ob._say(ctx, pid, sender, body)
    log.info("registration agent turn: %d model calls, %s, %d ms", run.calls,
             "model reply" if grounded else "validated template", run.ms)
    return True
