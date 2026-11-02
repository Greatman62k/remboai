"""Does this model host drive the agent? One real tool-calling round trip per backend and per wire format.
Used by `scripts/doctor.py` (agent mode on) and `scripts/probe_tools.py`."""
from __future__ import annotations

import json
from dataclasses import dataclass
from datetime import datetime
from zoneinfo import ZoneInfo

from .client import AgentModel
from .policy import load_policy, with_tool_descriptions
from .prompt import system_prompt
from .tools import TOOLS

ASK = "nataka fade kesho mchana"


def _system(settings) -> str:
    now = datetime(2026, 11, 2, 9, 0, tzinfo=ZoneInfo("Africa/Nairobi"))
    service = {"id": "probe-fade", "name": "Fade", "price_kes": 500, "duration_min": 45, "aliases": []}
    salon = {"name": "Zuri Cuts", "open_hours": {day: ["09:00", "18:00"] for day in ("mon", "tue", "wed", "thu", "fri", "sat")}}
    salon["open_hours"]["sun"] = None
    return system_prompt(
        salon=salon, services=[service], staff_by_service={"probe-fade": ["Brian"]}, now=now,
        st={"state": "idle"}, deposit_line="Deposit: none is required.", channel="Probe", lang_hint="Swahili or Sheng",
        policy=load_policy(getattr(settings, "agent_policy_path", None)),
    )


@dataclass
class Probe:
    backend: str
    model: str
    protocol: str
    ok: bool
    ms: int
    detail: str


async def probe_backends(http, settings, endpoints) -> list[Probe]:
    out: list[Probe] = []
    policy = load_policy(getattr(settings, "agent_policy_path", None))
    tools = with_tool_descriptions(TOOLS, policy, "booking_receptionist")
    for ep in endpoints:
        for protocol in ("tools", "json"):
            s = settings.model_copy(update={"agent_protocol": protocol, "agent_call_timeout_s": max(settings.agent_call_timeout_s, 20.0)})
            m = AgentModel(http, s, None, lambda ep=ep: [ep], None)
            try:
                step = await m.step(_system(settings), [{"role": "user", "content": ASK}], tools)
            except Exception as e:  # noqa: BLE001
                out.append(Probe(ep[0], ep[3], protocol, False, 0, f"{e.__class__.__name__}: {str(e)[:100]}"))
                continue
            call = step.calls[0] if step.calls else None
            good = bool(call and call.name == "find_slots" and str(call.args.get("service", "")).lower() == "fade")
            detail = (f"called {call.name}({json.dumps(call.args, ensure_ascii=False)})" if call else f"answered in words instead of calling a tool: {step.text[:80]!r}")
            out.append(Probe(ep[0], ep[3], protocol, good, step.ms, detail))
    return out
