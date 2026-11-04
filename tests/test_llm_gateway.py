"""Model gateway: backend labels, circuit breaker, call logging, reasoning strip, /model-info."""
import asyncio
import json
import os
from datetime import date
from types import SimpleNamespace

import httpx
import pytest

from app.ai.llm import Extractor, _BREAKER, _failure_kind, rule_extract, safe_chat_endpoint, strip_reasoning

DSN = os.environ.get("TEST_DATABASE_URL")
needs_db = pytest.mark.skipif(not DSN, reason="needs TEST_DATABASE_URL")

SERVICES = [{"name": "Fade", "aliases": []}]


def test_model_endpoint_evidence_never_includes_credentials_or_query_parameters():
    assert safe_chat_endpoint("https://judge:secret@api.meta.ai/v1?token=hidden#debug") == (
        "https://api.meta.ai/v1/chat/completions"
    )


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


def test_model_failures_are_named_so_the_operator_knows_what_broke():
    req = httpx.Request("POST", "https://primary.test/v1/chat")
    assert _failure_kind(httpx.ConnectError("no route to host"), None) == "network"
    assert _failure_kind(httpx.ReadTimeout("slow server"), None) == "timeout"
    denied = httpx.HTTPStatusError("denied", request=req, response=httpx.Response(401))
    assert _failure_kind(denied, 401) == "auth"
    busy = httpx.HTTPStatusError("busy", request=req, response=httpx.Response(429))
    assert _failure_kind(busy, 429) == "rate_limited"
    down = httpx.HTTPStatusError("down", request=req, response=httpx.Response(503))
    assert _failure_kind(down, 503) == "server"
    assert _failure_kind(ValueError("bad json"), None) == "other"


@pytest.mark.asyncio
async def test_no_network_means_keyword_rules_and_a_degraded_flag():
    def handler(req):
        raise httpx.ConnectError("no route to host")
    _BREAKER.clear()
    async with httpx.AsyncClient(transport=httpx.MockTransport(handler)) as http:
        ext = Extractor(http, settings())
        res = await ext.extract("what time do you close", today=date(2026, 10, 30), services=SERVICES, state="idle")
        assert (res.source, res.degraded) == ("keywords", True)
        assert res.ex.intent == rule_extract("what time do you close", date(2026, 10, 30), SERVICES, "idle").intent
    _BREAKER.clear()


@pytest.mark.asyncio
async def test_a_rejected_api_key_means_keyword_rules_and_a_degraded_flag():
    def handler(req):
        return httpx.Response(401, json={"error": "bad key"})
    _BREAKER.clear()
    async with httpx.AsyncClient(transport=httpx.MockTransport(handler)) as http:
        ext = Extractor(http, settings())
        res = await ext.extract("what time do you close", today=date(2026, 10, 30), services=SERVICES, state="idle")
        assert (res.source, res.degraded) == ("keywords", True)
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
        pool, out, salon = await build(DSN, llm_backend="baseten", ops_api_key="test-ops-key")
        async with client() as c:
            assert (await c.get("/model-info")).status_code == 401
            r = await c.get("/model-info", headers={"x-ops-key": "test-ops-key"})
            assert r.json()["backend"] == "baseten"
            assert r.json()["model"] == "muse-spark-1.3"
            assert r.json()["endpoint"] == "https://primary.test/v1/chat/completions"
            assert "k" not in r.json()["endpoint"]
        await pool.close()
    asyncio.run(run())
