import asyncio
import hashlib
import json
import os
import re
import uuid
from datetime import datetime, timezone
from zoneinfo import ZoneInfo

import httpx
import pytest

from app.glance_api import PREFIX
from app.glance_sessions import (
    CODE_ALPHABET,
    GlanceAuthError,
    authenticate_session,
    exchange_pairing_code,
    generate_pairing_code,
    issue_pairing_code,
    pairing_digest,
    revoke_owner_session,
)
from app.handoffs import create_handoff, record_customer_message, resolve_handoff
from app.main import app
from tests.harness import build

DSN = os.environ.get("TEST_DATABASE_URL")
needs_db = pytest.mark.skipif(not DSN, reason="needs TEST_DATABASE_URL")
OWNER = "tg:700000001"
HMAC_KEY = "0123456789abcdef" * 4


def _settings(**overrides):
    values = {
        "glance_enabled": True,
        "glance_pairing_hmac_keyring": json.dumps({"1": HMAC_KEY}),
        "glance_pairing_hmac_active_version": 1,
        "glance_session_ttl_seconds": 7 * 24 * 60 * 60,
        "glance_exchange_window_seconds": 900,
        "glance_exchange_source_limit": 12,
        "glance_exchange_owner_limit": 10,
        "glance_pairing_generation_window_seconds": 3600,
        "glance_pairing_generation_limit": 5,
        "glance_read_limit_per_minute": 120,
        "glance_cors_origins": "https://glance.test",
    }
    values.update(overrides)
    return values


async def _built(**overrides):
    pool, outside, salon_id = await build(DSN, **_settings(**overrides))
    await pool.execute("UPDATE salons SET owner_wa_id=$2,tg_bot_id='tg:111' WHERE id=$1::uuid", salon_id, OWNER)
    return pool, outside, salon_id, app.state.settings


async def _client():
    return httpx.AsyncClient(transport=httpx.ASGITransport(app=app), base_url="http://test")


async def _make_case(pool, salon_id, customer, message="Can I speak to the owner?", name="Amina"):
    client = await pool.fetchrow(
        "INSERT INTO clients (salon_id,wa_id,name) VALUES ($1::uuid,$2,$3) RETURNING id::text,wa_id",
        salon_id, customer, name)
    return await create_handoff(pool, salon_id, client["id"], client["wa_id"], "human_request", message,
                                source_message_id="seed-" + customer)


async def _paired(pool, settings, salon_id, source="192.0.2.10", owner=OWNER):
    issued = await issue_pairing_code(pool, settings, owner, salon_id, "tg:111")
    exchanged = await exchange_pairing_code(pool, settings, issued["code"], "Test Glance", source)
    return issued, exchanged


@needs_db
@pytest.mark.asyncio
async def test_pairing_code_has_60_bit_crockford_format_and_versioned_hmac_storage():
    pool, outside, salon_id, settings = await _built()
    try:
        values = [generate_pairing_code() for _ in range(400)]
        assert len(set(values)) == 400
        assert all(len(code) == 12 and set(code) <= set(CODE_ALPHABET) for code in values)
        issued = await issue_pairing_code(pool, settings, OWNER, salon_id, "tg:111")
        row = await pool.fetchrow("SELECT code_digest,key_version FROM glance_pairing_codes WHERE salon_id=$1::uuid", salon_id)
        created = await pool.fetchrow("SELECT created_at,expires_at FROM glance_pairing_codes WHERE salon_id=$1::uuid", salon_id)
        assert row["key_version"] == 1
        assert bytes(row["code_digest"]) == pairing_digest(issued["code"], bytes.fromhex(HMAC_KEY))
        assert issued["code"].encode() not in bytes(row["code_digest"])
        assert issued["expires_at"].tzinfo is not None
        assert 299 <= (created["expires_at"] - created["created_at"]).total_seconds() <= 301
        assert len([m for m in outside.tg if m["method"] == "sendMessage"]) == 0
    finally:
        await pool.close()


