"""Durable human handoffs, case history, and explicit resolution."""
from __future__ import annotations

import json
import uuid
from datetime import timezone


class CaseHasSendingReply(RuntimeError):
    """A provider send is in progress and cannot be atomically cancelled."""


class CaseVersionConflict(RuntimeError):
    pass


class UnknownReplyNeedsAcknowledgement(RuntimeError):
    pass


class NoUnknownReply(RuntimeError):
    pass


class LegacyUnresolvedReplyViolation(RuntimeError):
    pass



def channel_for(identity: str | None) -> str:
    return "telegram" if (identity or "").startswith("tg:") else "whatsapp"


async def _verified_owner(con, salon_id: str, owner_identity: str) -> None:
    row = await con.fetchrow("SELECT owner_wa_id FROM salons WHERE id=$1::uuid FOR SHARE", salon_id)
    if row is None or not row["owner_wa_id"] or row["owner_wa_id"].strip().lstrip("+") != (owner_identity or "").strip().lstrip("+"):
        raise PermissionError("actor is not the salon owner")


async def list_open_handoffs(pool, salon_id: str, owner_identity: str, limit: int = 20) -> list[dict]:
    """Owner-scoped inbox; customer identifiers are intentionally omitted."""
    limit = max(1, min(int(limit), 50))
    async with pool.acquire() as con:
        async with con.transaction():
            await _verified_owner(con, salon_id, owner_identity)
            rows = await con.fetch(
                "SELECT h.id::text,h.category,h.status,h.version,h.created_at,c.name AS customer_name, "
                "CASE WHEN c.wa_id LIKE 'tg:%' THEN 'telegram' ELSE 'whatsapp' END AS channel, "
                "COALESCE(preview.body,'') AS preview, count(*) OVER()::bigint AS open_count "
                "FROM handoffs h JOIN clients c ON c.id=h.client_id AND c.salon_id=h.salon_id "
                "LEFT JOIN LATERAL (SELECT e.body FROM handoff_events e "
                "WHERE e.handoff_id=h.id AND e.salon_id=h.salon_id AND e.visibility='customer_visible' "
                "AND e.event_type IN ('customer_message','assistant_reply','approved_reply') "
                "ORDER BY e.sequence DESC LIMIT 1) preview ON true "
                "WHERE h.salon_id=$1::uuid AND h.status='open' ORDER BY h.created_at,h.id LIMIT $2",
                salon_id, limit)
            return [dict(row) for row in rows]


async def get_handoff_context(pool, salon_id: str, handoff_id: str,
                              owner_identity: str, limit: int = 10) -> dict | None:
    """Fetch a tenant-scoped case and only its latest customer-visible messages."""
    limit = max(1, min(int(limit), 10))
    async with pool.acquire() as con:
        async with con.transaction():
            await _verified_owner(con, salon_id, owner_identity)
            case = await con.fetchrow(
                "SELECT h.id::text,h.category,h.status,h.version,h.created_at,h.resolved_at, "
                "c.name AS customer_name,CASE WHEN c.wa_id LIKE 'tg:%' THEN 'telegram' ELSE 'whatsapp' END AS channel "
                "FROM handoffs h JOIN clients c ON c.id=h.client_id AND c.salon_id=h.salon_id "
                "WHERE h.id=$1::uuid AND h.salon_id=$2::uuid", handoff_id, salon_id)
            if case is None:
                return None
            messages = await con.fetch(
                "SELECT sequence,event_type,actor_type,body,channel,created_at FROM handoff_events "
                "WHERE handoff_id=$1::uuid AND salon_id=$2::uuid AND visibility='customer_visible' "
                "ORDER BY sequence DESC LIMIT $3", handoff_id, salon_id, limit)
            result = dict(case)
            result["customer"] = {"name": case["customer_name"] or "Customer", "channel": case["channel"]}
            result["messages"] = [dict(row) for row in reversed(messages)]
            return result


