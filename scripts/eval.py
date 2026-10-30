"""Model eval: score each backend (and the keyword baseline) on the labeled corpus.

Usage:
  python scripts/eval.py                                   # keyword baseline only, no keys needed
  EVAL_BACKENDS="baseten:https://api.baseten.co/v1:meta-models/muse-spark-open-1026" \\
  EVAL_API_KEYS="baseten:$BASETEN_KEY" python scripts/eval.py

Each EVAL_BACKENDS entry is label:base_url:model. Keys come from EVAL_API_KEYS as
label:key pairs. Output is an accuracy/latency table for the demo slide.
"""
from __future__ import annotations

import asyncio
import json
import os
import statistics
import sys
from datetime import date

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

import httpx

from app.ai.llm import Extractor, rule_extract
from app.config import Settings

SERVICES = [{"name": "Fade", "aliases": ["kinyozi"]}, {"name": "Braids", "aliases": []}, {"name": "Shave", "aliases": []}]
TODAY = date(2026, 10, 30)


def load_corpus() -> list[dict]:
    here = os.path.dirname(os.path.abspath(__file__))
    with open(os.path.join(here, "eval_corpus.json")) as f:
        return json.load(f)


async def score_rules(corpus: list[dict]) -> dict:
    lats, hits, lang_hits = [], 0, 0
    import time as clock
    for item in corpus:
        t0 = clock.perf_counter()
        got = rule_extract(item["text"], TODAY, SERVICES, item.get("state", "idle"))
        lats.append((clock.perf_counter() - t0) * 1000)
        hits += got.intent == item["intent"]
        lang_hits += got.language == item["lang"]
    return {"backend": "keywords", "n": len(corpus), "intent": hits / len(corpus),
            "lang": lang_hits / len(corpus), "p50_ms": statistics.median(lats),
            "p95_ms": sorted(lats)[max(0, int(len(lats) * 0.95) - 1)]}


async def score_backend(label: str, base: str, model: str, key: str, corpus: list[dict]) -> dict:
    s = Settings(_env_file=None, llm_base_url=base, llm_api_key=key, llm_model=model,
                 llm_backend=label, llm_timeout_s=20.0, llm_json_mode="json_object")
    lats, hits, lang_hits, cost_in, cost_out = [], 0, 0, 0, 0
    async with httpx.AsyncClient() as http:
        ext = Extractor(http, s)
        for item in corpus:
            try:
                r = await ext.extract(item["text"], today=TODAY, services=SERVICES, state=item.get("state", "idle"))
            except Exception as e:
                print(f"  {label}: gave up on {item['text']!r}: {e}")
                return {"backend": label, "n": 0, "intent": 0.0, "lang": 0.0, "p50_ms": -1, "p95_ms": -1}
            hits += r.ex.intent == item["intent"]
            lang_hits += r.ex.language == item["lang"]
            lats.append(r.ms)
    return {"backend": label, "n": len(corpus), "intent": hits / len(corpus), "lang": lang_hits / len(corpus),
            "p50_ms": statistics.median(lats), "p95_ms": sorted(lats)[max(0, int(len(lats) * 0.95) - 1)]}


def show(rows: list[dict]) -> None:
    print(f"{'backend':<12}{'n':>4}{'intent':>9}{'lang':>7}{'p50_ms':>9}{'p95_ms':>9}")
    for r in rows:
        print(f"{r['backend']:<12}{r['n']:>4}{r['intent']:>8.0%}{r['lang']:>7.0%}{r['p50_ms']:>9.0f}{r['p95_ms']:>9.0f}")


async def main() -> None:
    corpus = load_corpus()
    rows = [await score_rules(corpus)]
    backends = [b for b in os.environ.get("EVAL_BACKENDS", "").split() if b.count(":") >= 2]
    keys = dict(p.split(":", 1) for p in os.environ.get("EVAL_API_KEYS", "").split() if ":" in p)
    for b in backends:
        label, base, model = b.split(":", 2)
        rows.append(await score_backend(label, base, model, keys.get(label, ""), corpus))
    show(rows)


if __name__ == "__main__":
    asyncio.run(main())