@needs_db
@pytest.mark.asyncio
async def test_authenticated_telegram_pairing_selects_a_salon_and_never_traces_the_code(caplog):
    from app import owner

    pool, outside, salon_id, settings = await _built()
    try:
        other = await pool.fetchval(
            "INSERT INTO salons (name,wa_phone_number_id,tg_bot_id,owner_wa_id) VALUES ('Other Cuts','PID-OTHER','tg:111',$1) RETURNING id::text",
            OWNER)
        salon = dict(await pool.fetchrow("SELECT id::text,name,owner_wa_id,tg_bot_id FROM salons WHERE id=$1::uuid", salon_id))
        handled = await owner.handle(app.state.ctx, salon, OWNER, "glance pair", ZoneInfo("UTC"), datetime.now(timezone.utc))
        assert handled is True
        choice = outside.tg[-1]["reply_markup"]["inline_keyboard"]
        assert len(choice) == 2
        selected = next(button["callback_data"] for row in choice for button in row if other in button["callback_data"])
        assert await owner.handle_tap(app.state.ctx, salon, OWNER, selected)
        sent = outside.tg[-1]["text"]
        code = re.search(r"Code: ([0-9A-HJKMNP-TV-Z]{12})", sent).group(1)
        assert "5 minutes" in sent
        assert caplog.text.find(code) == -1
        assert await pool.fetchval("SELECT 1 FROM glance_pairing_codes WHERE salon_id=$1::uuid AND code_digest=$2",
                                   other, pairing_digest(code, bytes.fromhex(HMAC_KEY)))
        trace_bodies = await pool.fetch("SELECT body FROM conversation_trace_replies")
        assert all(code not in (row["body"] or "") for row in trace_bodies)
    finally:
        await pool.close()


@needs_db
@pytest.mark.asyncio
async def test_exchange_is_atomic_uniform_for_bad_expired_and_used_codes_and_hashes_tokens():
    pool, _, salon_id, settings = await _built()
    try:
        client = await _client()
        unknown = await client.post(PREFIX + "/pairing/exchange", json={"code": "000000000000", "device_label": "G2"})
        expired = await issue_pairing_code(pool, settings, OWNER, salon_id, "tg:111")
        await pool.execute("UPDATE glance_pairing_codes SET created_at=now()-interval '6 minutes',expires_at=now()-interval '1 second' WHERE code_digest=$1",
                           pairing_digest(expired["code"], bytes.fromhex(HMAC_KEY)))
        expired_resp = await client.post(PREFIX + "/pairing/exchange", json={"code": expired["code"], "device_label": "G2"})
        live = await issue_pairing_code(pool, settings, OWNER, salon_id, "tg:111")
        attempts = await asyncio.gather(*[
            client.post(PREFIX + "/pairing/exchange", json={"code": live["code"], "device_label": "G2"})
            for _ in range(2)
        ])
        successful = [response for response in attempts if response.status_code == 200]
        rejected = [response for response in attempts if response.status_code == 401]
        reused = await client.post(PREFIX + "/pairing/exchange", json={"code": live["code"], "device_label": "G2"})
        assert len(successful) == len(rejected) == 1
        assert unknown.status_code == expired_resp.status_code == reused.status_code == 401
        public_errors = [unknown.json()["error"], expired_resp.json()["error"], rejected[0].json()["error"], reused.json()["error"]]
        assert all(error == public_errors[0] for error in public_errors)
        data = successful[0].json()["data"]
        token_hash = await pool.fetchval("SELECT token_hash FROM glance_sessions WHERE salon_id=$1::uuid", salon_id)
        assert bytes(token_hash) == hashlib.sha256(data["access_token"].encode()).digest()
        assert data["access_token"].encode() not in bytes(token_hash)
        assert data["expires_at"].endswith("Z")
        assert await pool.fetchval("SELECT count(*) FROM glance_sessions WHERE salon_id=$1::uuid", salon_id) == 1
        assert await pool.fetchval("SELECT consumed_at IS NOT NULL FROM glance_pairing_codes WHERE code_digest=$1",
                                   pairing_digest(live["code"], bytes.fromhex(HMAC_KEY)))
        await client.aclose()
    finally:
        await pool.close()


