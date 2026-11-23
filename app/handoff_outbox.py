"""Durable owner-approved replies. External sends always happen outside DB transactions."""
from __future__ import annotations

import asyncio
import hashlib
import hmac
import json
import logging
import uuid

from .channels.outbound import salon_token
from .handoffs import LegacyUnresolvedReplyViolation, _next_event, _verified_owner

log = logging.getLogger("rembo.handoff_outbox")
MAX_ATTEMPTS = 5
LEASE_SECONDS = 45


class StaleCaseVersion(RuntimeError):
    pass


class DraftExpired(StaleCaseVersion):
    pass


class DraftConsumed(StaleCaseVersion):
    pass


class DraftSuperseded(StaleCaseVersion):
    pass


class DraftNotFound(StaleCaseVersion):
    pass


class IdempotencyConflict(RuntimeError):
    pass


class RequestInProgress(RuntimeError):
    pass


class UnresolvedReplyExists(RuntimeError):
    pass


def delivery_state(channel: str, state: str) -> str:
    if channel == "telegram":
        return "unsupported"
    return "confirmed" if state == "delivered" else "unconfirmed"


def _public_error_category(value: str | None) -> str | None:
    if value is None:
        return None
    if value in {"recipient_unreachable", "provider_rejected", "provider_unavailable", "internal"}:
        return value
    if value in {"invalid_recipient", "recipient_not_found", "blocked_recipient"}:
        return "recipient_unreachable"
    if value in {"rate_limited", "network_error", "provider_timeout", "provider_unavailable",
                 "connection", "channel_unavailable"}:
        return "provider_unavailable"
    if value in {"provider_delivery_failure", "provider_rejected"}:
        return "provider_rejected"
    return "internal"


async def get_reply_status(pool, salon_id: str, reply_id: str) -> dict | None:
    async with pool.acquire() as con:
        row = await con.fetchrow(
            "SELECT id::text AS reply_id,handoff_id::text AS case_id,state,channel,last_error_category,created_at,updated_at,submitted_at,delivered_at,failed_at "
            "FROM outbound_outbox WHERE id=$1::uuid AND salon_id=$2::uuid", reply_id, salon_id)
    if row is None:
        return None
    return {
        "reply_id": row["reply_id"], "case_id": row["case_id"], "state": row["state"],
        "delivery": delivery_state(row["channel"], row["state"]),
        "error_category": _public_error_category(row["last_error_category"]),
        "created_at": row["created_at"], "updated_at": row["updated_at"],
        "submitted_at": row["submitted_at"], "delivered_at": row["delivered_at"],
        "failed_at": row["failed_at"],
    }