async def get_glance_handoff_context(pool, salon_id: str, handoff_id: str,
                                     owner_identity: str, limit: int = 11) -> dict | None:
    """Read a Glance case with a visibility allowlist, eleven-event look-ahead and latest reply state.

    The ordinary context service remains unchanged for its existing callers. This API-specific
    read intentionally selects only customer messages and customer-visible replies, never
    owner-internal events or diagnostic traces.
    """
    limit = max(1, min(int(limit), 11))
    async with pool.acquire() as con:
        async with con.transaction():
            await _verified_owner(con, salon_id, owner_identity)
            case = await con.fetchrow(
                "SELECT h.id::text,h.category,h.status,h.version,h.created_at,h.resolved_at, "
                "c.name AS customer_name,CASE WHEN c.wa_id LIKE 'tg:%' THEN 'telegram' ELSE 'whatsapp' END AS channel "
                "FROM handoffs h JOIN clients c ON c.id=h.client_id AND c.salon_id=h.salon_id "
                "WHERE h.id=$1::uuid AND h.salon_id=$2::uuid", handoff_id, salon_id)
            if case is None:
                return None
            messages = await con.fetch(
                "SELECT sequence,event_type,actor_type,body,channel,created_at FROM handoff_events "
                "WHERE handoff_id=$1::uuid AND salon_id=$2::uuid AND visibility='customer_visible' "
                "AND event_type IN ('customer_message','assistant_reply','approved_reply') "
                "AND actor_type IN ('customer','assistant','owner') "
                "ORDER BY sequence DESC LIMIT $3", handoff_id, salon_id, limit)
            latest_reply = await con.fetchrow(
                "SELECT id::text AS reply_id,state,channel,last_error_category,updated_at,submitted_at,delivered_at,failed_at FROM outbound_outbox "
                "WHERE handoff_id=$1::uuid AND salon_id=$2::uuid ORDER BY created_at DESC,id DESC LIMIT 1",
                handoff_id, salon_id)
            result = dict(case)
            result["customer"] = {"name": case["customer_name"] or "Customer", "channel": case["channel"]}
            result["messages"] = [dict(row) for row in reversed(messages)]
            result["latest_reply"] = dict(latest_reply) if latest_reply else None
            return result


async def _next_event(con, case: dict, *, event_type: str, actor_type: str, visibility: str,
                      body: str | None = None, channel: str | None = None,
                      provider_message_id: str | None = None, related_event_id: str | None = None,
                      bump_version: bool = False) -> dict:
    row = await con.fetchrow(
        "UPDATE handoffs SET event_seq=event_seq+1, version=version+$2 "
        "WHERE id=$1::uuid RETURNING event_seq, version", case["id"], int(bump_version))
    return dict(await con.fetchrow(
        "INSERT INTO handoff_events (handoff_id,salon_id,client_id,sequence,event_type,actor_type,visibility,body,channel,provider_message_id,related_event_id) "
        "VALUES ($1::uuid,$2::uuid,$3::uuid,$4,$5,$6,$7,$8,$9,$10,$11::uuid) "
        "RETURNING id::text, sequence, event_type, actor_type, visibility, body, channel, provider_message_id, created_at, $12::bigint AS case_version",
        case["id"], case["salon_id"], case["client_id"], row["event_seq"], event_type, actor_type,
        visibility, (body or "")[:4096] if body is not None else None, channel, provider_message_id,
        related_event_id, row["version"]))