@needs_db
@pytest.mark.asyncio
async def test_pairing_exchange_source_and_attributable_owner_limits_are_independent():
    pool, _, salon_id, settings = await _built(glance_exchange_source_limit=1, glance_exchange_owner_limit=1)
    try:
        other_owner = "tg:700000002"
        other_salon = await pool.fetchval(
            "INSERT INTO salons (name,wa_phone_number_id,tg_bot_id,owner_wa_id) VALUES ('Second','PID-SECOND','tg:111',$1) RETURNING id::text",
            other_owner)
        codes = []
        for owner, sid in ((OWNER, salon_id), (other_owner, other_salon)):
            issued = await issue_pairing_code(pool, settings, owner, sid, "tg:111")
            await pool.execute("UPDATE glance_pairing_codes SET created_at=now()-interval '6 minutes',expires_at=now()-interval '1 second' WHERE code_digest=$1",
                               pairing_digest(issued["code"], bytes.fromhex(HMAC_KEY)))
            codes.append(issued["code"])
        with pytest.raises(GlanceAuthError) as first:
            await exchange_pairing_code(pool, settings, codes[0], "G2", "198.51.100.1")
        assert first.value.status_code == 401
        with pytest.raises(GlanceAuthError) as owner_block:
            await exchange_pairing_code(pool, settings, codes[0], "G2", "198.51.100.2")
        assert owner_block.value.status_code == 429
        # A different owner from a different source is still allowed to make an attempt.
        with pytest.raises(GlanceAuthError) as unrelated:
            await exchange_pairing_code(pool, settings, codes[1], "G2", "198.51.100.3")
        assert unrelated.value.status_code == 401
        with pytest.raises(GlanceAuthError) as source_block:
            await exchange_pairing_code(pool, settings, "000000000000", "G2", "198.51.100.3")
        assert source_block.value.status_code == 429
        third_owner = "tg:700000003"
        third_salon = await pool.fetchval(
            "INSERT INTO salons (name,wa_phone_number_id,tg_bot_id,owner_wa_id) VALUES ('Third','PID-THIRD','tg:111',$1) RETURNING id::text",
            third_owner)
        third = await issue_pairing_code(pool, settings, third_owner, third_salon, "tg:111")
        await pool.execute("UPDATE glance_pairing_codes SET created_at=now()-interval '6 minutes',expires_at=now()-interval '1 second' WHERE code_digest=$1",
                           pairing_digest(third["code"], bytes.fromhex(HMAC_KEY)))
        with pytest.raises(GlanceAuthError) as still_available:
            await exchange_pairing_code(pool, settings, third["code"], "G2", "198.51.100.4")
        assert still_available.value.status_code == 401
    finally:
        await pool.close()


@needs_db
@pytest.mark.asyncio
async def test_pairing_hmac_key_rotation_can_consume_an_old_version_code():
    pool, _, salon_id, settings = await _built(
        glance_pairing_hmac_keyring=json.dumps({"1": HMAC_KEY, "2": "fedcba9876543210" * 4}),
        glance_pairing_hmac_active_version=2)
    try:
        issued = await issue_pairing_code(pool, settings, OWNER, salon_id, "tg:111")
        assert await pool.fetchval("SELECT key_version FROM glance_pairing_codes WHERE salon_id=$1::uuid", salon_id) == 2
        settings.glance_pairing_hmac_active_version = 1
        result = await exchange_pairing_code(pool, settings, issued["code"], "G2", "203.0.113.4")
        assert result["session_id"]
    finally:
        await pool.close()


