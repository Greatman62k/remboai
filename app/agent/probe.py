"""Does this model host drive the agent? One real tool-calling round trip per backend and per wire format.
Used by `scripts/doctor.py` (agent mode on) and `scripts/probe_tools.py`."""
from __future__ import annotations

import json
from dataclasses import dataclass

from .client import AgentModel
from .tools import TOOLS

SYSTEM = ("You are the booking receptionist for 'Zuri Cuts'. Today is Monday 2026-11-02. Calendar: Tue = 2026-11-03. Services: Fade (KES 500). "
          "To learn what is free you must call find_slots; never guess availability.")
ASK = "nataka fade kesho mchana"


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
    for ep in endpoints:
        for protocol in ("tools", "json"):
            s = settings.model_copy(update={"agent_protocol": protocol, "agent_call_timeout_s": max(settings.agent_call_timeout_s, 20.0)})
            m = AgentModel(http, s, None, lambda ep=ep: [ep], None)
            try:
                step = await m.step(SYSTEM, [{"role": "user", "content": ASK}], TOOLS)
            except Exception as e:  # noqa: BLE001
                out.append(Probe(ep[0], ep[3], protocol, False, 0, f"{e.__class__.__name__}: {str(e)[:100]}"))
                continue
            call = step.calls[0] if step.calls else None
            good = bool(call and call.name == "find_slots" and str(call.args.get("service", "")).lower() == "fade")
            detail = (f"called {call.name}({json.dumps(call.args, ensure_ascii=False)})" if call else f"answered in words instead of calling a tool: {step.text[:80]!r}")
            out.append(Probe(ep[0], ep[3], protocol, good, step.ms, detail))
    return out
