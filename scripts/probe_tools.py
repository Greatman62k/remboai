"""Does your model host support the receptionist's tool calling? Prints one line per backend and wire format.

  python scripts/probe_tools.py

`tools` is native function calling (Meta documents it for Muse Spark). `json` is the fallback the app uses when a host has none.
If `tools` fails but `json` passes, set AGENT_PROTOCOL=json (or leave auto: the app switches by itself)."""
from __future__ import annotations

import asyncio
import os
import sys

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

import httpx

from app.agent.probe import probe_backends
from app.ai.llm import Extractor
from app.config import get_settings


async def main() -> int:
    s = get_settings()
    async with httpx.AsyncClient() as http:
        results = await probe_backends(http, s, Extractor(http, s).endpoints())
    print(f"{'backend':<12} {'model':<34} {'protocol':<9} {'result':<6} {'ms':>6}  detail")
    for r in results:
        print(f"{r.backend:<12} {r.model:<34} {r.protocol:<9} {'PASS' if r.ok else 'FAIL':<6} {r.ms:>6}  {r.detail}")
    primary_ok = any(r.ok for r in results[:2])
    print("\nAgent mode can run on the primary backend." if primary_ok else "\nThe primary backend failed both formats: check LLM_BASE_URL, LLM_API_KEY and LLM_MODEL first.")
    return 0 if primary_ok else 1


if __name__ == "__main__":
    sys.exit(asyncio.run(main()))