async def approve_reply(pool, salon_id: str, handoff_id: str, owner_identity: str,
                        expected_version: int, text: str, idempotency_key: str,
                        draft_id: str | None = None, *, request_method: str = "POST",
                        resource_path: str | None = None, session_id: str | None = None,
                        device_label: str | None = None,
                        idempotency_claim=None) -> dict:
    """Atomically store the exact owner-approved text, timeline event, and queued send."""
    if not isinstance(text, str) or not text.strip() or len(text) > 4096:
        raise ValueError("reply must contain 1 to 4096 characters")
    if not isinstance(idempotency_key, str) or not 1 <= len(idempotency_key) <= 200:
        raise ValueError("idempotency key must contain 1 to 200 characters")
    resource_path = resource_path or f"/api/v1/glance/cases/{handoff_id}/replies"
    payload_raw = json.dumps({"expected_version": int(expected_version), "draft_id": draft_id,
                              "owner_identity": owner_identity, "text": text},
                             sort_keys=True, ensure_ascii=False, separators=(",", ":")).encode("utf-8")
    request_hash = hashlib.sha256(payload_raw).digest()
    async with pool.acquire() as con:
        async with con.transaction():
            await _verified_owner(con, salon_id, owner_identity)
            # Scope lookup is salon-bound and precedes idempotency replay.
            scoped = await con.fetchval(
                "SELECT 1 FROM handoffs WHERE id=$1::uuid AND salon_id=$2::uuid", handoff_id, salon_id)
            if not scoped:
                return None
            lock_key = f"handoff-reply:{salon_id}:{request_method}:{resource_path}:{idempotency_key}"
            locked = await con.fetchval("SELECT pg_try_advisory_xact_lock(hashtextextended($1,0))", lock_key)
            if not locked:
                raise RequestInProgress("the approval request is still being processed")
            existing = await con.fetchrow(
                "SELECT o.id::text,o.handoff_id::text,o.state,o.approved_by,o.draft_id::text,e.body,"
                "o.request_payload_hash,o.response_status,o.response_body,o.approved_case_version "
                "FROM outbound_outbox o JOIN handoff_events e ON e.id=o.approved_event_id "
                "WHERE o.salon_id=$1::uuid AND o.idempotency_method=$2 AND o.idempotency_resource_path=$3 "
                "AND o.idempotency_key=$4", salon_id, request_method, resource_path, idempotency_key)
            if existing:
                if (existing["handoff_id"] != handoff_id or existing["approved_by"] != owner_identity or
                        existing["request_payload_hash"] is None or
                        not hmac.compare_digest(bytes(existing["request_payload_hash"]), request_hash)):
                    raise IdempotencyConflict("idempotency key was already used for a different reply")
                response = existing["response_body"]
                if isinstance(response, str):
                    response = json.loads(response)
                if response is None:
                    response = {"data": {"reply_id": existing["id"], "state": "queued",
                                          "case_version": int(existing["approved_case_version"])} }
                if idempotency_claim is not None:
                    from .glance_workflows import complete_request
                    await complete_request(con, idempotency_claim, int(existing["response_status"] or 202), response)
                return {"id": existing["id"], "handoff_id": existing["handoff_id"], "state": "queued",
                        "duplicate": True, "approved_case_version": int(existing["approved_case_version"]),
                        "response_status": int(existing["response_status"] or 202),
                        "response_body": response}

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
            draft = None
            if draft_id:
                draft = await con.fetchrow(
                    "SELECT based_case_version,requested_by,expires_at,consumed_at,body,source,superseded_at FROM handoff_drafts "
                    "WHERE id=$1::uuid AND handoff_id=$2::uuid AND salon_id=$3::uuid AND client_id=$4::uuid FOR UPDATE",
                    draft_id, handoff_id, salon_id, case["client_id"])
                if draft is None or draft["requested_by"] != owner_identity:
                    raise DraftNotFound("draft not found")
                if draft["superseded_at"] is not None:
                    raise DraftSuperseded("draft was superseded")
                if draft["consumed_at"] is not None:
                    raise DraftConsumed("draft was already approved")
                if draft["expires_at"] <= await con.fetchval("SELECT now()"):
                    raise DraftExpired("draft expired")
                if int(draft["based_case_version"]) != int(expected_version):
                    raise StaleCaseVersion("draft no longer matches this case")
            unresolved = await con.fetch(
                "SELECT id::text,state FROM outbound_outbox WHERE handoff_id=$1::uuid AND salon_id=$2::uuid "
                "AND state IN ('queued','sending','unknown') ORDER BY id FOR UPDATE", handoff_id, salon_id)
            if len(unresolved) > 1:
                raise LegacyUnresolvedReplyViolation("case has multiple unresolved outbound replies")
            if unresolved:
                raise UnresolvedReplyExists("case already has an unresolved outbound reply")
            channel = "telegram" if case["wa_id"].startswith("tg:") else "whatsapp"
            related = await con.fetchval(
                "SELECT id::text FROM handoff_events WHERE handoff_id=$1::uuid AND event_type='customer_message' "
                "AND visibility='customer_visible' ORDER BY sequence DESC LIMIT 1", handoff_id)
            event = await _next_event(
                con, case, event_type="approved_reply", actor_type="owner", visibility="customer_visible",
                body=text, channel=channel, related_event_id=related, bump_version=True)
            if draft is None or draft["source"] == "owner":
                reply_source = "owner"
            else:
                reply_source = "ai" if draft["body"] == text else "ai_edited"
            outbox_id = await con.fetchval(
                "INSERT INTO outbound_outbox (handoff_id,salon_id,client_id,approved_event_id,channel,recipient,phone_number_id,approved_by,idempotency_key,approved_case_version,draft_id,"
                "approved_glance_session_id,approved_device_label,reply_source,approved_draft_text,idempotency_method,idempotency_resource_path,request_payload_hash,response_status,response_body) "
                "VALUES ($1::uuid,$2::uuid,$3::uuid,$4::uuid,$5,$6,$7,$8,$9,$10,$11::uuid,$12::uuid,$13,$14,$15,$16,$17,$18,202,$19::jsonb) RETURNING id::text",
                handoff_id, salon_id, case["client_id"], event["id"], channel, case["wa_id"],
                case["wa_phone_number_id"] if channel == "whatsapp" else None, owner_identity,
                idempotency_key, event["case_version"], draft_id, session_id, device_label,
                reply_source, draft["body"] if draft else None, request_method, resource_path, request_hash,
                json.dumps({"data": {"reply_id": "", "state": "queued", "case_version": int(event["case_version"])}}))
            response_body = {"data": {"reply_id": outbox_id, "state": "queued",
                                      "case_version": int(event["case_version"])} }
            await con.execute("UPDATE outbound_outbox SET response_body=$2::jsonb WHERE id=$1::uuid",
                              outbox_id, json.dumps(response_body))
            if idempotency_claim is not None:
                from .glance_workflows import complete_request
                await complete_request(con, idempotency_claim, 202, response_body)
            if draft_id:
                await con.execute("UPDATE handoff_drafts SET consumed_at=now() WHERE id=$1::uuid AND consumed_at IS NULL", draft_id)
            return {"id": outbox_id, "handoff_id": handoff_id, "state": "queued", "duplicate": False,
                    "approved_case_version": event["case_version"], "provider_message_id": None,
                    "response_status": 202, "response_body": response_body}


