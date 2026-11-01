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
async def test_per_sender_capacity_covers_telegram_and_counts_the_whole_batch():
    pool = await _pool(DSN)
    await enqueue_jobs(pool, "telegram", [("t1", {"sender": "tg:5001", "text": "hi"})], max_pending=100, max_pending_per_sender=1)
    try:
        await enqueue_jobs(pool, "telegram", [("t2", {"sender": "tg:5001", "text": "hi again"})], max_pending=100, max_pending_per_sender=1)
        assert False, "should have raised"
    except QueueCapacityError:
        pass
    await enqueue_jobs(pool, "telegram", [("t3", {"sender": "tg:5002", "text": "hi"})], max_pending=100, max_pending_per_sender=1)
    try:
        await enqueue_jobs(pool, "whatsapp", [("w1", {"wa_id": "254700000001"}), ("w2", {"wa_id": "254700000001"}), ("w3", {"wa_id": "254700000001"})],
                           max_pending=100, max_pending_per_sender=2)
        assert False, "a batch that overshoots the cap should have raised"
    except QueueCapacityError:
        pass
    assert await pool.fetchval("SELECT count(*) FROM webhook_jobs") == 2
    await pool.close()


@pytest.mark.asyncio
async def test_ordering_keys_do_not_cross_providers():
    from app.webhook_jobs import ordering_key
    wa = ordering_key("whatsapp", {"phone_number_id": "PID", "sender": "254700000001"})
    tg = ordering_key("telegram", {"phone_number_id": "PID", "sender": "254700000001"})
    assert wa and tg and wa != tg
    assert ordering_key("mpesa", {}) is None


@pytest.mark.asyncio
async def test_malformed_telegram_job_is_parked_not_retried_forever():
    from app.webhook_jobs import PermanentJobError, _process_claimed_job
    pool = await _pool(DSN)
    job = {"id": 1, "provider": "telegram", "event_id": "x", "payload": {"text": "no sender here"}, "attempts": 1}
    try:
        await _process_claimed_job(SimpleNamespace(pool=pool), job)
        assert False, "should have raised"
    except PermanentJobError:
        pass
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
async def test_reserialized_mpesa_callback_redelivers_into_the_same_job():
    import json
    from tests.harness import build, callback, client
    from tests.test_payments import deliver
    pool, out, salon = await build(DSN)
    body = callback("ws_CO_9", receipt="R9")
    async with client() as c:
        assert (await deliver(c, body)).status_code == 200
        raw = json.dumps(body, indent=2, sort_keys=True).encode()
        import hashlib, hmac
        from tests.harness import CB_SECRET
        sig = "sha256=" + hmac.new(CB_SECRET.encode(), raw, hashlib.sha256).hexdigest()
        assert (await c.post("/mpesa/callback/" + CB_SECRET, content=raw,
                             headers={"x-hub-signature-256": sig, "content-type": "application/json"})).status_code == 200
    assert await pool.fetchval("SELECT count(*) FROM webhook_jobs WHERE provider='mpesa'") == 1
    assert await pool.fetchval("SELECT count(*) FROM mpesa_callbacks") == 1
    await pool.close()


@pytest.mark.asyncio
async def test_unmatched_payments_can_be_resolved_by_ops():
    from tests.harness import build
    pool, out, salon = await build(DSN)
    from app.main import app
    uid = await pool.fetchval("INSERT INTO unmatched_payments (checkout_request_id, result_code, amount) VALUES ('ws_X', 0, 200) RETURNING id::text")
    transport = httpx.ASGITransport(app=app)
    async with httpx.AsyncClient(transport=transport, base_url="http://test") as c:
        assert (await c.post(f"/ops/unmatched-payments/{uid}/resolve", json={})).status_code in (401, 404)
        orig = app.state.ctx.settings.ops_api_key
        try:
            app.state.ctx.settings.ops_api_key = "0123456789abcdef" * 4
            key = {"x-ops-key": "0123456789abcdef" * 4}
            assert (await c.post("/ops/unmatched-payments/not-a-uuid/resolve", json={}, headers=key)).status_code == 422
            assert (await c.post("/ops/unmatched-payments/00000000-0000-0000-0000-000000000000/resolve", json={}, headers=key)).status_code == 404
            r = await c.post(f"/ops/unmatched-payments/{uid}/resolve", json={"resolution_note": "refunded by hand"}, headers=key)
            assert r.json() == {"id": uid, "status": "resolved"}
            assert (await c.post(f"/ops/unmatched-payments/{uid}/resolve", json={}, headers=key)).status_code == 409
            r = await c.get("/ops/unmatched-payments", headers=key)
            assert r.json() == {"items": []}
        finally:
            app.state.ctx.settings.ops_api_key = orig
    await pool.close()