@needs_db
@pytest.mark.asyncio
async def test_pairing_generation_limit_is_per_owner_and_secrets_fail_closed():
    pool, _, salon_id, settings = await _built(glance_pairing_generation_limit=1)
    try:
        await issue_pairing_code(pool, settings, OWNER, salon_id, "tg:111")
        with pytest.raises(GlanceAuthError) as owner_limited:
            await issue_pairing_code(pool, settings, OWNER, salon_id, "tg:111")
        assert owner_limited.value.status_code == 429
        unrelated = "tg:700000002"
        other_salon = await pool.fetchval(
            "INSERT INTO salons (name,wa_phone_number_id,tg_bot_id,owner_wa_id) VALUES ('Other Owner','PID-OTHER-OWNER','tg:111',$1) RETURNING id::text",
            unrelated)
        assert (await issue_pairing_code(pool, settings, unrelated, other_salon, "tg:111"))["code"]
        settings.glance_pairing_hmac_keyring = ""
        with pytest.raises(GlanceAuthError) as missing_secret:
            await exchange_pairing_code(pool, settings, "000000000000", "G2", "192.0.2.33")
        assert missing_secret.value.status_code == 503
    finally:
        await pool.close()


@needs_db
@pytest.mark.asyncio
async def test_disabled_public_glance_routes_fail_closed_with_common_response_headers():
    pool, _, salon_id, settings = await _built(glance_enabled=False)
    try:
        client = await _client()
        response = await client.get(PREFIX + "/inbox")
        assert response.status_code == 404
        assert response.json()["error"]["code"] == "not_found"
        assert response.headers["cache-control"] == "no-store"
        assert response.headers["x-request-id"] == response.json()["request_id"]
        await client.aclose()
    finally:
        await pool.close()


@needs_db
@pytest.mark.asyncio
async def test_session_expiry_revalidation_current_revocation_and_lost_device_owner_revocation():
    from app import owner

    pool, outside, salon_id, settings = await _built()
    try:
        _, paired = await _paired(pool, settings, salon_id)
        token = paired["access_token"]
        row = await pool.fetchrow("SELECT id::text,token_hash FROM glance_sessions WHERE salon_id=$1::uuid", salon_id)
        assert bytes(row["token_hash"]) == hashlib.sha256(token.encode()).digest()
        assert bytes(row["token_hash"]) != token.encode()
        salon = dict(await pool.fetchrow("SELECT id::text,name,owner_wa_id,tg_bot_id FROM salons WHERE id=$1::uuid", salon_id))
        await owner.handle(app.state.ctx, salon, OWNER, "glance devices", ZoneInfo("UTC"), datetime.now(timezone.utc))
        callback = outside.tg[-1]["reply_markup"]["inline_keyboard"][0][0]["callback_data"]
        assert "access_token" not in str(outside.tg[-1])
        assert await owner.handle_tap(app.state.ctx, salon, OWNER, callback)
        with pytest.raises(GlanceAuthError) as revoked:
            await authenticate_session(pool, settings, token)
        assert revoked.value.status_code == 403 and revoked.value.code == "session_revoked"

        _, current = await _paired(pool, settings, salon_id, source="192.0.2.11")
        client = await _client()
        auth = {"Authorization": f"Bearer {current['access_token']}"}
        first = await client.delete(PREFIX + "/sessions/current", headers=auth)
        second = await client.delete(PREFIX + "/sessions/current", headers=auth)
        denied = await client.get(PREFIX + "/inbox", headers=auth)
        assert first.status_code == second.status_code == 204
        assert denied.status_code == 403 and denied.json()["error"]["code"] == "session_revoked"
        await client.aclose()

        _, expired = await _paired(pool, settings, salon_id, source="192.0.2.12")
        await pool.execute("UPDATE glance_sessions SET created_at=now()-interval '8 days',expires_at=now()-interval '1 second' WHERE token_hash=$1",
                           hashlib.sha256(expired["access_token"].encode()).digest())
        with pytest.raises(GlanceAuthError) as expiry:
            await authenticate_session(pool, settings, expired["access_token"])
        assert expiry.value.code == "session_expired"
    finally:
        await pool.close()


