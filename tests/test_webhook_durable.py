import asyncio
import hashlib
import datetime
import hmac
import json
from types import SimpleNamespace

import httpx
import pytest
from fastapi.testclient import TestClient

from app.config import Settings
from app.webhook_jobs import QueueCapacityError, claim_next, enqueue_jobs, list_dead_jobs, process_job, requeue_dead_job

pytestmark = pytest.mark.asyncio

async def _pool(dsn):
    from app.db import make_pool, migrate
    pool = await make_pool(dsn)
    await pool.execute("DROP SCHEMA public CASCADE; CREATE SCHEMA public;")
    await migrate(pool)
    return pool

DSN = None
try:
    import os
    DSN = os.environ.get("TEST_DATABASE_URL")
except:
    pass

# fallback for unit tests without DB - we skip if no TEST_DATABASE_URL
def open_day():
    from datetime import datetime, date, timedelta
    from zoneinfo import ZoneInfo
    NBO = ZoneInfo("Africa/Nairobi")
    today = datetime.now(NBO).date()
    return (today + timedelta(days=1)).isoformat() if today.weekday() != 6 else (today + timedelta(days=2)).isoformat()

pytestmark = pytest.mark.skipif(not DSN, reason="set TEST_DATABASE_URL to run durable queue tests")

@pytest.mark.asyncio
async def test_enqueue_and_dedup():
    pool = await _pool(DSN)
    await enqueue_jobs(pool, "whatsapp", [("id1", {"wa_id": "254712345678", "text": "hi"})])
    assert await pool.fetchval("SELECT count(*) FROM webhook_jobs WHERE provider='whatsapp'") == 1
    await enqueue_jobs(pool, "whatsapp", [("id1", {"wa_id": "254712345678", "text": "hi"})])
    assert await pool.fetchval("SELECT count(*) FROM webhook_jobs") == 1
    await enqueue_jobs(pool, "mpesa", [("cb1", {"Body": {"stkCallback": {"CheckoutRequestID": "c1", "ResultCode": 0}}})])
    assert await pool.fetchval("SELECT count(*) FROM webhook_jobs WHERE provider='mpesa'") == 1
    await pool.close()

@pytest.mark.asyncio
async def test_global_capacity():
    pool = await _pool(DSN)
    await enqueue_jobs(pool, "whatsapp", [("g1", {"wa_id": "254700000001"})], max_pending=1)
    try:
        await enqueue_jobs(pool, "whatsapp", [("g2", {"wa_id": "254700000002"})], max_pending=1)
        assert False, "should have raised"
    except QueueCapacityError:
        pass
    assert await pool.fetchval("SELECT count(*) FROM webhook_jobs") == 1
    await pool.close()

@pytest.mark.asyncio
async def test_per_sender_capacity():
    pool = await _pool(DSN)
    await enqueue_jobs(pool, "whatsapp", [("s1", {"wa_id": "254711111111"})], max_pending=100, max_pending_per_sender=1)
    try:
        await enqueue_jobs(pool, "whatsapp", [("s2", {"wa_id": "254711111111"})], max_pending=100, max_pending_per_sender=1)
        assert False
    except QueueCapacityError:
        pass
    await pool.close()

@pytest.mark.asyncio
async def test_claim_and_process_whatsapp():
    from tests.harness import build, post, client
    pool, out, salon = await build(DSN)
    from app.main import app
    # enqueue a whatsapp message via the durable queue directly
    day = open_day()
    out.script["fade kesho mchana"] = {"intent": "book", "service": "Fade", "date": open_day(), "window": "afternoon", "language": "en"}
    payload = {"id": "wamid.test-claim", "sender": "254712345678", "phone_number_id": "PID1", "kind": "text", "text": "Fade kesho mchana", "wa_id": "254712345678"}
    await enqueue_jobs(pool, "whatsapp", [(payload["id"], payload)])
    job = await claim_next(pool)
    assert job is not None
    assert job["event_id"] == "wamid.test-claim"
    await process_job(app.state.ctx, job)
    assert await pool.fetchval("SELECT status FROM webhook_jobs WHERE id=$1", job["id"]) == "processed"
    # booking should have been created via inbound
    assert len(out.sent) >= 1
    assert any("Fade" in str(m) for m in out.sent)
    await pool.close()

