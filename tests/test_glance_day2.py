import asyncio
import os
import uuid
from datetime import datetime, timedelta, timezone
from types import SimpleNamespace

import pytest

from app.agent.client import Call, Step
from app.glance_api import PREFIX
from app.handoff_outbox import _claim_next
from app.handoffs import create_handoff, record_customer_message
from app.main import app
from tests.test_glance_day1 import OWNER, _built, _client, _make_case, _paired

DSN = os.environ.get("TEST_DATABASE_URL")
needs_db = pytest.mark.skipif(not DSN, reason="needs TEST_DATABASE_URL")


class FixedModel:
    def __init__(self, *steps):
        self.steps = list(steps)
        self.calls = 0
        self.prompts = []

    async def step(self, system, messages, tools):
        assert tools == []
        self.calls += 1
        self.prompts.append(messages[0]["content"])
        result = self.steps[min(self.calls - 1, len(self.steps) - 1)]
        if callable(result):
            return await result()
        return result


async def _session(pool, settings, salon_id):
    _, paired = await _paired(pool, settings, salon_id)
    return {"Authorization": f"Bearer {paired['access_token']}"}, paired


def _key():
    return str(uuid.uuid4())


@needs_db
@pytest.mark.asyncio
async def test_ai_draft_route_is_tool_free_payload_bound_and_replays_without_a_second_model_call():
    pool, _, salon_id, settings = await _built()
    try:
        case = await _make_case(pool, salon_id, "tg:810000001")
        headers, _ = await _session(pool, settings, salon_id)
        model = FixedModel(Step(text="Thanks for explaining. What would help resolve this?", model="internal-model-name"))
        app.state.ctx.agent = model
        key = _key()
        client = await _client()
        first = await client.post(PREFIX + f"/cases/{case['id']}/drafts", json={"expected_version": 1},
                                  headers=headers | {"Idempotency-Key": key})
        replay = await client.post(PREFIX + f"/cases/{case['id']}/drafts", json={"expected_version": 1},
                                   headers=headers | {"Idempotency-Key": key})
        conflict = await client.post(PREFIX + f"/cases/{case['id']}/drafts", json={"expected_version": 2},
                                     headers=headers | {"Idempotency-Key": key})
        assert first.status_code == replay.status_code == 200
        assert first.json() == replay.json()
        assert first.json()["data"]["source"] == "ai"
        assert "model" not in str(first.json()).lower()
        assert first.json()["data"]["based_on_version"] == 1
        assert first.headers["cache-control"] == "no-store"
        assert first.headers["x-request-id"].startswith("req_")
        assert model.calls == 1
        assert conflict.status_code == 409 and conflict.json()["error"]["code"] == "idempotency_conflict"
        row = await pool.fetchrow("SELECT source,model_id,expires_at-created_at AS lifetime FROM handoff_drafts WHERE id=$1::uuid",
                                  first.json()["data"]["draft_id"])
        assert row["source"] == "ai" and row["model_id"] == "internal-model-name"
        assert row["lifetime"].total_seconds() == pytest.approx(1800, abs=2)
        await client.aclose()
    finally:
        await pool.close()


