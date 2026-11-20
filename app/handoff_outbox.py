"""Durable owner-approved replies. External sends always happen outside DB transactions."""
from __future__ import annotations

import asyncio
import logging
import uuid

from .channels.outbound import salon_token
from .handoffs import _next_event, _verified_owner

log = logging.getLogger("rembo.handoff_outbox")
MAX_ATTEMPTS = 5
LEASE_SECONDS = 45


class StaleCaseVersion(RuntimeError):
    pass


class IdempotencyConflict(RuntimeError):
    pass


async def approve_reply(pool, salon_id: str, handoff_id: str, owner_identity: str,
                        expected_version: int, text: str, idempotency_key: str) -> dict:
    """Atomically store the exact owner-approved text, timeline event, and queued send."""
    if not isinstance(text, str) or not text.strip() or len(text) > 4096:
        raise ValueError("reply must contain 1 to 4096 characters")
    if not isinstance(idempotency_key, str) or not 1 <= len(idempotency_key) <= 200:
        raise ValueError("idempotency key must contain 1 to 200 characters")
    async with pool.acquire() as con:
        async with con.transaction():
            await _verified_owner(con, salon_id, owner_identity)
            await con.execute("SELECT pg_advisory_xact_lock(hashtextextended($1,0))",
                              f"handoff-reply:{salon_id}:{idempotency_key}")
            existing = await con.fetchrow(
                "SELECT o.id::text,o.handoff_id::text,o.state,o.approved_by,e.body "
                "FROM outbound_outbox o JOIN handoff_events e ON e.id=o.approved_event_id "
                "WHERE o.salon_id=$1::uuid AND o.idempotency_key=$2", salon_id, idempotency_key)
            if existing:
                if (existing["handoff_id"] != handoff_id or existing["approved_by"] != owner_identity or existing["body"] != text):
                    raise IdempotencyConflict("idempotency key was already used for a different reply")
                return {"id": existing["id"], "handoff_id": existing["handoff_id"], "state": existing["state"], "duplicate": True}

            row = await con.fetchrow(
                "SELECT h.id::text,h.salon_id::text,h.client_id::text,h.status,h.version, "
                "c.wa_id,s.wa_phone_number_id FROM handoffs h "
                "JOIN clients c ON c.id=h.client_id AND c.salon_id=h.salon_id "
                "JOIN salons s ON s.id=h.salon_id "
                "WHERE h.id=$1::uuid AND h.salon_id=$2::uuid FOR UPDATE OF h", handoff_id, salon_id)
            if row is None:
                return None
            case = dict(row)
            if case["status"] != "open":
                raise StaleCaseVersion("handoff is no longer open")
            if int(case["version"]) != int(expected_version):
                raise StaleCaseVersion("case changed; refresh its context before approving")
            channel = "telegram" if case["wa_id"].startswith("tg:") else "whatsapp"
            related = await con.fetchval(
                "SELECT id::text FROM handoff_events WHERE handoff_id=$1::uuid AND event_type='customer_message' "
                "AND visibility='customer_visible' ORDER BY sequence DESC LIMIT 1", handoff_id)
            event = await _next_event(
                con, case, event_type="approved_reply", actor_type="owner", visibility="customer_visible",
                body=text, channel=channel, related_event_id=related, bump_version=True)
            await con.execute(
                "UPDATE outbound_outbox SET state='cancelled',last_error_category='stale_case_version',updated_at=now() "
                "WHERE handoff_id=$1::uuid AND state='queued' AND approved_case_version<>$2",
                handoff_id, event["case_version"])
            outbox_id = await con.fetchval(
                "INSERT INTO outbound_outbox (handoff_id,salon_id,client_id,approved_event_id,channel,recipient,phone_number_id,approved_by,idempotency_key,approved_case_version) "
                "VALUES ($1::uuid,$2::uuid,$3::uuid,$4::uuid,$5,$6,$7,$8,$9,$10) RETURNING id::text",
                handoff_id, salon_id, case["client_id"], event["id"], channel, case["wa_id"],
                case["wa_phone_number_id"] if channel == "whatsapp" else None, owner_identity,
                idempotency_key, event["case_version"])
            return {"id": outbox_id, "handoff_id": handoff_id, "state": "queued", "duplicate": False,
                    "approved_case_version": event["case_version"], "provider_message_id": None}


async def _cancel_stale_queued(con) -> None:
    await con.execute(
        "UPDATE outbound_outbox o SET state='cancelled',last_error_category='stale_case_version',updated_at=now() "
        "WHERE o.state='queued' AND NOT EXISTS (SELECT 1 FROM handoffs h WHERE h.id=o.handoff_id "
        "AND h.salon_id=o.salon_id AND h.client_id=o.client_id AND h.status='open' AND h.version=o.approved_case_version)")


async def mark_expired_leases_unknown(pool) -> int:
    """A worker may have reached the provider before crashing; never resend its claim."""
    result = await pool.execute(
        "UPDATE outbound_outbox SET state='unknown',last_error_category='worker_lease_expired', "
        "claim_token=NULL,lease_until=NULL,updated_at=now() WHERE state='sending' AND lease_until < now()")
    return int(result.split()[-1])


