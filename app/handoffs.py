"""Durable human handoffs, case history, and explicit resolution."""
from __future__ import annotations

import json



def channel_for(identity: str | None) -> str:
    return "telegram" if (identity or "").startswith("tg:") else "whatsapp"


async def _verified_owner(con, salon_id: str, owner_identity: str) -> None:
    row = await con.fetchrow("SELECT owner_wa_id FROM salons WHERE id=$1::uuid FOR KEY SHARE", salon_id)
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
                "CASE WHEN c.wa_id LIKE 'tg:%' THEN 'telegram' ELSE 'whatsapp' END AS channel "
                "FROM handoffs h JOIN clients c ON c.id=h.client_id AND c.salon_id=h.salon_id "
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
                "SELECT id::text, salon_id::text, client_id::text, status FROM handoffs "
                "WHERE salon_id=$1::uuid AND client_id=$2::uuid AND status='open' FOR UPDATE",
                salon_id, client_id)
            created = case_row is None
            if created:
                case_row = await con.fetchrow(
                    "INSERT INTO handoffs (salon_id,client_id,category,message) VALUES ($1::uuid,$2::uuid,$3,$4) "
                    "RETURNING id::text,salon_id::text,client_id::text,status", salon_id, client_id, category, text[:1000])
            case = dict(case_row)

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
                if created:
                    await _next_event(con, case, event_type="case_opened", actor_type="system", visibility="owner_internal",
                                      body=category, channel=channel)
                if owner_notification:
                    await _next_event(
                        con, case, event_type="owner_notification", actor_type="system", visibility="owner_internal",
                        body=owner_notification, channel=channel, related_event_id=inbound["id"])

            state = json.dumps({"state": "handoff", "category": category})
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


async def resolve_handoff(pool, salon_id: str, handoff_id: str, actor_identity: str, reason: str) -> dict | None:
    """Resolve only with the salon's verified current owner identity; payments are untouched."""
    actor_identity = (actor_identity or "").strip()
    reason = " ".join((reason or "").split())[:200]
    if not actor_identity or not reason:
        raise ValueError("owner identity and resolution reason are required")
    async with pool.acquire() as con:
        async with con.transaction():
            salon = await con.fetchrow("SELECT owner_wa_id FROM salons WHERE id=$1::uuid FOR KEY SHARE", salon_id)
            if salon is None:
                return None
            owner_identity = (salon["owner_wa_id"] or "").strip()
            if owner_identity.lstrip("+") != actor_identity.lstrip("+"):
                raise PermissionError("actor is not the salon owner")
            case_row = await con.fetchrow(
                "SELECT id::text,salon_id::text,client_id::text,status FROM handoffs "
                "WHERE id=$1::uuid AND salon_id=$2::uuid FOR UPDATE", handoff_id, salon_id)
            if case_row is None:
                return None
            case = dict(case_row)
            if case["status"] != "open":
                return {"id": case["id"], "status": "resolved", "already_resolved": True}
            updated = await con.fetchrow(
                "UPDATE handoffs SET status='resolved',resolved_at=now(),resolved_by=$2,resolution_reason=$3,version=version+1,event_seq=event_seq+1 "
                "WHERE id=$1::uuid AND status='open' RETURNING event_seq,version", case["id"], actor_identity, reason)
            await con.execute(
                "INSERT INTO handoff_events (handoff_id,salon_id,client_id,sequence,event_type,actor_type,visibility,body,channel) "
                "VALUES ($1::uuid,$2::uuid,$3::uuid,$4,'case_resolved','owner','owner_internal',$5,$6)",
                case["id"], salon_id, case["client_id"], updated["event_seq"], reason,
                channel_for(actor_identity))
            client_identity = await con.fetchval("SELECT wa_id FROM clients WHERE id=$1::uuid AND salon_id=$2::uuid", case["client_id"], salon_id)
            await con.execute(
                "UPDATE conversations SET state=COALESCE(state,'{}'::jsonb)-'state'-'category',updated_at=now() "
                "WHERE salon_id=$1::uuid AND wa_id=$2 AND state->>'state'='handoff'", salon_id, client_identity)
            return {"id": case["id"], "status": "resolved", "already_resolved": False,
                    "version": updated["version"], "resolved_by": actor_identity, "resolution_reason": reason}