@needs_db
@pytest.mark.asyncio
async def test_draft_generation_in_progress_timeout_and_tool_attempt_are_safe_and_idempotent(monkeypatch):
    pool, _, salon_id, settings = await _built()
    try:
        case = await _make_case(pool, salon_id, "tg:810000002")
        headers, _ = await _session(pool, settings, salon_id)
        started, release = asyncio.Event(), asyncio.Event()

        async def slow_step():
            started.set()
            await release.wait()
            return Step(text="Thanks for reaching out. How can we help?", model="test-model")

        model = FixedModel(slow_step)
        app.state.ctx.agent = model
        key = _key()
        client = await _client()
        first_task = asyncio.create_task(client.post(
            PREFIX + f"/cases/{case['id']}/drafts", json={"expected_version": 1},
            headers=headers | {"Idempotency-Key": key}))
        await asyncio.wait_for(started.wait(), timeout=3)
        running = await client.post(PREFIX + f"/cases/{case['id']}/drafts", json={"expected_version": 1},
                                    headers=headers | {"Idempotency-Key": key})
        assert running.status_code == 409 and running.json()["error"]["code"] == "request_in_progress"
        assert running.headers["retry-after"] == "1"
        release.set()
        assert (await first_task).status_code == 200

        import app.glance_api as glance_api
        monkeypatch.setattr(glance_api.handoff_drafts, "GLANCE_MODEL_TIMEOUT_SECONDS", 0.01)
        async def never_finishes():
            await asyncio.Event().wait()

        timeout_model = FixedModel(never_finishes)
        app.state.ctx.agent = timeout_model
        timeout_key = _key()
        timeout = await client.post(PREFIX + f"/cases/{case['id']}/drafts", json={"expected_version": 1},
                                    headers=headers | {"Idempotency-Key": timeout_key})
        assert timeout.status_code == 504 and timeout.json()["error"]["code"] == "draft_timeout"
        timeout_replay = await client.post(PREFIX + f"/cases/{case['id']}/drafts", json={"expected_version": 1},
                                           headers=headers | {"Idempotency-Key": timeout_key})
        assert timeout_replay.status_code == 504 and timeout_model.calls == 1

        tool_model = FixedModel(Step(text="No action needed.", calls=[Call("call-1", "reserve_slot", {})], model="test-model"))
        app.state.ctx.agent = tool_model
        tool_reply = await client.post(PREFIX + f"/cases/{case['id']}/drafts", json={"expected_version": 1},
                                       headers=headers | {"Idempotency-Key": _key()})
        assert tool_reply.status_code == 422 and tool_reply.json()["error"]["code"] == "draft_rejected"
        assert await pool.fetchval("SELECT count(*) FROM handoff_drafts") == 1
        await client.aclose()
    finally:
        await pool.close()


@needs_db
@pytest.mark.asyncio
async def test_manual_draft_normalizes_owner_text_and_expired_draft_cannot_be_approved():
    pool, _, salon_id, settings = await _built()
    try:
        case = await _make_case(pool, salon_id, "tg:810000003")
        headers, _ = await _session(pool, settings, salon_id)
        model = FixedModel(Step(text="unused", model="unused"))
        app.state.ctx.agent = model
        client = await _client()
        key = _key()
        manual = await client.post(
            PREFIX + f"/cases/{case['id']}/drafts/manual",
            json={"expected_version": 1, "text": "  Cafe\u0301 is ready.  "},
            headers=headers | {"Idempotency-Key": key})
        assert manual.status_code == 201
        data = manual.json()["data"]
        assert data["text"] == "Café is ready." and data["source"] == "owner"
        row = await pool.fetchrow("SELECT source,requested_by,model_id,expires_at-created_at AS lifetime FROM handoff_drafts WHERE id=$1::uuid",
                                  data["draft_id"])
        assert tuple(row[:3]) == ("owner", OWNER, "owner_manual")
        assert row["lifetime"].total_seconds() == pytest.approx(1800, abs=2)
        assert model.calls == 0
        conflict = await client.post(
            PREFIX + f"/cases/{case['id']}/drafts/manual",
            json={"expected_version": 1, "text": "Different text"},
            headers=headers | {"Idempotency-Key": key})
        assert conflict.status_code == 409 and conflict.json()["error"]["code"] == "idempotency_conflict"
        await pool.execute("UPDATE handoff_drafts SET expires_at=now()-interval '1 second' WHERE id=$1::uuid", data["draft_id"])
        approval = await client.post(
            PREFIX + f"/cases/{case['id']}/replies",
            json={"expected_version": 1, "draft_id": data["draft_id"], "text": data["text"]},
            headers=headers | {"Idempotency-Key": _key()})
        assert approval.status_code == 409 and approval.json()["error"]["code"] == "draft_expired"
        invalid = await client.post(
            PREFIX + f"/cases/{case['id']}/drafts/manual",
            json={"expected_version": 1, "text": "\t\n"}, headers=headers | {"Idempotency-Key": _key()})
        assert invalid.status_code == 422 and invalid.json()["error"]["code"] == "invalid_text"
        await client.aclose()
    finally:
        await pool.close()


