"""Agent eval: what does the model decide to do with a customer message, compared with the keyword rules?

The corpus (scripts/agent_corpus.json, 33 messages) was written to be hard for keyword rules: Sheng, typos, swahili clock times, a
stylist's name, a pick that depends on the slots just offered, a refund complaint, an injection attempt. Each item says which tool
the receptionist should call first and with which arguments, or that it should answer in words (and what the words must or must not say).

  python scripts/agent_eval.py                 # keyword rules only, no keys needed
  python scripts/agent_eval.py --models        # also every model backend configured in .env (LLM_* and LLM_FALLBACK_*)
  python scripts/agent_eval.py --models --out docs/AGENT_EVAL.md

Only the model's FIRST move is scored (the tool it calls, or its reply); the checker and tools in the live app add safety on top.
Rules cannot produce a reply, so their reply checks are skipped and only the booking decision counts."""
from __future__ import annotations

import argparse
import asyncio
import json
import os
import statistics
import sys
from datetime import date, datetime, timedelta
from zoneinfo import ZoneInfo

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

import httpx

from app.agent.client import AgentModel
from app.agent.policy import load_policy, with_tool_descriptions
from app.agent.prompt import system_prompt
from app.agent.tools import TOOLS
from app.ai.llm import Extractor, rule_extract
from app.config import get_settings

NBO = ZoneInfo("Africa/Nairobi")
NOW = datetime(2026, 11, 2, 9, 0, tzinfo=NBO)   # a Monday
TODAY = NOW.date()
SERVICES = [
    {"id": "1", "name": "Fade", "aliases": ["kinyozi"], "duration_min": 45, "price_kes": 500},
    {"id": "2", "name": "Beard trim", "aliases": [], "duration_min": 20, "price_kes": 200},
    {"id": "3", "name": "Braids", "aliases": ["kusuka"], "duration_min": 180, "price_kes": 1500},
    {"id": "4", "name": "Kids cut", "aliases": [], "duration_min": 30, "price_kes": 300},
    {"id": "5", "name": "Silk press", "aliases": [], "duration_min": 90, "price_kes": 2500},
]
STAFF = {"1": ["Amina", "Brian"], "2": ["Brian"], "3": ["Amina"], "4": ["Brian"], "5": ["Amina"]}
HOURS = {k: ["09:00", "18:00"] for k in ("mon", "tue", "wed", "thu", "fri", "sat")} | {"sun": None}
SALON = {"name": "Zuri Cuts", "open_hours": HOURS}
OFFERED = [{"slot_id": "s1", "when": "Tue 03 Nov, 10:00", "staff_name": "Brian"},
           {"slot_id": "s2", "when": "Tue 03 Nov, 14:00", "staff_name": "Brian"},
           {"slot_id": "s3", "when": "Tue 03 Nov, 15:30", "staff_name": "Amina"}]


def want_date(offset: int) -> str:
    return (TODAY + timedelta(days=offset)).isoformat()


def brief(item: dict) -> str:
    st = {"state": "offered", "service_id": "1", "_offered_view": OFFERED} if item["offered"] else {}
    return system_prompt(salon=SALON, services=SERVICES, staff_by_service=STAFF, now=NOW, st=st,
                         deposit_line="Deposit: KES 200 (never more than the service price) is requested by M-Pesa when a slot is held.",
                         channel="Telegram", lang_hint="English", policy=load_policy(get_settings().agent_policy_path))


def eval_tools() -> list[dict]:
    return with_tool_descriptions(TOOLS, load_policy(get_settings().agent_policy_path), "booking_receptionist")


def judge(item: dict, tool: str | None, args: dict, reply: str | None) -> tuple[bool, bool]:
    """(chose the right action, and the right arguments / words)."""
    e = item["expect"]
    allowed = e["tool"] if isinstance(e["tool"], list) else [e["tool"]]
    acted = tool in allowed
    if not acted:
        return False, False
    ok = True
    for k, v in (e.get("args") or {}).items():
        if k == "day_offset":
            ok &= str(args.get("date")) == want_date(v)
        elif k == "stylist":
            ok &= v.lower() in str(args.get("stylist", "")).lower()
        else:
            ok &= str(args.get(k, "")).lower() == str(v).lower()
    if reply is not None and e["tool"] is None:
        low = reply.lower()
        if e.get("reply_has_any"):
            ok &= any(w.lower() in low for w in e["reply_has_any"])
        ok &= not any(w.lower() in low for w in e.get("reply_lacks", []))
    return True, bool(ok)


