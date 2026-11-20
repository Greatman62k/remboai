import os
from types import SimpleNamespace

import pytest

from app.channels.results import SendResult
from app.channels.whatsapp import Receipt
from app.handoffs import CaseHasSendingReply, create_handoff, record_customer_message, resolve_handoff
from app.handoff_outbox import (
    IdempotencyConflict,
    StaleCaseVersion,
    _claim_next,
    approve_reply,
    dispatch_one,
    mark_expired_leases_unknown,
    record_whatsapp_receipt,
)
from tests.harness import build

DSN = os.environ.get("TEST_DATABASE_URL")
needs_db = pytest.mark.skipif(not DSN, reason="needs TEST_DATABASE_URL")


async def _case(pool, salon_id, owner="254700000010", customer="254700000011"):
    await pool.execute("UPDATE salons SET owner_wa_id=$2 WHERE id=$1::uuid", salon_id, owner)
    client = await pool.fetchrow(
        "INSERT INTO clients (salon_id,wa_id) VALUES ($1::uuid,$2) RETURNING id::text,wa_id", salon_id, customer)
    case = await create_handoff(pool, salon_id, client["id"], client["wa_id"], "complaint", "I need help",
                                source_message_id=f"wamid-{customer}")
    return case, client


@needs_db
@pytest.mark.asyncio
async def test_approval_persists_exact_text_and_duplicate_idempotency_before_dispatch():
    pool, _, salon_id = await build(DSN)
    case, _ = await _case(pool, salon_id)
    approved = "I am sorry about that. The owner will review your concern."
    first = await approve_reply(pool, salon_id, case["id"], "254700000010", 1, approved, "device-a:approval-1")
    duplicate = await approve_reply(pool, salon_id, case["id"], "254700000010", 1, approved, "device-a:approval-1")
    assert first["state"] == "queued" and first["duplicate"] is False
    assert duplicate["id"] == first["id"] and duplicate["duplicate"] is True
    row = await pool.fetchrow(
        "SELECT o.state,o.approved_case_version,e.body,e.actor_type,e.visibility "
        "FROM outbound_outbox o JOIN handoff_events e ON e.id=o.approved_event_id WHERE o.id=$1::uuid", first["id"])
    assert tuple(row) == ("queued", 2, approved, "owner", "customer_visible")
    with pytest.raises(Exception, match="handoff events are immutable"):
        await pool.execute("UPDATE handoff_events SET body='edited' WHERE id=(SELECT approved_event_id FROM outbound_outbox WHERE id=$1::uuid)", first["id"])
    with pytest.raises(IdempotencyConflict):
        await approve_reply(pool, salon_id, case["id"], "254700000010", 1, "Changed text", "device-a:approval-1")
    with pytest.raises(StaleCaseVersion):
        await approve_reply(pool, salon_id, case["id"], "254700000010", 1, "Second response", "device-b:approval-2")
    assert await pool.fetchval("SELECT count(*) FROM outbound_outbox") == 1
    await pool.close()


@needs_db
@pytest.mark.asyncio
async def test_stale_customer_message_cancels_queued_reply_before_dispatch():
    pool, _, salon_id = await build(DSN)
    case, client = await _case(pool, salon_id)
    approved = await approve_reply(pool, salon_id, case["id"], "254700000010", 1, "We will look into this.", "approval-1")
    await record_customer_message(pool, salon_id, client["wa_id"], "One more detail", "wamid-followup")
    assert await pool.fetchval("SELECT state FROM outbound_outbox WHERE id=$1::uuid", approved["id"]) == "cancelled"
    assert await _claim_next(pool) is None
    await pool.close()


@needs_db
@pytest.mark.asyncio
async def test_dispatch_records_submission_then_delivery_receipt_separately():
    pool, _, salon_id = await build(DSN)
    case, _ = await _case(pool, salon_id)
    approved = await approve_reply(pool, salon_id, case["id"], "254700000010", 1, "Thank you for telling us.", "approval-1")

    class Sender:
        def __init__(self):
            self.calls = []

        async def send_text_result(self, pid, token, recipient, body):
            self.calls.append((pid, recipient, body))
            return SendResult(True, provider_message_id="wamid-submitted-1")

    sender = Sender()
    ctx = SimpleNamespace(pool=pool, settings=SimpleNamespace(token_encryption_key="", wa_access_token="test-token"), wa=sender)
    assert await dispatch_one(ctx) == "submitted"
    assert sender.calls == [("PID1", "254700000011", "Thank you for telling us.")]
    row = await pool.fetchrow("SELECT state,provider_message_id,submitted_at,delivered_at FROM outbound_outbox WHERE id=$1::uuid", approved["id"])
    assert row["state"] == "submitted" and row["provider_message_id"] == "wamid-submitted-1"
    assert row["submitted_at"] is not None and row["delivered_at"] is None
    wrong_number = Receipt("wamid-submitted-1", "OTHER", "delivered", None)
    assert await record_whatsapp_receipt(pool, "OTHER", wrong_number) is False
    receipt = Receipt("wamid-submitted-1", "PID1", "delivered", None)
    assert await record_whatsapp_receipt(pool, "PID1", receipt) is True
    assert await pool.fetchval("SELECT state FROM outbound_outbox WHERE id=$1::uuid", approved["id"]) == "delivered"
    assert await record_whatsapp_receipt(pool, "PID1", receipt) is False

    late_case, _ = await _case(pool, salon_id, customer="254700000012")
    late = await approve_reply(pool, salon_id, late_case["id"], "254700000010", 1, "A late status.", "approval-late")
    await pool.execute("UPDATE outbound_outbox SET state='unknown',provider_message_id='wamid-late' WHERE id=$1::uuid", late["id"])
    late_receipt = Receipt("wamid-late", "PID1", "delivered", None)
    assert await record_whatsapp_receipt(pool, "PID1", late_receipt) is True
    assert await pool.fetchval("SELECT state FROM outbound_outbox WHERE id=$1::uuid", late["id"]) == "delivered"
    await pool.close()