async def create_handoff(pool, salon_id: str, client_id: str, customer_identity: str,
                         category: str, text: str, *, source_message_id: str | None,
                         owner_notification: str | None = None) -> dict:
    """Persist case, first customer event and paused conversation atomically."""
    channel = channel_for(customer_identity)
    async with pool.acquire() as con:
        async with con.transaction():
            salon = await con.fetchrow("SELECT id FROM salons WHERE id=$1::uuid FOR KEY SHARE", salon_id)
            client = await con.fetchrow(
                "SELECT id::text, salon_id::text, wa_id FROM clients WHERE id=$1::uuid AND salon_id=$2::uuid FOR UPDATE",
                client_id, salon_id)
            if salon is None or client is None or client["wa_id"] != customer_identity:
                raise ValueError("handoff salon and customer do not match")

            case_row = await con.fetchrow(
                "SELECT id::text, salon_id::text, client_id::text, status, category FROM handoffs "
                "WHERE salon_id=$1::uuid AND client_id=$2::uuid AND status='open' FOR UPDATE",
                salon_id, client_id)
            created = case_row is None
            if created:
                case_row = await con.fetchrow(
                    "INSERT INTO handoffs (salon_id,client_id,category,message) VALUES ($1::uuid,$2::uuid,$3,$4) "
                    "RETURNING id::text,salon_id::text,client_id::text,status,category", salon_id, client_id, category, text[:1000])
            case = dict(case_row)
            effective_category = category if created or category == "emergency" else case.get("category", category)

            prior = None
            if source_message_id:
                prior = await con.fetchrow(
                    "SELECT id::text FROM handoff_events WHERE salon_id=$1::uuid AND channel=$2 AND provider_message_id=$3 "
                    "AND event_type='customer_message'", salon_id, channel, source_message_id)
            if prior is None:
                if not created and category == "emergency":
                    await con.execute("UPDATE handoffs SET category='emergency',message=$2 WHERE id=$1::uuid", case["id"], text[:1000])
                inbound = await _next_event(
                    con, case, event_type="customer_message", actor_type="customer", visibility="customer_visible",
                    body=text, channel=channel, provider_message_id=source_message_id, bump_version=not created)
                if not created:
                    await con.execute(
                        "UPDATE outbound_outbox SET state='cancelled',last_error_category='stale_case_version',updated_at=now() "
                        "WHERE handoff_id=$1::uuid AND state='queued' AND approved_case_version<>$2",
                        case["id"], inbound["case_version"])
                if created:
                    await _next_event(con, case, event_type="case_opened", actor_type="system", visibility="owner_internal",
                                      body=category, channel=channel)
                if owner_notification:
                    await _next_event(
                        con, case, event_type="owner_notification", actor_type="system", visibility="owner_internal",
                        body=owner_notification, channel=channel, related_event_id=inbound["id"])

            state = json.dumps({"state": "handoff", "category": effective_category})
            await con.execute(
                "INSERT INTO conversations (salon_id,wa_id,state) VALUES ($1::uuid,$2,$3::jsonb) "
                "ON CONFLICT (salon_id,wa_id) DO UPDATE SET state=EXCLUDED.state,updated_at=now()",
                salon_id, customer_identity, state)
            current = await con.fetchrow("SELECT version,event_seq FROM handoffs WHERE id=$1::uuid", case["id"])
            case.update(dict(current))
    return {"id": case["id"], "salon_id": salon_id, "client_id": client_id,
            "created": created, "version": case["version"], "source_message_id": source_message_id}


async def record_customer_message(pool, salon_id: str, customer_identity: str,
                                  text: str, provider_message_id: str | None) -> dict | None:
    """Append a customer event and invalidate case-bound drafts by bumping version."""
    channel = channel_for(customer_identity)
    async with pool.acquire() as con:
        async with con.transaction():
            case_row = await con.fetchrow(
                "SELECT h.id::text,h.salon_id::text,h.client_id::text FROM handoffs h "
                "JOIN clients c ON c.id=h.client_id AND c.salon_id=h.salon_id "
                "WHERE h.salon_id=$1::uuid AND c.wa_id=$2 AND h.status='open' FOR UPDATE OF h",
                salon_id, customer_identity)
            if case_row is None:
                return None
            case = dict(case_row)
            if provider_message_id:
                prior = await con.fetchrow(
                    "SELECT id::text,sequence FROM handoff_events WHERE salon_id=$1::uuid AND channel=$2 "
                    "AND provider_message_id=$3 AND event_type='customer_message'",
                    salon_id, channel, provider_message_id)
                if prior:
                    version = await con.fetchval("SELECT version FROM handoffs WHERE id=$1::uuid", case["id"])
                    return {"handoff_id": case["id"], "event_id": prior["id"], "version": version, "duplicate": True}
            event = await _next_event(
                con, case, event_type="customer_message", actor_type="customer", visibility="customer_visible",
                body=text, channel=channel, provider_message_id=provider_message_id, bump_version=True)
            await con.execute(
                "UPDATE outbound_outbox SET state='cancelled',last_error_category='stale_case_version',updated_at=now() "
                "WHERE handoff_id=$1::uuid AND state='queued' AND approved_case_version<>$2",
                case["id"], event["case_version"])
            return {"handoff_id": case["id"], "event_id": event["id"], "version": event["case_version"], "duplicate": False}