@pytest.mark.asyncio
async def test_claim_and_process_whatsapp():
    from tests.harness import build, post, client
    pool, out, salon = await build(DSN)
    from app.main import app
    day = open_day()
    out.script["fade kesho mchana"] = {"intent": "book", "service": "Fade", "date": open_day(), "window": "afternoon", "language": "en"}
    payload = {"id": "wamid.test-claim", "sender": "254712345678", "phone_number_id": "PID1", "kind": "text", "text": "Fade kesho mchana", "wa_id": "254712345678"}
    await enqueue_jobs(pool, "whatsapp", [(payload["id"], payload)])
    job = await claim_next(pool)
    assert job is not None
    assert job["event_id"] == "wamid.test-claim"
    await process_job(app.state.ctx, job)
    assert await pool.fetchval("SELECT status FROM webhook_jobs WHERE id=$1", job["id"]) == "processed"
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
    from app.ai.llm import Extractor
    from app.channels.telegram import Channels, Telegram
    from app.ai.vision import StyleMatcher
    from app.ai.voice import Transcriber
    from app.channels.whatsapp import WhatsApp
    import httpx
    out = Outside()
    http = httpx.AsyncClient(transport=httpx.MockTransport(out.handler))
    settings = Settings(_env_file=None, database_url=DSN, wa_app_secret="s", wa_verify_token="v", wa_access_token="tok", llm_base_url="https://primary.test/v1", llm_api_key="k", llm_model="m", mpesa_consumer_key="ck", mpesa_consumer_secret="cs", mpesa_passkey="pk", mpesa_callback_secret="cbsec", public_base_url="https://t.test", mpesa_verify_callbacks=False)
    wa = WhatsApp(http, settings.graph_version)
    app.state.settings = settings
    app.state.pool = pool
    app.state.wa = wa
    app.state.ctx = Ctx(settings, pool, Channels(wa, None), Extractor(http, settings), None, Daraja(http, settings), Transcriber(http, settings), StyleMatcher(http, settings, Extractor(http, settings).endpoints))
    cb_payload = {"Body": {"stkCallback": {"CheckoutRequestID": "unknown-checkout", "ResultCode": 0, "ResultDesc": "ok", "CallbackMetadata": {"Item": [{"Name": "Amount", "Value": 200}, {"Name": "PhoneNumber", "Value": "254712345678"}, {"Name": "MpesaReceiptNumber", "Value": "REC123"}]}}}}
    event_id = hashlib.sha256(json.dumps(cb_payload).encode()).hexdigest()
    await enqueue_jobs(pool, "mpesa", [(event_id, cb_payload)])
    job = await claim_next(pool)
    assert job is not None
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
async def test_payment_review_resolutions_are_atomic_and_single_winner():
    from tests.harness import build
    from app.main import app
    pool, out, salon = await build(DSN)
    staff = await pool.fetchval("SELECT id::text FROM staff LIMIT 1")
    service = await pool.fetchval("SELECT id::text FROM services LIMIT 1")
    client_id = await pool.fetchval(
        "INSERT INTO clients (salon_id, wa_id) VALUES ($1::uuid,'254711000099') RETURNING id::text", salon)
    starts = datetime.datetime.now(datetime.timezone.utc) + datetime.timedelta(days=2)
    booking = await pool.fetchval(
        "INSERT INTO bookings (salon_id,staff_id,service_id,client_id,starts_at,ends_at,status) "
        "VALUES ($1::uuid,$2::uuid,$3::uuid,$4::uuid,$5,$6,'confirmed') RETURNING id::text",
        salon, staff, service, client_id, starts, starts + datetime.timedelta(minutes=30))
    payment = await pool.fetchval(
        "INSERT INTO payments (booking_id,salon_id,phone,amount_kes,status,checkout_request_id) "
        "VALUES ($1::uuid,$2::uuid,'254711000099',200,'needs_refund','review-race') RETURNING id::text",
        booking, salon)
    review = await pool.fetchval(
        "INSERT INTO payment_reviews (payment_id,reason,status,expected_amount,received_amount) "
        "VALUES ($1::uuid,'payment_unresolved','refund_due',200,200) RETURNING id::text", payment)
    transport = httpx.ASGITransport(app=app)
    key_value = "0123456789abcdef" * 4
    original_key = app.state.ctx.settings.ops_api_key
    app.state.ctx.settings.ops_api_key = key_value
    try:
        async with httpx.AsyncClient(transport=transport, base_url="http://test") as c:
            responses = await asyncio.gather(
                c.post(f"/ops/payment-reviews/{review}/resolve", json={"action": "refunded"}, headers={"x-ops-key": key_value}),
                c.post(f"/ops/payment-reviews/{review}/resolve", json={"action": "closed_no_refund"}, headers={"x-ops-key": key_value}),
            )
        assert sorted(r.status_code for r in responses) == [200, 409]
        statuses = await pool.fetchrow(
            "SELECT r.status AS review, p.status AS payment FROM payment_reviews r "
            "JOIN payments p ON p.id=r.payment_id WHERE r.id=$1::uuid", review)
        assert (statuses["review"] == "refunded") == (statuses["payment"] == "refunded")
    finally:
        app.state.ctx.settings.ops_api_key = original_key
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