@pytest.mark.asyncio
async def test_mpesa_unknown_becomes_unmatched_after_retries():
    pool = await _pool(DSN)
    from app.main import app
    from app.config import Settings
    from app.ctx import Ctx
    from tests.harness import Outside
    from app.mpesa import Daraja
    from app.llm import Extractor
    from app.telegram import Channels, Telegram
    from app.vision import StyleMatcher
    from app.voice import Transcriber
    from app.whatsapp import WhatsApp
    import httpx
    # minimal ctx with mocked daraja that returns None (unverifiable) then amount mismatch
    out = Outside()
    http = httpx.AsyncClient(transport=httpx.MockTransport(out.handler))
    settings = Settings(_env_file=None, database_url=DSN, wa_app_secret="s", wa_verify_token="v", wa_access_token="tok", llm_base_url="https://primary.test/v1", llm_api_key="k", llm_model="m", mpesa_consumer_key="ck", mpesa_consumer_secret="cs", mpesa_passkey="pk", mpesa_callback_secret="cbsec", public_base_url="https://t.test", mpesa_verify_callbacks=False)
    wa = WhatsApp(http, settings.graph_version)
    app.state.settings = settings
    app.state.pool = pool
    app.state.wa = wa
    app.state.ctx = Ctx(settings, pool, Channels(wa, None), Extractor(http, settings), None, Daraja(http, settings), Transcriber(http, settings), StyleMatcher(http, settings, Extractor(http, settings).endpoints))
    # insert callback payload directly
    cb_payload = {"Body": {"stkCallback": {"CheckoutRequestID": "unknown-checkout", "ResultCode": 0, "ResultDesc": "ok", "CallbackMetadata": {"Item": [{"Name": "Amount", "Value": 200}, {"Name": "PhoneNumber", "Value": "254712345678"}, {"Name": "MpesaReceiptNumber", "Value": "REC123"}]}}}}
    event_id = hashlib.sha256(json.dumps(cb_payload).encode()).hexdigest()
    await enqueue_jobs(pool, "mpesa", [(event_id, cb_payload)])
    job = await claim_next(pool)
    assert job is not None
    # first process should raise unknown and cause retry (pending)
    try:
        await process_job(app.state.ctx, job)
        assert False, "should have raised"
    except RuntimeError as e:
        assert "unknown CheckoutRequestID" in str(e)
    from app.webhook_jobs import retry_job
    await retry_job(pool, job, RuntimeError("unknown CheckoutRequestID unknown-checkout"))
    pending = await pool.fetchval("SELECT status FROM webhook_jobs WHERE id=$1", job["id"])
    assert pending == "pending"
    await pool.close()
    await http.aclose()

@pytest.mark.asyncio
async def test_ops_endpoints_require_key():
    from tests.harness import build
    pool, out, salon = await build(DSN)
    from app.main import app
    transport = httpx.ASGITransport(app=app)
    async with httpx.AsyncClient(transport=transport, base_url="http://test") as c:
        r = await c.get("/ops/payment-reviews")
        assert r.status_code in (401, 404)
        orig = app.state.ctx.settings.ops_api_key
        try:
            app.state.ctx.settings.ops_api_key = "0123456789abcdef"*4
            r = await c.get("/ops/payment-reviews", headers={"x-ops-key": "0123456789abcdef"*4})
            assert r.status_code == 200
            assert "items" in r.json()
            r = await c.get("/ops/payment-reviews", headers={"x-ops-key": "wrong"})
            assert r.status_code == 401
        finally:
            app.state.ctx.settings.ops_api_key = orig
    await pool.close()

@pytest.mark.asyncio
async def test_purge_expired():
    pool = await _pool(DSN)
    await pool.execute("INSERT INTO webhook_jobs (provider, event_id, payload, status, completed_at) VALUES ('whatsapp','old1','{}','processed', now() - interval '40 days')")
    await pool.execute("INSERT INTO inbound_messages (wa_message_id, wa_id, received_at) VALUES ('oldmsg','254700000001', now() - interval '100 days')")
    from app.webhook_jobs import purge_expired_data
    purged = await purge_expired_data(pool, webhook_days=30, inbound_days=90)
    assert purged["webhook_jobs"] >= 1
    assert purged["inbound_messages"] >= 1
    await pool.close()