async def record_customer_reply(pool, salon_id: str, customer_identity: str, body: str,
                                provider_message_id: str | None = None) -> dict | None:
    """Store only an assistant message sent to the customer during an open case."""
    async with pool.acquire() as con:
        async with con.transaction():
            case_row = await con.fetchrow(
                "SELECT h.id::text,h.salon_id::text,h.client_id::text FROM handoffs h "
                "JOIN clients c ON c.id=h.client_id AND c.salon_id=h.salon_id "
                "WHERE h.salon_id=$1::uuid AND c.wa_id=$2 AND h.status='open' FOR UPDATE OF h",
                salon_id, customer_identity)
            if case_row is None:
                return None
            case = dict(case_row)
            inbound_id = None
            from .channels.outbound import current_message_id
            mid = current_message_id()
            if mid:
                inbound_id = await con.fetchval(
                    "SELECT id::text FROM handoff_events WHERE handoff_id=$1::uuid AND event_type='customer_message' "
                    "AND provider_message_id=$2 ORDER BY sequence DESC LIMIT 1", case["id"], mid)
            related = inbound_id
            if related:
                prior = await con.fetchrow(
                    "SELECT id::text,sequence FROM handoff_events WHERE handoff_id=$1::uuid AND related_event_id=$2::uuid "
                    "AND event_type='assistant_reply' AND actor_type='assistant'", case["id"], related)
                if prior:
                    return {"id": prior["id"], "sequence": prior["sequence"], "duplicate": True}
            return await _next_event(
                con, case, event_type="assistant_reply", actor_type="assistant", visibility="customer_visible",
                body=body, channel=channel_for(customer_identity), provider_message_id=provider_message_id,
                related_event_id=related)