@needs_db
@pytest.mark.asyncio
async def test_owner_change_revokes_access_and_marks_the_session():
    pool, _, salon_id, settings = await _built()
    try:
        _, paired = await _paired(pool, settings, salon_id)
        token_hash = hashlib.sha256(paired["access_token"].encode()).digest()
        await pool.execute("UPDATE salons SET owner_wa_id='tg:700000099' WHERE id=$1::uuid", salon_id)
        with pytest.raises(GlanceAuthError) as denied:
            await authenticate_session(pool, settings, paired["access_token"])
        assert denied.value.status_code == 403
        assert await pool.fetchval("SELECT revoked_at IS NOT NULL FROM glance_sessions WHERE token_hash=$1", token_hash)
    finally:
        await pool.close()


@needs_db
@pytest.mark.asyncio
async def test_inbox_and_case_api_are_session_tenant_scoped_and_enveloped():
    pool, _, salon_id, settings = await _built()
    try:
        other_salon = await pool.fetchval(
            "INSERT INTO salons (name,wa_phone_number_id,owner_wa_id) VALUES ('Other Salon','PID-OTHER','tg:700000099') RETURNING id::text")
        await pool.execute("UPDATE salons SET tg_bot_id='tg:111' WHERE id=$1::uuid", other_salon)
        case_a = await _make_case(pool, salon_id, "tg:800000001", "A customer message", "Amina")
        await _make_case(pool, other_salon, "tg:800000002", "Other tenant secret", "Other Customer")
        client_row = await pool.fetchrow("SELECT id::text FROM clients WHERE salon_id=$1::uuid AND wa_id='tg:800000001'", salon_id)
        case_closed = await _make_case(pool, salon_id, "tg:800000003", "Closed case", "Closed")
        await resolve_handoff(pool, salon_id, case_closed["id"], OWNER, "handled")
        _, paired = await _paired(pool, settings, salon_id)
        client = await _client()
        headers = {"Authorization": f"Bearer {paired['access_token']}"}
        inbox = await client.get(PREFIX + "/inbox?limit=20", headers=headers)
        body = inbox.json()
        assert inbox.status_code == 200 and set(body) == {"data"}
        assert [item["case_id"] for item in body["data"]["items"]] == [case_a["id"]]
        assert body["data"]["open_count"] == 1 and body["data"]["has_more"] is False
        assert body["data"]["next_cursor"] is None
        detail = await client.get(PREFIX + f"/cases/{case_a['id']}", headers=headers)
        assert detail.status_code == 200
        assert detail.json()["data"]["context"][0]["text"] == "A customer message"
        hidden = await client.get(PREFIX + f"/cases/{case_closed['id']}", headers=headers)
        assert hidden.status_code == 200  # detail is salon-scoped, not limited to open status
        cross = await client.get(PREFIX + f"/cases/{(await pool.fetchval('SELECT id::text FROM handoffs WHERE salon_id=$1::uuid', other_salon))}", headers=headers)
        assert cross.status_code == 404
        assert cross.json()["error"]["code"] == "case_not_found"
        assert cross.json()["request_id"] == cross.headers["x-request-id"]
        assert cross.headers["cache-control"] == "no-store"
        assert inbox.headers["x-request-id"].startswith("req_")
        assert inbox.headers["cache-control"] == "no-store"
        assert inbox.json()["data"]["items"][0]["created_at"].endswith("Z")
        await client.aclose()
    finally:
        await pool.close()


