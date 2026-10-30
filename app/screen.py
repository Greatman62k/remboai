"""A free, code-only screen in front of the language model. A first message that has nothing to do with a salon or barbershop
(no service, day, time, price, hours, cancel, greeting or hair-and-booking vocabulary) never reaches the model: it gets a short
pointer back to what the receptionist can do. Messages inside a running conversation (slots offered, a question asked, a deposit
due) are follow-ups and always pass. This saves money, and keeps strangers from using the receptionist as a free general chatbot."""
from __future__ import annotations

import re
from datetime import date

from . import lexicon
from .ai.llm import rule_extract

MAX_FIRST_MESSAGE = 400

_STRONG = lexicon.load().pattern(*lexicon.STRONG)
_SPECIFIC = lexicon.load().pattern(*lexicon.SPECIFIC)


def _menu_hit(text: str, services: list[dict]) -> bool:
    low = text.lower()
    return any(k and re.search(rf"\b{re.escape(k.lower())}\b", low) for s in services for k in [s["name"], *s.get("aliases", [])])


def route(text: str, services: list[dict], state: str, today: date) -> str:
    """Where a message goes:
      'model' a service, style or booking word, or an item on this salon's menu: worth a model call;
      'rules' prices, hours, cancel, a greeting, a day or time that the free keyword rules can read: no model;
      'model' also when the rules cannot read it but it has a specific price, payment, hours or cancel word ("when are you open");
      'off'   nothing to do with a salon: a pointer back to what the bot can do. Generic request words alone ("nifanyie", "i want") do not qualify.
    Messages inside a running conversation are follow-ups and always go to the model."""
    if state != "idle":
        return "model"
    t = (text or "").strip()
    if not t or len(t) > MAX_FIRST_MESSAGE:
        return "off"
    if _STRONG.search(t) or _menu_hit(t, services):
        return "model"
    if rule_extract(t, today, services, state).intent != "other":
        return "rules"
    return "model" if _SPECIFIC.search(t) else "off"


def relevant(text: str, services: list[dict], state: str, today: date) -> bool:
    return route(text, services, state, today) != "off"