async def resolve_handoff(pool, salon_id: str, handoff_id: str, actor_identity: str, reason: str,
                          *, expected_version: int | None = None,
                          acknowledge_unknown_reply: bool = False,
                          session_id: str | None = None,
                          device_label: str | None = None,
                          reason_code: str = "other",
                          acknowledgement_note: str | None = None) -> dict | None:
    """Resolve with the shared handoff-then-outbox lock order; payment state is untouched."""
    actor_identity = (actor_identity or "").strip()
    reason = " ".join((reason or "").split())[:200]
    if not actor_identity or not reason:
        raise ValueError("owner identity and resolution reason are required")
    if reason_code not in {"handled_in_person", "customer_updated", "no_action_needed", "other"}:
        raise ValueError("invalid resolution reason")
    async with pool.acquire() as con:
        async with con.transaction():
            salon = await con.fetchrow("SELECT owner_wa_id FROM salons WHERE id=$1::uuid FOR SHARE", salon_id)
            if salon is None:
                return None
            owner_identity = (salon["owner_wa_id"] or "").strip()
            if owner_identity.lstrip("+") != actor_identity.lstrip("+"):
                raise PermissionError("actor is not the salon owner")
            case_row = await con.fetchrow(
                "SELECT id::text,salon_id::text,client_id::text,status,version,resolved_at,resolution_result FROM handoffs "
                "WHERE id=$1::uuid AND salon_id=$2::uuid FOR UPDATE", handoff_id, salon_id)
            if case_row is None:
                return None
            case = dict(case_row)
            if case["status"] != "open":
                saved = case.get("resolution_result")
                if isinstance(saved, str):
                    saved = json.loads(saved)
                if isinstance(saved, dict):
                    return saved
                return {"id": case["id"], "status": "resolved", "already_resolved": True,
                        "version": int(case["version"]), "resolved_at": case["resolved_at"],
                        "unknown_reply_acknowledged": False, "cancelled_reply_ids": []}
            if expected_version is not None and int(case["version"]) != int(expected_version):
                raise CaseVersionConflict("case changed; refresh its context before resolving")

            # Dispatch takes the same case row lock before any outbox row.
            unresolved = await con.fetch(
                "SELECT id::text,state FROM outbound_outbox WHERE handoff_id=$1::uuid AND salon_id=$2::uuid "
                "AND state IN ('queued','sending','unknown') ORDER BY id FOR UPDATE",
                case["id"], salon_id)
            if len(unresolved) > 1:
                raise LegacyUnresolvedReplyViolation("case has multiple unresolved outbound replies")
            sending = next((row for row in unresolved if row["state"] == "sending"), None)
            if sending is not None:
                raise CaseHasSendingReply("a customer reply is currently being submitted")
            unknown = next((row for row in unresolved if row["state"] == "unknown"), None)
            if unknown is not None and not acknowledge_unknown_reply:
                raise UnknownReplyNeedsAcknowledgement("an outbound reply has an ambiguous provider outcome")
            if unknown is None and acknowledge_unknown_reply:
                raise NoUnknownReply("there is no unknown outbound reply to acknowledge")
            if unknown is not None:
                await con.execute(
                    "INSERT INTO handoff_unknown_reply_acknowledgements "
                    "(handoff_id,salon_id,client_id,reply_id,owner_identity,session_id,device_label,reason,note) "
                    "VALUES ($1::uuid,$2::uuid,$3::uuid,$4::uuid,$5,$6::uuid,$7,$8,$9)",
                    case["id"], salon_id, case["client_id"], unknown["id"], actor_identity,
                    session_id, device_label, reason_code,
                    " ".join((acknowledgement_note or "").split())[:200] or None)

            cancelled = [row["id"] for row in unresolved if row["state"] == "queued"]
            if cancelled:
                await con.execute(
                    "UPDATE outbound_outbox SET state='cancelled',last_error_category='case_resolved',updated_at=now() "
                    "WHERE handoff_id=$1::uuid AND salon_id=$2::uuid AND id=ANY($3::uuid[]) AND state='queued'",
                    case["id"], salon_id, [uuid.UUID(reply_id) for reply_id in cancelled])
            updated = await con.fetchrow(
                "UPDATE handoffs SET status='resolved',resolved_at=now(),resolved_by=$2,resolution_reason=$3,version=version+1,event_seq=event_seq+1 "
                "WHERE id=$1::uuid AND status='open' RETURNING event_seq,version,resolved_at", case["id"], actor_identity, reason)
            await con.execute(
                "INSERT INTO handoff_events (handoff_id,salon_id,client_id,sequence,event_type,actor_type,visibility,body,channel) "
                "VALUES ($1::uuid,$2::uuid,$3::uuid,$4,'case_resolved','owner','owner_internal',$5,$6)",
                case["id"], salon_id, case["client_id"], updated["event_seq"], reason,
                channel_for(actor_identity))
            client_identity = await con.fetchval(
                "SELECT wa_id FROM clients WHERE id=$1::uuid AND salon_id=$2::uuid", case["client_id"], salon_id)
            await con.execute(
                "UPDATE conversations SET state=COALESCE(state,'{}'::jsonb)-'state'-'category',updated_at=now() "
                "WHERE salon_id=$1::uuid AND wa_id=$2 AND state->>'state'='handoff'", salon_id, client_identity)
            result = {"id": case["id"], "status": "resolved", "already_resolved": False,
                      "version": int(updated["version"]), "resolved_by": actor_identity,
                      "resolved_at": updated["resolved_at"].astimezone(timezone.utc).isoformat().replace("+00:00", "Z"),
                      "resolution_reason": reason,
                      "unknown_reply_acknowledged": bool(unknown), "cancelled_reply_ids": cancelled,
                      "acknowledged_unknown_reply_id": unknown["id"] if unknown else None}
            await con.execute("UPDATE handoffs SET resolution_result=$2::jsonb WHERE id=$1::uuid",
                              case["id"], json.dumps(result))
            return result
