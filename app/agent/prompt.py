"""The receptionist's brief. Everything the model may state as fact is written here, from the database, every turn."""
from __future__ import annotations

import json
from datetime import datetime, timedelta

from app.messages import t

DAY_NAMES = ("mon", "tue", "wed", "thu", "fri", "sat", "sun")


def hours_lines(open_hours: dict) -> str:
    out = []
    for k in DAY_NAMES:
        v = (open_hours or {}).get(k)
        out.append(f"- {k.capitalize()}: {v[0]}-{v[1]}" if v else f"- {k.capitalize()}: closed")
    return "\n".join(out)


def menu_lines(services: list[dict], staff_by_service: dict[str, list[str]]) -> str:
    lines = []
    for s in sorted(services, key=lambda x: x["name"]):
        who = ", ".join(staff_by_service.get(s["id"], [])) or "no team member assigned"
        alias = f" (also called: {', '.join(s['aliases'])})" if s.get("aliases") else ""
        lines.append(f"- {s['name']}{alias}: KES {s['price_kes']}, {s['duration_min']} min, team: {who}")
    return "\n".join(lines) or "- (no services listed yet)"


def state_summary(st: dict, services: list[dict], tz_label: str) -> str:
    state = st.get("state") or "idle"
    if state == "asking":
        bits = [f"service {st.get('service_name')}" if st.get("service_name") else "no service yet"]
        if st.get("date"):
            bits.append(f"day {st['date']}")
        if st.get("window"):
            bits.append(f"window {st['window']}")
        if st.get("time"):
            bits.append(f"time {st['time']}")
        return "The customer is part way through a request: " + ", ".join(bits) + "."
    if state == "offered":
        svc = next((s["name"] for s in services if s["id"] == st.get("service_id")), "the service")
        return f"These slots for {svc} were already offered (use their slot_id with reserve_slot if the customer picks one):\n" + "\n".join(
            f"- {s['slot_id']}: {s['when']} with {s['staff_name']}" for s in st.get("_offered_view", []))
    if state == "no_slots":
        svc = next((s["name"] for s in services if s["id"] == st.get("service_id")), "that service")
        return f"No slots were free for {svc} on {st.get('date')}. The customer may join the waitlist (join_waitlist) or try another day."
    if state == "held":
        return "The customer has a slot held while they pay the deposit. If they want to cancel, use cancel_booking."
    return "No booking is in progress."


def system_prompt(*, salon: dict, services: list[dict], staff_by_service: dict[str, list[str]], now: datetime, st: dict,
                  deposit_line: str, channel: str, lang_hint: str, policy: dict,
                  first_reply: bool = False, help_example: str = "Fade") -> str:
    cal = "\n".join(f"{(now + timedelta(days=i)).strftime('%a')} = {(now + timedelta(days=i)).date().isoformat()}" for i in range(15))
    persona = policy["booking_receptionist"]
    lang = "sw" if lang_hint == "Swahili or Sheng" else "en"
    first_reply_rule = (
        f'This is your first reply in this chat. End with: "{t(lang, "help_hint")}"'
        if first_reply else "The customer already received the help invitation in this chat."
    )
    return f"""{persona['identity']}
{persona['purpose']}
{"\n".join(policy["prompt_wrapper"])}
{json.dumps(persona, ensure_ascii=False, indent=2)}

LIVE CHAT CONTEXT
Business: {salon['name']}
Channel: {channel}
Customer last wrote in: {lang_hint}
First-reply guidance: {first_reply_rule}
Today is {now.strftime('%A')} {now.date().isoformat()}, time {now.strftime('%H:%M')} (Africa/Nairobi). Calendar:
{cal}

VERIFIED BUSINESS FACTS
Services:
{menu_lines(services, staff_by_service)}
Opening hours:
{hours_lines(salon.get('open_hours') or {})}
{deposit_line}

HELP GUIDE
Explain these options in simple words when the customer asks HELP or asks what you can do. Give only the example they need, and answer follow-up questions about each option.
English examples: book by sending "{help_example} Saturday afternoon"; ask for prices with "prices"; ask for hours with "hours"; cancel with "cancel"; when no time is free, join the waitlist with "WAITLIST".
Kiswahili examples: book by sending "{help_example} Jumamosi mchana"; ask for prices with "bei"; ask for hours with "muda"; cancel with "ghairi"; ask to be notified when a time opens with "nijulishe".

CURRENT SITUATION
{state_summary(st, services, 'Africa/Nairobi')}
"""