@needs_db
@pytest.mark.asyncio
async def test_revision_stores_owner_instruction_supersedes_only_after_success_and_replays():
    pool, _, salon_id, settings = await _built()
    try:
        case = await _make_case(pool, salon_id, "tg:810000004")
        headers, _ = await _session(pool, settings, salon_id)
        model = FixedModel(
            Step(text="Thanks for reaching out. What would help?", model="private-model"),
            Step(text="Payment received, thank you.", model="private-model"),
            Step(text="We welcome walk-ins until 17:00.", model="private-model"))
        app.state.ctx.agent = model
        client = await _client()
        initial = await client.post(PREFIX + f"/cases/{case['id']}/drafts", json={"expected_version": 1},
                                    headers=headers | {"Idempotency-Key": _key()})
        original_id = initial.json()["data"]["draft_id"]
        instruction = "Please tell them we welcome walk-ins until 17:00."
        revision_key = _key()
        failed = await client.post(
            PREFIX + f"/cases/{case['id']}/drafts/{original_id}/revise",
            json={"expected_version": 1, "instruction": instruction},
            headers=headers | {"Idempotency-Key": _key()})
        assert failed.status_code == 422 and failed.json()["error"]["code"] == "draft_rejected"
        assert await pool.fetchval("SELECT superseded_at FROM handoff_drafts WHERE id=$1::uuid", original_id) is None
        revised = await client.post(
            PREFIX + f"/cases/{case['id']}/drafts/{original_id}/revise",
            json={"expected_version": 1, "instruction": instruction},
            headers=headers | {"Idempotency-Key": revision_key})
        replay = await client.post(
            PREFIX + f"/cases/{case['id']}/drafts/{original_id}/revise",
            json={"expected_version": 1, "instruction": instruction},
            headers=headers | {"Idempotency-Key": revision_key})
        assert revised.status_code == 200 and revised.json() == replay.json()
        data = revised.json()["data"]
        assert data["draft_id"] != original_id and data["source"] == "ai"
        assert data["text"] == "We welcome walk-ins until 17:00."
        rows = await pool.fetch("SELECT id::text,source,instruction,superseded_at FROM handoff_drafts ORDER BY created_at")
        assert rows[0]["superseded_at"] is not None
        assert rows[1]["instruction"] == instruction and rows[1]["source"] == "ai"
        assert model.calls == 3
        assert "Authenticated owner instruction" in model.prompts[2]
        await client.aclose()
    finally:
        await pool.close()


@needs_db
@pytest.mark.asyncio
async def test_stale_case_after_inference_does_not_persist_a_draft():
    pool, _, salon_id, settings = await _built()
    try:
        case = await _make_case(pool, salon_id, "tg:810000005")
        headers, _ = await _session(pool, settings, salon_id)

        async def racing_step():
            await record_customer_message(pool, salon_id, "tg:810000005", "A new synthetic detail", "race-message")
            return Step(text="Thanks for the update. What else can we help with?", model="test-model")

        app.state.ctx.agent = FixedModel(racing_step)
        client = await _client()
        response = await client.post(PREFIX + f"/cases/{case['id']}/drafts", json={"expected_version": 1},
                                     headers=headers | {"Idempotency-Key": _key()})
        assert response.status_code == 409 and response.json()["error"]["code"] == "stale_case_version"
        assert await pool.fetchval("SELECT count(*) FROM handoff_drafts") == 0
        await client.aclose()
    finally:
        await pool.close()