@needs_db
@pytest.mark.asyncio
async def test_case_context_uses_eleven_visible_event_lookahead_and_redacts_contact_details():
    pool, _, salon_id, settings = await _built()
    try:
        case = await _make_case(pool, salon_id, "tg:800000011", "Initial message", "Amina +254712345678\u202e")
        for index in range(10):
            await record_customer_message(pool, salon_id, "tg:800000011",
                                          f"Message {index} contact +25471100000{index:02d} receipt ABC1234567", f"event-{index}")
        sequence = await pool.fetchval("UPDATE handoffs SET event_seq=event_seq+1 WHERE id=$1::uuid RETURNING event_seq", case["id"])
        client_id = await pool.fetchval("SELECT client_id::text FROM handoffs WHERE id=$1::uuid", case["id"])
        await pool.execute(
            "INSERT INTO handoff_events (handoff_id,salon_id,client_id,sequence,event_type,actor_type,visibility,body) "
            "VALUES ($1::uuid,$2::uuid,$3::uuid,$4,'customer_message','owner','owner_internal','Internal only')",
            case["id"], salon_id, client_id, sequence)
        _, paired = await _paired(pool, settings, salon_id)
        client = await _client()
        headers = {"Authorization": f"Bearer {paired['access_token']}"}
        response = await client.get(PREFIX + f"/cases/{case['id']}", headers=headers)
        data = response.json()["data"]
        assert data["has_earlier"] is True
        assert len(data["context"]) == 10
        assert [event["sequence"] for event in data["context"]] == list(range(3, 13))
        assert "Internal only" not in str(data)
        assert "254711" not in str(data)
        assert "ABC1234567" not in str(data)
        assert "\u202e" not in data["customer_label"]
        assert len(data["customer_label"]) <= 24
        assert data["customer_label"].startswith("Amina")
        assert len(data["context"][0]["text"]) <= 4096
        assert all(event["created_at"].endswith("Z") for event in data["context"])
        # A second case with exactly ten eligible events remains has_earlier=false
        # even if it has extra owner-internal events.
        second = await _make_case(pool, salon_id, "tg:800000012", "Start", "Bina")
        for index in range(9):
            await record_customer_message(pool, salon_id, "tg:800000012", f"Visible {index}", f"visible-{index}")
        await pool.execute("UPDATE handoffs SET event_seq=event_seq+1 WHERE id=$1::uuid", second["id"])
        await pool.execute(
            "INSERT INTO handoff_events (handoff_id,salon_id,client_id,sequence,event_type,actor_type,visibility,body) "
            "SELECT h.id,h.salon_id,h.client_id,h.event_seq,'owner_notification','system','owner_internal','Internal only' "
            "FROM handoffs h WHERE h.id=$1::uuid", second["id"])
        response = await client.get(PREFIX + f"/cases/{second['id']}", headers=headers)
        assert response.json()["data"]["has_earlier"] is False
        assert len(response.json()["data"]["context"]) == 10
        await client.aclose()
    finally:
        await pool.close()