async def _cancel_stale_queued(con) -> None:
    rows = await con.fetch(
        "SELECT o.id::text FROM handoffs h JOIN outbound_outbox o ON o.handoff_id=h.id "
        "AND o.salon_id=h.salon_id AND o.client_id=h.client_id "
        "WHERE o.state='queued' AND (h.status<>'open' OR h.version<>o.approved_case_version) "
        "ORDER BY h.id,o.id LIMIT 100 FOR UPDATE OF h SKIP LOCKED")
    for row in rows:
        await con.execute(
            "UPDATE outbound_outbox SET state='cancelled',last_error_category='stale_case_version',updated_at=now() "
            "WHERE id=$1::uuid AND state='queued'", row["id"])


async def mark_expired_leases_unknown(pool) -> int:
    """A worker may have reached the provider before crashing; never resend its claim."""
    changed = 0
    async with pool.acquire() as con:
        async with con.transaction():
            candidates = await con.fetch(
                "SELECT o.id::text,o.handoff_id::text,o.salon_id::text FROM outbound_outbox o "
                "JOIN handoffs h ON h.id=o.handoff_id AND h.salon_id=o.salon_id AND h.client_id=o.client_id "
                "WHERE o.state='sending' AND o.lease_until<now() ORDER BY h.id,o.id "
                "LIMIT 100 FOR UPDATE OF h SKIP LOCKED")
            for candidate in candidates:
                row = await con.fetchrow(
                    "SELECT state,lease_until FROM outbound_outbox WHERE id=$1::uuid FOR UPDATE", candidate["id"])
                if row is None or row["state"] != "sending" or row["lease_until"] is None or row["lease_until"] >= await con.fetchval("SELECT now()"):
                    continue
                await con.execute(
                    "UPDATE outbound_outbox SET state='unknown',last_error_category='worker_lease_expired', "
                    "claim_token=NULL,lease_until=NULL,updated_at=now() WHERE id=$1::uuid AND state='sending'",
                    candidate["id"])
                changed += 1
    return changed


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
            result = await dispatch_one(ctx)
            if result is None:
                await asyncio.sleep(1)
        except asyncio.CancelledError:
            raise
        except Exception:
            log.exception("handoff outbox worker iteration failed")
            await asyncio.sleep(1)