@needs_db
@pytest.mark.asyncio
async def test_owner_instruction_cannot_override_a_structured_booking_record():
    pool, _, salon_id, settings = await _built()
    try:
        case = await _make_case(pool, salon_id, "tg:810000010")
        client_row = await pool.fetchrow(
            "SELECT id::text FROM clients WHERE salon_id=$1::uuid AND wa_id=$2", salon_id, "tg:810000010")
        staff_id = await pool.fetchval("SELECT id::text FROM staff WHERE salon_id=$1::uuid LIMIT 1", salon_id)
        service_id = await pool.fetchval("SELECT id::text FROM services WHERE salon_id=$1::uuid LIMIT 1", salon_id)
        await pool.execute(
            "INSERT INTO bookings (salon_id,staff_id,service_id,client_id,starts_at,ends_at,status) "
            "VALUES ($1::uuid,$2::uuid,$3::uuid,$4::uuid,now()+interval '1 day',now()+interval '1 day 1 hour','held')",
            salon_id, staff_id, service_id, client_row["id"])
        headers, _ = await _session(pool, settings, salon_id)
        model = FixedModel(
            Step(text="Thanks for reaching out. How can we help?", model="test-model"),
            Step(text="Your appointment is confirmed.", model="test-model"))
        app.state.ctx.agent = model
        client = await _client()
        initial = await client.post(PREFIX + f"/cases/{case['id']}/drafts", json={"expected_version": 1},
                                    headers=headers | {"Idempotency-Key": _key()})
        original_id = initial.json()["data"]["draft_id"]
        response = await client.post(
            PREFIX + f"/cases/{case['id']}/drafts/{original_id}/revise",
            json={"expected_version": 1, "instruction": "Tell them their appointment is confirmed."},
            headers=headers | {"Idempotency-Key": _key()})
        assert response.status_code == 422
        assert response.json()["error"]["code"] == "instruction_conflicts_with_record"
        assert await pool.fetchval("SELECT superseded_at FROM handoff_drafts WHERE id=$1::uuid", original_id) is None
        assert model.calls == 1
        await client.aclose()
    finally:
        await pool.close()


@needs_db
@pytest.mark.asyncio
async def test_reply_event_outbox_and_version_roll_back_together_if_queue_insert_fails():
    pool, _, salon_id, settings = await _built()
    try:
        case = await _make_case(pool, salon_id, "tg:810000011")
        headers, _ = await _session(pool, settings, salon_id)
        client = await _client()
        manual = await client.post(PREFIX + f"/cases/{case['id']}/drafts/manual",
                                   json={"expected_version": 1, "text": "We will review this."},
                                   headers=headers | {"Idempotency-Key": _key()})
        draft_id = manual.json()["data"]["draft_id"]
        event_count = await pool.fetchval("SELECT count(*) FROM handoff_events WHERE handoff_id=$1::uuid", case["id"])
        await pool.execute("""
            CREATE FUNCTION test_reject_glance_outbox_insert() RETURNS trigger LANGUAGE plpgsql AS $$
            BEGIN RAISE EXCEPTION 'injected outbox insert failure'; END $$;
            CREATE TRIGGER test_reject_glance_outbox_insert BEFORE INSERT ON outbound_outbox
            FOR EACH ROW EXECUTE FUNCTION test_reject_glance_outbox_insert();
        """)
        key = _key()
        payload = {"expected_version": 1, "draft_id": draft_id, "text": "We will review this."}
        try:
            failed = await client.post(PREFIX + f"/cases/{case['id']}/replies", json=payload,
                                       headers=headers | {"Idempotency-Key": key})
        finally:
            await pool.execute("DROP TRIGGER test_reject_glance_outbox_insert ON outbound_outbox")
            await pool.execute("DROP FUNCTION test_reject_glance_outbox_insert()")
        assert failed.status_code == 503 and failed.json()["error"]["code"] == "reply_unavailable"
        assert await pool.fetchval("SELECT count(*) FROM handoff_events WHERE handoff_id=$1::uuid", case["id"]) == event_count
        assert await pool.fetchval("SELECT version FROM handoffs WHERE id=$1::uuid", case["id"]) == 1
        assert await pool.fetchval("SELECT count(*) FROM outbound_outbox WHERE handoff_id=$1::uuid", case["id"]) == 0
        retried = await client.post(PREFIX + f"/cases/{case['id']}/replies", json=payload,
                                    headers=headers | {"Idempotency-Key": key})
        assert retried.status_code == 202 and retried.json()["data"]["case_version"] == 2
        await client.aclose()
    finally:
        await pool.close()