@needs_db
@pytest.mark.asyncio
async def test_case_status_is_lightweight_versioned_tenant_scoped_and_reports_latest_reply():
    pool, _, salon_id, settings = await _built()
    try:
        case = await _make_case(pool, salon_id, "tg:800000031", "Synthetic status context", "Amina")
        other_salon = await pool.fetchval(
            "INSERT INTO salons (name,wa_phone_number_id,owner_wa_id) "
            "VALUES ('Status Tenant','PID-STATUS-OTHER','tg:700000099') RETURNING id::text")
        other_case = await _make_case(pool, other_salon, "tg:800000032", "Other salon context", "Other")
        _, paired = await _paired(pool, settings, salon_id)
        client = await _client()
        headers = {"Authorization": f"Bearer {paired['access_token']}"}

        unauthenticated = await client.get(PREFIX + f"/cases/{case['id']}/status")
        assert unauthenticated.status_code == 401
        first = await client.get(PREFIX + f"/cases/{case['id']}/status", headers=headers)
        first_data = first.json()["data"]
        assert first.status_code == 200
        assert set(first_data) == {"case_id", "status", "version", "latest_reply", "case_updated_at"}
        assert first_data["case_id"] == case["id"] and first_data["version"] == 1
        assert first_data["status"] == "open" and first_data["latest_reply"] is None
        assert first_data["case_updated_at"].endswith("Z")
        assert "context" not in first_data and "customer_label" not in first_data
        assert first.headers["cache-control"] == "no-store"
        assert first.headers["x-request-id"].startswith("req_")

        await record_customer_message(pool, salon_id, "tg:800000031", "A new synthetic message", "status-new-message")
        bumped = await client.get(PREFIX + f"/cases/{case['id']}/status", headers=headers)
        assert bumped.json()["data"]["version"] == 2

        manual = await client.post(PREFIX + f"/cases/{case['id']}/drafts/manual",
                                   json={"expected_version": 2, "text": "The owner will confirm shortly."},
                                   headers=headers | {"Idempotency-Key": str(uuid.uuid4())})
        assert manual.status_code == 201
        approval = await client.post(PREFIX + f"/cases/{case['id']}/replies",
                                     json={"expected_version": 2, "draft_id": manual.json()["data"]["draft_id"],
                                           "text": "The owner will confirm shortly."},
                                     headers=headers | {"Idempotency-Key": str(uuid.uuid4())})
        assert approval.status_code == 202
        queued = await client.get(PREFIX + f"/cases/{case['id']}/status", headers=headers)
        queued_data = queued.json()["data"]
        assert queued_data["version"] == 3
        assert queued_data["latest_reply"]["reply_id"] == approval.json()["data"]["reply_id"]
        assert queued_data["latest_reply"]["state"] == "queued"
        assert set(queued_data["latest_reply"]) == {"reply_id", "state", "updated_at"}
        assert queued_data["case_updated_at"] >= bumped.json()["data"]["case_updated_at"]

        # Outbox state timestamps can advance without changing the case version/time.
        await pool.execute("UPDATE outbound_outbox SET updated_at=updated_at+interval '1 second' WHERE id=$1::uuid",
                           approval.json()["data"]["reply_id"])
        outbox_only = await client.get(PREFIX + f"/cases/{case['id']}/status", headers=headers)
        assert outbox_only.json()["data"]["version"] == queued_data["version"]
        assert outbox_only.json()["data"]["case_updated_at"] == queued_data["case_updated_at"]
        assert outbox_only.json()["data"]["latest_reply"]["updated_at"] != queued_data["latest_reply"]["updated_at"]

        foreign = await client.get(PREFIX + f"/cases/{other_case['id']}/status", headers=headers)
        missing = await client.get(PREFIX + "/cases/00000000-0000-4000-8000-000000000001/status", headers=headers)
        assert foreign.status_code == missing.status_code == 404
        assert foreign.json()["error"] == missing.json()["error"]
        assert foreign.json()["error"]["code"] == "case_not_found"
        await client.aclose()
    finally:
        await pool.close()


@needs_db
@pytest.mark.asyncio
async def test_case_status_uses_existing_per_session_read_quota():
    pool, _, salon_id, settings = await _built(glance_read_limit_per_minute=90)
    try:
        case = await _make_case(pool, salon_id, "tg:800000033", "Synthetic rate limit case", "Nia")
        _, paired = await _paired(pool, settings, salon_id)
        from app.glance_sessions import GlanceSession, check_read_quota
        row = await pool.fetchrow(
            "SELECT gs.id::text,gs.salon_id::text,gs.owner_identity,gs.device_label,s.name AS salon_name,"
            "gs.expires_at,gs.created_at,gs.last_used_at,gs.revoked_at "
            "FROM glance_sessions gs JOIN salons s ON s.id=gs.salon_id WHERE gs.token_hash=$1",
            hashlib.sha256(paired["access_token"].encode()).digest())
        session = GlanceSession(**dict(row))
        for _ in range(90):
            await check_read_quota(pool, settings, session)
        client = await _client()
        headers = {"Authorization": f"Bearer {paired['access_token']}"}
        limited = await client.get(PREFIX + f"/cases/{case['id']}/status", headers=headers)
        assert limited.status_code == 429
        assert limited.json()["error"]["code"] == "rate_limited"
        assert int(limited.headers["retry-after"]) >= 1
        await client.aclose()
    finally:
        await pool.close()