async def mark_unconfirmed_submissions_unknown(pool) -> int:
    """No provider receipt arrived in 24 hours; preserve uncertainty without retrying."""
    result = await pool.execute(
        "UPDATE outbound_outbox SET state='unknown',last_error_category='delivery_receipt_timeout',updated_at=now() "
        "WHERE state='submitted' AND submitted_at < now()-interval '24 hours'")
    return int(result.split()[-1])


async def _claim_next(pool) -> dict | None:
    async with pool.acquire() as con:
        async with con.transaction():
            await _cancel_stale_queued(con)
            candidate = await con.fetchrow(
                "SELECT h.id::text AS handoff_id,o.id::text AS outbox_id "
                "FROM outbound_outbox o JOIN handoffs h ON h.id=o.handoff_id AND h.salon_id=o.salon_id AND h.client_id=o.client_id "
                "WHERE o.state='queued' AND o.available_at<=now() AND h.status='open' AND h.version=o.approved_case_version "
                "ORDER BY o.available_at,o.created_at,o.id LIMIT 1 FOR UPDATE OF h SKIP LOCKED")
            if candidate is None:
                return None
            row = await con.fetchrow(
                "SELECT o.id::text,o.handoff_id::text,o.salon_id::text,o.client_id::text,o.approved_event_id::text, "
                "o.channel,o.recipient,o.phone_number_id,o.approved_case_version,o.attempts,e.body "
                "FROM outbound_outbox o JOIN handoffs h ON h.id=o.handoff_id AND h.salon_id=o.salon_id AND h.client_id=o.client_id "
                "JOIN handoff_events e ON e.id=o.approved_event_id AND e.handoff_id=o.handoff_id "
                "WHERE o.id=$1::uuid AND o.state='queued' AND o.available_at<=now() "
                "FOR UPDATE OF o", candidate["outbox_id"])
            if row is None:
                return None
            token = uuid.uuid4()
            updated = await con.fetchrow(
                "UPDATE outbound_outbox SET state='sending',attempts=attempts+1,claim_token=$2, "
                "lease_until=now()+($3 * interval '1 second'),updated_at=now() WHERE id=$1::uuid AND state='queued' "
                "RETURNING attempts,lease_until", row["id"], token, LEASE_SECONDS)
            return dict(row) | {"claim_token": token, "attempts": updated["attempts"], "lease_until": updated["lease_until"]}


async def _before_send(pool, claim: dict) -> dict | None:
    """Fence the claim and recheck case version immediately before network I/O."""
    async with pool.acquire() as con:
        async with con.transaction():
            row = await con.fetchrow(
                "SELECT o.id::text,o.state,o.claim_token,o.handoff_id::text,o.salon_id::text,o.client_id::text, "
                "o.approved_event_id::text,o.channel,o.recipient,o.phone_number_id,o.approved_case_version,o.attempts, "
                "e.body,h.status AS case_status,h.version AS current_version, "
                "s.wa_phone_number_id AS salon_phone_number_id,s.wa_access_token "
                "FROM outbound_outbox o JOIN handoffs h ON h.id=o.handoff_id AND h.salon_id=o.salon_id AND h.client_id=o.client_id "
                "JOIN handoff_events e ON e.id=o.approved_event_id AND e.handoff_id=o.handoff_id "
                "JOIN salons s ON s.id=o.salon_id WHERE o.id=$1::uuid FOR UPDATE OF h", claim["id"])
            if row is None:
                return None
            outbox = await con.fetchrow("SELECT state,claim_token FROM outbound_outbox WHERE id=$1::uuid FOR UPDATE", claim["id"])
            if outbox is None or outbox["state"] != "sending" or outbox["claim_token"] != claim["claim_token"]:
                return None
            if row["case_status"] != "open" or row["current_version"] != row["approved_case_version"]:
                await con.execute(
                    "UPDATE outbound_outbox SET state='cancelled',last_error_category='stale_case_version', "
                    "claim_token=NULL,lease_until=NULL,updated_at=now() WHERE id=$1::uuid AND claim_token=$2",
                    claim["id"], claim["claim_token"])
                return None
            return dict(row)