def rules_decide(item: dict) -> tuple[str | None, dict]:
    ex = rule_extract(item["text"], TODAY, SERVICES, "offered" if item["offered"] else "idle")
    if ex.intent == "book":
        return "find_slots", {"service": ex.service, "date": ex.date, "window": ex.window, "time": ex.time}
    if ex.intent == "choose_slot" and ex.choice:
        return "reserve_slot", {"slot_id": f"s{ex.choice}"}
    if ex.intent == "cancel":
        return "cancel_booking", {}
    return None, {}


async def model_decide(model: AgentModel, item: dict) -> tuple[str | None, dict, str | None, int]:
    step = await model.step(brief(item), [{"role": "user", "content": item["text"]}], eval_tools())
    if step.calls:
        return step.calls[0].name, step.calls[0].args, None, step.ms
    return None, {}, step.text, step.ms


def summarize(rows: list[dict]) -> dict:
    cats: dict[str, list[dict]] = {}
    for r in rows:
        cats.setdefault(r["cat"], []).append(r)
    return {"n": len(rows), "action": sum(r["acted"] for r in rows), "exact": sum(r["exact"] for r in rows),
            "by_cat": {c: (sum(x["exact"] for x in v), len(v)) for c, v in cats.items()},
            "ms": int(statistics.median([r["ms"] for r in rows if r["ms"]])) if any(r["ms"] for r in rows) else 0}


async def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--models", action="store_true", help="also score the configured model backends")
    ap.add_argument("--out", help="write a markdown table here")
    a = ap.parse_args()
    corpus = json.load(open(os.path.join(os.path.dirname(os.path.abspath(__file__)), "agent_corpus.json")))
    results: dict[str, dict] = {}
    rows = []
    for it in corpus:
        tool, args = rules_decide(it)
        acted, exact = judge(it, tool, args, None)
        rows.append({"cat": it["cat"], "acted": acted, "exact": exact, "ms": 0})
    results["keyword rules"] = summarize(rows)
    if a.models:
        s = get_settings()
        async with httpx.AsyncClient() as http:
            for ep in Extractor(http, s).endpoints():
                if not ep[2]:
                    print(f"skipping {ep[0]} {ep[3]}: no API key", file=sys.stderr)
                    continue
                model = AgentModel(http, s.model_copy(update={"agent_call_timeout_s": 30.0}), None, lambda ep=ep: [ep], None)
                sem = asyncio.Semaphore(4)

                async def one(it):
                    async with sem:
                        try:
                            tool, args, reply, ms = await model_decide(model, it)
                        except Exception as e:  # noqa: BLE001
                            print(f"  {ep[3]}: {it['text'][:40]!r} failed: {e.__class__.__name__}", file=sys.stderr)
                            return {"cat": it["cat"], "acted": False, "exact": False, "ms": 0}
                        acted, exact = judge(it, tool, args, reply)
                        return {"cat": it["cat"], "acted": acted, "exact": exact, "ms": ms}
                results[f"{ep[3]} ({ep[0]})"] = summarize(await asyncio.gather(*(one(it) for it in corpus)))
    cats = sorted({c["cat"] for c in corpus})
    head = f"| Decision maker | Right action | Right action and details | " + " | ".join(cats) + " | median ms |"
    lines = [head, "|" + "---|" * (len(cats) + 4)]
    for name, r in results.items():
        lines.append(f"| {name} | {r['action']}/{r['n']} | {r['exact']}/{r['n']} | " + " | ".join(f"{r['by_cat'][c][0]}/{r['by_cat'][c][1]}" for c in cats) + f" | {r['ms'] or '-'} |")
    table = "\n".join(lines)
    print(table)
    if a.out:
        with open(a.out, "w") as f:
            f.write(f"# Agent eval\n\nHeld-out messages the keyword rules were not tuned for. First move only. Date fixed at {TODAY}.\n\n{table}\n")
    return 0


if __name__ == "__main__":
    sys.exit(asyncio.run(main()))