@needs_db
@pytest.mark.asyncio
async def test_approval_is_exact_transactional_resource_idempotent_and_status_is_tenant_scoped():
    pool, _, salon_id, settings = await _built()
    try:
        case = await _make_case(pool, salon_id, "tg:810000006")
        headers, paired = await _session(pool, settings, salon_id)
        client = await _client()
        manual = await client.post(
            PREFIX + f"/cases/{case['id']}/drafts/manual",
            json={"expected_version": 1, "text": "The owner will review this today."},
            headers=headers | {"Idempotency-Key": _key()})
        draft_id = manual.json()["data"]["draft_id"]
        reply_key = _key()
        final = "I've reviewed the exact wording and will follow up today."
        approval = await client.post(
            PREFIX + f"/cases/{case['id']}/replies",
            json={"expected_version": 1, "draft_id": draft_id, "text": final},
            headers=headers | {"Idempotency-Key": reply_key})
        assert approval.status_code == 202
        data = approval.json()["data"]
        assert data["state"] == "queued" and data["case_version"] == 2
        event = await pool.fetchrow(
            "SELECT e.body,o.reply_source,o.approved_draft_text,o.approved_glance_session_id::text,o.approved_device_label "
            "FROM outbound_outbox o JOIN handoff_events e ON e.id=o.approved_event_id WHERE o.id=$1::uuid", data["reply_id"])
        assert tuple(event) == (final, "owner", "The owner will review this today.", paired["session_id"], "Test Glance")
        assert await pool.fetchval("SELECT version FROM handoffs WHERE id=$1::uuid", case["id"]) == 2
        await pool.execute("UPDATE outbound_outbox SET state='submitted',submitted_at=now() WHERE id=$1::uuid", data["reply_id"])
        replay = await client.post(
            PREFIX + f"/cases/{case['id']}/replies",
            json={"expected_version": 1, "draft_id": draft_id, "text": final},
            headers=headers | {"Idempotency-Key": reply_key})
        assert replay.status_code == 202 and replay.json() == approval.json()
        conflict = await client.post(
            PREFIX + f"/cases/{case['id']}/replies",
            json={"expected_version": 1, "draft_id": draft_id, "text": "Changed after approval."},
            headers=headers | {"Idempotency-Key": reply_key})
        assert conflict.status_code == 409 and conflict.json()["error"]["code"] == "idempotency_conflict"
        status = await client.get(PREFIX + f"/replies/{data['reply_id']}", headers=headers)
        assert status.status_code == 200
        assert status.json()["data"]["case_id"] == case["id"]
        assert status.json()["data"]["state"] == "submitted"
        assert status.json()["data"]["delivery"] == "unsupported"
        assert status.json()["data"]["submitted_at"].endswith("Z")
        assert await pool.fetchval("SELECT version FROM handoffs WHERE id=$1::uuid", case["id"]) == 2

        owner_b = "tg:700000099"
        salon_b = await pool.fetchval(
            "INSERT INTO salons (name,wa_phone_number_id,owner_wa_id,tg_bot_id) VALUES ('Synthetic B','PID-B',$1,'tg:111') RETURNING id::text",
            OWNER)
        headers_b, _ = await _session(pool, settings, salon_b)
        hidden = await client.get(PREFIX + f"/replies/{data['reply_id']}", headers=headers_b)
        assert hidden.status_code == 404 and hidden.json()["error"]["code"] == "reply_not_found"
        latest = (await client.get(PREFIX + f"/cases/{case['id']}", headers=headers)).json()["data"]["latest_reply"]
        assert latest["reply_id"] == data["reply_id"] and latest["state"] == "submitted"
        await client.aclose()
    finally:
        await pool.close()


