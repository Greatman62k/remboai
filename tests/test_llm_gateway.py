"""Model gateway: backend labels, circuit breaker, call logging, reasoning strip, /model-info."""
import asyncio
import json
import os
from datetime import date
from types import SimpleNamespace

import httpx
import pytest

from app.ai.llm import Extractor, _BREAKER, strip_reasoning

DSN = os.environ.get("TEST_DATABASE_URL")
needs_db = pytest.mark.skipif(not DSN, reason="needs TEST_DATABASE_URL")

SERVICES = [{"name": "Fade", "aliases": []}]


def settings(**over):
    base = dict(llm_base_url="https://primary.test", llm_api_key="k1", llm_model="m1",
                llm_fallback_base_url="https://fallback.test", llm_fallback_api_key="k2", llm_fallback_model="m2",
                llm_backend="baseten", llm_fallback_backend="modal", llm_json_mode="none", llm_reasoning_effort="",
                llm_system_role="developer", llm_timeout_s=5.0, llm_breaker_trips=2, llm_breaker_cooldown_s=60,
                llm_quantization="", llm_host="")
    base.update(over)
    return SimpleNamespace(**base)


def transport(counts, fail_primary=False):
    def handler(req):
        host = req.url.host
        counts[host] = counts.get(host, 0) + 1
        if fail_primary and host == "primary.test":
            return httpx.Response(500, json={"error": "down"})
        return httpx.Response(200, json={"choices": [{"message": {"content": '{"intent": "hours"}'}}],
                                         "usage": {"prompt_tokens": 10, "completion_tokens": 2}})
    return httpx.MockTransport(handler)


def test_eval_corpus_stays_honest():
    import asyncio as _asyncio
    from scripts.eval import load_corpus, score_rules
    row = _asyncio.run(score_rules(load_corpus()))
    assert row["n"] == 40 and row["intent"] >= 0.95 and row["lang"] >= 0.95


def test_reasoning_text_never_reaches_the_parser_or_the_customer():
    assert strip_reasoning('<think>hmm, tricky</think>{"intent": "hours"}') == '{"intent": "hours"}'
    assert strip_reasoning('{"intent": "hours"}') == '{"intent": "hours"}'


@pytest.mark.asyncio
async def test_a_tripped_backend_rests_while_the_fallback_serves():
    _BREAKER.clear()
    counts = {}
    async with httpx.AsyncClient(transport=transport(counts, fail_primary=True)) as http:
        ext = Extractor(http, settings())
        first = await ext.extract("what time do you close", today=date(2026, 10, 30), services=SERVICES, state="idle")
        assert (first.ex.intent, first.source) == ("hours", "model")
        assert counts == {"primary.test": 2, "fallback.test": 1}
        second = await ext.extract("what time do you close", today=date(2026, 10, 30), services=SERVICES, state="idle")
        assert second.ex.intent == "hours"
        assert counts["primary.test"] == 2
    _BREAKER.clear()


@needs_db
def test_successful_calls_are_logged_with_backend_and_tokens():
    async def run():
        from app.db import make_pool, migrate
        pool = await make_pool(DSN)
        await pool.execute("DROP SCHEMA public CASCADE; CREATE SCHEMA public;")
        await migrate(pool)
        counts = {}
        async with httpx.AsyncClient(transport=transport(counts)) as http:
            ext = Extractor(http, settings(), pool=pool)
            r = await ext.extract("bei ya fade?", today=date(2026, 10, 30), services=SERVICES, state="idle")
            assert r.source == "model"
        row = await pool.fetchrow("SELECT backend, model, ok, tokens_in, tokens_out FROM model_calls")
        assert (row["backend"], row["model"], row["ok"], row["tokens_in"], row["tokens_out"]) == ("baseten", "m1", True, 10, 2)
        await pool.close()
    asyncio.run(run())


@needs_db
def test_model_info_names_the_active_backend():
    async def run():
        from tests.harness import build, client
        from app.main import app
        pool, out, salon = await build(DSN, llm_backend="baseten")
        async with client() as c:
            r = await c.get("/model-info")
            assert r.json()["backend"] == "baseten"
            assert r.json()["model"] == "muse-spark-1.3"
        await pool.close()
    asyncio.run(run())