@needs_db
@pytest.mark.asyncio
async def test_read_session_quota_is_per_session_and_at_least_ninety_requests_per_minute():
    pool, _, salon_id, settings = await _built(glance_read_limit_per_minute=90)
    try:
        _, paired = await _paired(pool, settings, salon_id)
        row = await pool.fetchrow("SELECT gs.id::text,gs.salon_id::text,gs.owner_identity,gs.device_label,s.name AS salon_name,gs.expires_at,gs.created_at,gs.last_used_at,gs.revoked_at FROM glance_sessions gs JOIN salons s ON s.id=gs.salon_id WHERE gs.salon_id=$1::uuid", salon_id)
        from app.glance_sessions import GlanceSession, check_read_quota
        session = GlanceSession(**dict(row))
        for _ in range(90):
            await check_read_quota(pool, settings, session)
        second_salon = await pool.fetchval(
            "INSERT INTO salons (name,wa_phone_number_id,tg_bot_id,owner_wa_id) VALUES ('Second','PID-2','tg:111',$1) RETURNING id::text", "tg:700000002")
        _, other = await _paired(pool, settings, second_salon, source="192.0.2.99", owner="tg:700000002")
        other_row = await pool.fetchrow("SELECT gs.id::text,s.id::text AS salon_id,gs.owner_identity,gs.device_label,s.name AS salon_name,gs.expires_at,gs.created_at,gs.last_used_at,gs.revoked_at FROM glance_sessions gs JOIN salons s ON s.id=gs.salon_id WHERE gs.token_hash=$1", hashlib.sha256(other["access_token"].encode()).digest())
        other_session = GlanceSession(**dict(other_row))
        await check_read_quota(pool, settings, other_session)
        assert other_session.id != session.id
    finally:
        await pool.close()


@needs_db
@pytest.mark.asyncio
async def test_cors_preflight_is_unauthenticated_and_real_requests_still_require_bearer_auth():
    pool, _, salon_id, settings = await _built(glance_cors_origins="https://glance.test")
    try:
        _, paired = await _paired(pool, settings, salon_id)
        client = await _client()
        origin = {"Origin": "https://glance.test"}
        preflight = await client.options(PREFIX + "/inbox", headers=origin | {
            "Access-Control-Request-Method": "GET",
            "Access-Control-Request-Headers": "authorization,content-type",
        })
        assert preflight.status_code == 204
        assert preflight.headers["access-control-allow-origin"] == "https://glance.test"
        assert preflight.headers["access-control-allow-methods"] == "GET, POST, DELETE, OPTIONS"
        assert "authorization" in preflight.headers["access-control-allow-headers"].lower()
        assert preflight.headers["access-control-max-age"] == "600"
        assert "access-control-allow-credentials" not in preflight.headers
        unauthenticated = await client.get(PREFIX + "/inbox", headers=origin)
        assert unauthenticated.status_code == 401
        assert unauthenticated.headers["access-control-allow-origin"] == "https://glance.test"
        assert "Retry-After" not in unauthenticated.headers
        authorized = await client.get(PREFIX + "/inbox", headers=origin | {"Authorization": f"Bearer {paired['access_token']}"})
        assert authorized.status_code == 200
        assert authorized.headers["access-control-expose-headers"] == "Retry-After, X-Request-ID"
        rejected = await client.options(PREFIX + "/inbox", headers={
            "Origin": "https://other.test", "Access-Control-Request-Method": "GET",
            "Access-Control-Request-Headers": "authorization",
        })
        assert rejected.status_code == 403
        assert rejected.json()["error"]["code"] == "cors_rejected"
        settings.glance_cors_origins = ""
        settings.glance_cors_wildcard_bearer = True
        wildcard = await client.options(PREFIX + "/inbox", headers={
            "Origin": "null", "Access-Control-Request-Method": "POST",
            "Access-Control-Request-Headers": "authorization,content-type,idempotency-key",
        })
        assert wildcard.status_code == 204
        assert wildcard.headers["access-control-allow-origin"] == "*"
        assert "access-control-allow-credentials" not in wildcard.headers
        exchange_wildcard = await client.options(PREFIX + "/pairing/exchange", headers={
            "Origin": "null", "Access-Control-Request-Method": "POST",
            "Access-Control-Request-Headers": "content-type",
        })
        assert exchange_wildcard.status_code == 403
        await client.aclose()
    finally:
        await pool.close()