async def _complete(pool, claim: dict, result) -> str | None:
    async with pool.acquire() as con:
        async with con.transaction():
            identity = await con.fetchrow(
                "SELECT id::text,handoff_id::text,salon_id::text,client_id::text FROM outbound_outbox WHERE id=$1::uuid", claim["id"])
            if identity is None:
                return None
            case_locked = await con.fetchrow(
                "SELECT id FROM handoffs WHERE id=$1::uuid AND salon_id=$2::uuid AND client_id=$3::uuid FOR UPDATE",
                identity["handoff_id"], identity["salon_id"], identity["client_id"])
            if case_locked is None:
                return None
            row = await con.fetchrow(
                "SELECT id::text,handoff_id::text,salon_id::text,client_id::text,claim_token,attempts,state "
                "FROM outbound_outbox WHERE id=$1::uuid FOR UPDATE", claim["id"])
            if row is None or row["state"] != "sending" or row["claim_token"] != claim["claim_token"]:
                return None
            if result.accepted:
                state, delay = "submitted", None
            elif result.ambiguous:
                state, delay = "unknown", None
            elif result.retryable and row["attempts"] < MAX_ATTEMPTS:
                state, delay = "queued", min(60, 2 ** max(0, row["attempts"] - 1) * 2)
            else:
                state, delay = "failed", None
            await con.execute(
                "UPDATE outbound_outbox SET state=$2,provider_message_id=$3,available_at=CASE WHEN $4::int IS NULL THEN available_at ELSE now()+($4 * interval '1 second') END, "
                "last_error_category=$5,last_error_code=$6,claim_token=NULL,lease_until=NULL, "
                "submitted_at=CASE WHEN $2='submitted' THEN now() ELSE submitted_at END, "
                "failed_at=CASE WHEN $2='failed' THEN now() ELSE failed_at END,updated_at=now() "
                "WHERE id=$1::uuid", claim["id"], state, result.provider_message_id, delay,
                result.error_category, result.error_code)
            case = {"id": row["handoff_id"], "salon_id": row["salon_id"], "client_id": row["client_id"]}
            await _next_event(con, case, event_type="delivery_status", actor_type="system", visibility="owner_internal",
                              body=f"Owner-approved reply status: {state}", channel=claim["channel"])
            return state


async def dispatch_one(ctx) -> str | None:
    """Claim one message, send outside a transaction, then store the provider outcome."""
    claim = await _claim_next(ctx.pool)
    if claim is None:
        return None
    ready = await _before_send(ctx.pool, claim)
    if ready is None:
        return "cancelled"
    salon = {"id": ready["salon_id"], "wa_phone_number_id": ready["salon_phone_number_id"],
             "wa_access_token": ready["wa_access_token"]}
    token = salon_token(ctx, salon)
    try:
        result = await ctx.wa.send_text_result(ready["phone_number_id"], token, ready["recipient"], ready["body"])
    except Exception as exc:
        # An unexpected exception after starting the provider call is ambiguous.
        log.exception("outbox send raised %s", exc.__class__.__name__)
        from .channels.results import SendResult
        result = SendResult(False, error_category="sender_exception", ambiguous=True)
    return await _complete(ctx.pool, claim, result)


async def record_whatsapp_receipt(pool, phone_number_id: str, receipt) -> bool:
    """Apply only provider delivery/failure receipts, scoped to the sending number."""
    if receipt.status not in ("delivered", "read", "failed"):
        return False
    state = "failed" if receipt.status == "failed" else "delivered"
    async with pool.acquire() as con:
        async with con.transaction():
            identity = await con.fetchrow(
                "SELECT o.id::text,o.handoff_id::text,o.salon_id::text,o.client_id::text "
                "FROM outbound_outbox o JOIN salons s ON s.id=o.salon_id "
                "JOIN handoffs h ON h.id=o.handoff_id AND h.salon_id=o.salon_id "
                "WHERE o.channel='whatsapp' AND o.provider_message_id=$1 AND s.wa_phone_number_id=$2 "
                "AND o.state IN ('submitted','unknown')", receipt.provider_message_id, phone_number_id)
            if identity is None:
                return False
            await con.fetchrow(
                "SELECT id FROM handoffs WHERE id=$1::uuid AND salon_id=$2::uuid AND client_id=$3::uuid FOR UPDATE",
                identity["handoff_id"], identity["salon_id"], identity["client_id"])
            row = await con.fetchrow(
                "SELECT id::text,handoff_id::text,salon_id::text,client_id::text,channel,state "
                "FROM outbound_outbox WHERE id=$1::uuid FOR UPDATE", identity["id"])
            if row is None or row["state"] not in ("submitted", "unknown"):
                return False
            await con.execute(
                "UPDATE outbound_outbox SET state=$2,delivered_at=CASE WHEN $2='delivered' THEN now() ELSE delivered_at END, "
                "failed_at=CASE WHEN $2='failed' THEN now() ELSE failed_at END,last_error_category=$3,last_error_code=$4,updated_at=now() "
                "WHERE id=$1::uuid AND state IN ('submitted','unknown')", row["id"], state,
                "provider_delivery_failure" if state == "failed" else None, receipt.error_code)
            case = {"id": row["handoff_id"], "salon_id": row["salon_id"], "client_id": row["client_id"]}
            await _next_event(con, case, event_type="delivery_status", actor_type="system", visibility="owner_internal",
                              body=f"Provider delivery receipt: {state}", channel="whatsapp")
            return True


async def worker(ctx) -> None:
    while True:
        try:
            await mark_expired_leases_unknown(ctx.pool)
            await mark_unconfirmed_submissions_unknown(ctx.pool)
            result = await dispatch_one(ctx)
            if result is None:
                await asyncio.sleep(1)
        except asyncio.CancelledError:
            raise
        except Exception:
            log.exception("handoff outbox worker iteration failed")
            await asyncio.sleep(1)