@needs_db
@pytest.mark.asyncio
async def test_unresolved_reply_blocks_new_approval_and_resolution_is_safe_idempotent_and_payment_neutral():
    pool, _, salon_id, settings = await _built()
    try:
        case = await _make_case(pool, salon_id, "tg:810000007")
        client_row = await pool.fetchrow("SELECT id::text FROM clients WHERE salon_id=$1::uuid AND wa_id=$2", salon_id, "tg:810000007")
        staff_id, service_id = await pool.fetchrow("SELECT id::text FROM staff WHERE salon_id=$1::uuid LIMIT 1", salon_id), None
        service_id = await pool.fetchval("SELECT id::text FROM services WHERE salon_id=$1::uuid LIMIT 1", salon_id)
        booking_id = await pool.fetchval(
            "INSERT INTO bookings (salon_id,staff_id,service_id,client_id,starts_at,ends_at,status) "
            "VALUES ($1::uuid,$2::uuid,$3::uuid,$4::uuid,now()+interval '1 day',now()+interval '1 day 1 hour','confirmed') RETURNING id::text",
            salon_id, staff_id["id"], service_id, client_row["id"])
        payment_id = await pool.fetchval(
            "INSERT INTO payments (booking_id,salon_id,phone,amount_kes,status) VALUES ($1::uuid,$2::uuid,'254700000000',500,'pending') RETURNING id::text",
            booking_id, salon_id)
        headers, _ = await _session(pool, settings, salon_id)
        client = await _client()
        manual = await client.post(PREFIX + f"/cases/{case['id']}/drafts/manual",
                                   json={"expected_version": 1, "text": "We will look into this."},
                                   headers=headers | {"Idempotency-Key": _key()})
        draft_id = manual.json()["data"]["draft_id"]
        queued = await client.post(PREFIX + f"/cases/{case['id']}/replies",
                                   json={"expected_version": 1, "draft_id": draft_id, "text": "We will look into this."},
                                   headers=headers | {"Idempotency-Key": _key()})
        reply_id = queued.json()["data"]["reply_id"]
        next_draft = await client.post(PREFIX + f"/cases/{case['id']}/drafts/manual",
                                       json={"expected_version": 2, "text": "A second response."},
                                       headers=headers | {"Idempotency-Key": _key()})
        blocked = await client.post(PREFIX + f"/cases/{case['id']}/replies",
                                    json={"expected_version": 2, "draft_id": next_draft.json()["data"]["draft_id"],
                                          "text": "A second response."},
                                    headers=headers | {"Idempotency-Key": _key()})
        assert blocked.status_code == 409 and blocked.json()["error"]["code"] == "unresolved_reply_exists"
        resolved = await client.post(PREFIX + f"/cases/{case['id']}/resolve",
                                     json={"expected_version": 2, "reason": "handled_in_person"}, headers=headers)
        assert resolved.status_code == 200
        data = resolved.json()["data"]
        assert data["status"] == "resolved" and data["version"] == 3
        assert data["cancelled_reply_ids"] == [reply_id]
        assert data["unknown_reply_acknowledged"] is False
        assert await pool.fetchval("SELECT state FROM outbound_outbox WHERE id=$1::uuid", reply_id) == "cancelled"
        repeated = await client.post(PREFIX + f"/cases/{case['id']}/resolve",
                                     json={"expected_version": 1, "reason": "other"}, headers=headers)
        assert repeated.status_code == 200 and repeated.json() == resolved.json()
        assert await pool.fetchval("SELECT count(*) FROM handoff_events WHERE handoff_id=$1::uuid AND event_type='case_resolved'", case["id"]) == 1
        assert await pool.fetchval("SELECT status FROM payments WHERE id=$1::uuid", payment_id) == "pending"
        assert await pool.fetchval("SELECT status FROM bookings WHERE id=$1::uuid", booking_id) == "confirmed"
        await client.aclose()
    finally:
        await pool.close()