@needs_db
@pytest.mark.asyncio
async def test_ambiguous_acceptance_and_expired_worker_lease_are_never_retried():
    pool, _, salon_id = await build(DSN)
    case, _ = await _case(pool, salon_id)
    approved = await approve_reply(pool, salon_id, case["id"], "254700000010", 1, "I will check this for you.", "approval-1")

    class AmbiguousSender:
        async def send_text_result(self, *args):
            return SendResult(False, error_category="read_timeout", ambiguous=True)

    ctx = SimpleNamespace(pool=pool, settings=SimpleNamespace(token_encryption_key="", wa_access_token="test-token"), wa=AmbiguousSender())
    assert await dispatch_one(ctx) == "unknown"
    assert await _claim_next(pool) is None
    assert await pool.fetchval("SELECT state FROM outbound_outbox WHERE id=$1::uuid", approved["id"]) == "unknown"

    second_case, _ = await _case(pool, salon_id, customer="254700000012")
    second = await approve_reply(pool, salon_id, second_case["id"], "254700000010", 1, "A second case reply.", "approval-2")
    claim = await _claim_next(pool)
    assert claim["id"] == second["id"]
    await pool.execute("UPDATE outbound_outbox SET lease_until=now()-interval '1 second' WHERE id=$1::uuid", second["id"])
    assert await mark_expired_leases_unknown(pool) == 1
    assert await _claim_next(pool) is None
    assert await pool.fetchval("SELECT state FROM outbound_outbox WHERE id=$1::uuid", second["id"]) == "unknown"
    await pool.close()


@needs_db
@pytest.mark.asyncio
async def test_provider_rejection_does_not_resolve_case_and_resolution_waits_for_inflight_send():
    pool, _, salon_id = await build(DSN)
    case, _ = await _case(pool, salon_id)
    approved = await approve_reply(pool, salon_id, case["id"], "254700000010", 1, "I understand.", "approval-1")
    await _claim_next(pool)
    with pytest.raises(CaseHasSendingReply):
        await resolve_handoff(pool, salon_id, case["id"], "254700000010", "owner resolved")
    await pool.execute(
        "UPDATE outbound_outbox SET state='queued',claim_token=NULL,lease_until=NULL,attempts=0 WHERE id=$1::uuid", approved["id"])

    class RejectedSender:
        async def send_text_result(self, *args):
            return SendResult(False, error_category="provider_rejected")

    ctx = SimpleNamespace(pool=pool, settings=SimpleNamespace(token_encryption_key="", wa_access_token="test-token"), wa=RejectedSender())
    assert await dispatch_one(ctx) == "failed"
    assert await pool.fetchval("SELECT status FROM handoffs WHERE id=$1::uuid", case["id"]) == "open"
    assert await pool.fetchval("SELECT state FROM outbound_outbox WHERE id=$1::uuid", approved["id"]) == "failed"
    await pool.close()


@needs_db
@pytest.mark.asyncio
async def test_known_retryable_rejection_requeues_with_backoff_but_can_then_submit():
    pool, _, salon_id = await build(DSN)
    case, _ = await _case(pool, salon_id)
    approved = await approve_reply(pool, salon_id, case["id"], "254700000010", 1, "We will review this.", "approval-1")

    class RetryThenAccept:
        def __init__(self):
            self.calls = 0

        async def send_text_result(self, *args):
            self.calls += 1
            if self.calls == 1:
                return SendResult(False, error_category="rate_limited", retryable=True)
            return SendResult(True, provider_message_id="wamid-after-retry")

    sender = RetryThenAccept()
    ctx = SimpleNamespace(pool=pool, settings=SimpleNamespace(token_encryption_key="", wa_access_token="test-token"), wa=sender)
    assert await dispatch_one(ctx) == "queued"
    assert await pool.fetchval("SELECT attempts FROM outbound_outbox WHERE id=$1::uuid", approved["id"]) == 1
    await pool.execute("UPDATE outbound_outbox SET available_at=now()-interval '1 second' WHERE id=$1::uuid", approved["id"])
    assert await dispatch_one(ctx) == "submitted"
    assert sender.calls == 2
    assert await pool.fetchval("SELECT attempts FROM outbound_outbox WHERE id=$1::uuid", approved["id"]) == 2
    await pool.close()


@needs_db
@pytest.mark.asyncio
async def test_salon_deletion_cannot_purge_unknown_outbound_audit():
    from app.lifecycle import PendingOutboundError, delete_salon

    pool, _, salon_id = await build(DSN)
    case, _ = await _case(pool, salon_id)
    approved = await approve_reply(pool, salon_id, case["id"], "254700000010", 1, "I understand.", "approval-1")
    await resolve_handoff(pool, salon_id, case["id"], "254700000010", "owner completed review")
    await pool.execute("UPDATE outbound_outbox SET state='unknown' WHERE id=$1::uuid", approved["id"])
    with pytest.raises(PendingOutboundError):
        await delete_salon(pool, salon_id)
    assert await pool.fetchval("SELECT count(*) FROM outbound_outbox WHERE id=$1::uuid", approved["id"]) == 1
    await pool.close()