@needs_db
@pytest.mark.asyncio
async def test_sending_refusal_unknown_ack_audit_and_telegram_resolver_parity():
    pool, _, salon_id, settings = await _built()
    try:
        case = await _make_case(pool, salon_id, "tg:810000008")
        headers, paired = await _session(pool, settings, salon_id)
        client = await _client()
        manual = await client.post(PREFIX + f"/cases/{case['id']}/drafts/manual",
                                   json={"expected_version": 1, "text": "We are reviewing this."},
                                   headers=headers | {"Idempotency-Key": _key()})
        approved = await client.post(PREFIX + f"/cases/{case['id']}/replies",
                                     json={"expected_version": 1, "draft_id": manual.json()["data"]["draft_id"],
                                           "text": "We are reviewing this."},
                                     headers=headers | {"Idempotency-Key": _key()})
        reply_id = approved.json()["data"]["reply_id"]
        await pool.execute("UPDATE outbound_outbox SET state='sending' WHERE id=$1::uuid", reply_id)
        sending = await client.post(PREFIX + f"/cases/{case['id']}/resolve",
                                    json={"expected_version": 2, "reason": "other"}, headers=headers)
        assert sending.status_code == 409 and sending.json()["error"]["code"] == "reply_still_sending"
        assert await pool.fetchval("SELECT status FROM handoffs WHERE id=$1::uuid", case["id"]) == "open"
        await pool.execute("UPDATE outbound_outbox SET state='unknown',last_error_category='transport_ambiguous' WHERE id=$1::uuid", reply_id)
        app.state.ctx.agent = None
        from app import guard

        class Sender:
            def __init__(self):
                self.messages = []

            async def text(self, value):
                self.messages.append(value)

        sender = Sender()
        salon = dict(await pool.fetchrow("SELECT id::text,owner_wa_id FROM salons WHERE id=$1::uuid", salon_id))
        assert await guard.owner_command(app.state.ctx, salon, OWNER, "/resolve", sender)
        assert "uncertain" in sender.messages[-1].lower()
        assert await pool.fetchval("SELECT status FROM handoffs WHERE id=$1::uuid", case["id"]) == "open"

        refused = await client.post(PREFIX + f"/cases/{case['id']}/resolve",
                                   json={"expected_version": 2, "reason": "other"}, headers=headers)
        assert refused.status_code == 409 and refused.json()["error"]["code"] == "unknown_reply_requires_acknowledgement"
        acknowledged = await client.post(PREFIX + f"/cases/{case['id']}/resolve",
                                         json={"expected_version": 2, "reason": "customer_updated",
                                               "note": "Owner checked directly.", "acknowledge_unknown_reply": True},
                                         headers=headers)
        assert acknowledged.status_code == 200
        assert acknowledged.json()["data"]["unknown_reply_acknowledged"] is True
        audit = await pool.fetchrow(
            "SELECT reply_id::text,owner_identity,session_id::text,device_label,reason,note,acknowledged_at "
            "FROM handoff_unknown_reply_acknowledgements WHERE reply_id=$1::uuid", reply_id)
        assert tuple(audit[:6]) == (reply_id, OWNER, paired["session_id"], "Test Glance", "customer_updated", "Owner checked directly.")
        assert audit["acknowledged_at"].tzinfo is not None
        assert await pool.fetchval("SELECT state FROM outbound_outbox WHERE id=$1::uuid", reply_id) == "unknown"
        assert await _claim_next(pool) is None
        await client.aclose()
    finally:
        await pool.close()


@needs_db
@pytest.mark.asyncio
async def test_dispatch_resolution_race_cancels_before_claim_or_refuses_after_claim():
    pool, _, salon_id, settings = await _built()
    try:
        case = await _make_case(pool, salon_id, "tg:810000009")
        headers, _ = await _session(pool, settings, salon_id)
        client = await _client()
        manual = await client.post(PREFIX + f"/cases/{case['id']}/drafts/manual",
                                   json={"expected_version": 1, "text": "We'll review this."},
                                   headers=headers | {"Idempotency-Key": _key()})
        approval = await client.post(PREFIX + f"/cases/{case['id']}/replies",
                                     json={"expected_version": 1, "draft_id": manual.json()["data"]["draft_id"],
                                           "text": "We'll review this."},
                                     headers=headers | {"Idempotency-Key": _key()})
        reply_id = approval.json()["data"]["reply_id"]
        claimed = await _claim_next(pool)
        assert claimed["id"] == reply_id
        rejected = await client.post(PREFIX + f"/cases/{case['id']}/resolve",
                                     json={"expected_version": 2, "reason": "other"}, headers=headers)
        assert rejected.status_code == 409 and rejected.json()["error"]["code"] == "reply_still_sending"
        await pool.execute("UPDATE outbound_outbox SET state='queued',claim_token=NULL,lease_until=NULL WHERE id=$1::uuid", reply_id)
        await pool.execute("UPDATE handoffs SET resolution_result=NULL WHERE id=$1::uuid", case["id"])
        cancelled = await client.post(PREFIX + f"/cases/{case['id']}/resolve",
                                      json={"expected_version": 2, "reason": "other"}, headers=headers)
        assert cancelled.status_code == 200
        assert await _claim_next(pool) is None
        assert await pool.fetchval("SELECT state FROM outbound_outbox WHERE id=$1::uuid", reply_id) == "cancelled"
        await client.aclose()
    finally:
        await pool.close()
