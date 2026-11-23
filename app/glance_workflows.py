"""Transactional draft lifecycle and payload-bound Glance idempotency."""
from __future__ import annotations

import hashlib
import json
import secrets
import unicodedata
import uuid
from dataclasses import dataclass
from datetime import datetime, timezone

from .handoff_drafts import GLANCE_DRAFT_TTL_MINUTES, insert_draft
from .handoffs import _verified_owner


class WorkflowError(RuntimeError):
    def __init__(self, code: str, status: int, message: str, retry_after: int | None = None):
        super().__init__(code)
        self.code = code
        self.status = status
        self.message = message
        self.retry_after = retry_after


@dataclass(frozen=True)
class RequestClaim:
    id: str
    lease_token: str


def canonical_text(value: str, *, maximum: int) -> str:
    if not isinstance(value, str):
        raise WorkflowError("invalid_text", 422, "Text is invalid.")
    text = unicodedata.normalize("NFC", value).strip()
    if not text or len(text) > maximum or any(
            unicodedata.category(char) == "Cc" and char != "\n" for char in text):
        raise WorkflowError("invalid_text", 422, "Text is invalid.")
    return text


def canonical_idempotency_key(value: str | None) -> str:
    try:
        parsed = uuid.UUID(str(value))
    except (TypeError, ValueError, AttributeError):
        raise WorkflowError("invalid_idempotency_key", 422, "A UUIDv4 Idempotency-Key is required.")
    if parsed.version != 4 or str(parsed) != str(value).lower():
        raise WorkflowError("invalid_idempotency_key", 422, "A UUIDv4 Idempotency-Key is required.")
    return str(parsed)


def payload_hash(payload: dict) -> bytes:
    raw = json.dumps(payload, ensure_ascii=False, sort_keys=True, separators=(",", ":")).encode("utf-8")
    return hashlib.sha256(raw).digest()


async def claim_request(pool, salon_id: str, method: str, resource_path: str,
                       key: str, payload: dict) -> tuple[str, RequestClaim | dict]:
    """Return claimed/replay; conflicting and currently-running keys fail closed."""
    digest = payload_hash(payload)
    lease_token = str(uuid.uuid4())
    async with pool.acquire() as con:
        async with con.transaction():
            row = await con.fetchrow(
                "INSERT INTO glance_request_idempotency "
                "(salon_id,http_method,resource_path,idempotency_key,payload_hash,state,lease_until,lease_token,expires_at) "
                "VALUES ($1::uuid,$2,$3,$4,$5,'in_progress',now()+interval '2 minutes',$6::uuid,now()+interval '24 hours') "
                "ON CONFLICT (salon_id,http_method,resource_path,idempotency_key) DO NOTHING "
                "RETURNING id::text",
                salon_id, method, resource_path, key, digest, lease_token)
            if row is not None:
                return "claimed", RequestClaim(row["id"], lease_token)

            row = await con.fetchrow(
                "SELECT id::text,payload_hash,state,response_status,response_body,expires_at,lease_until,lease_token::text "
                "FROM glance_request_idempotency WHERE salon_id=$1::uuid AND http_method=$2 "
                "AND resource_path=$3 AND idempotency_key=$4 FOR UPDATE",
                salon_id, method, resource_path, key)
            now = await con.fetchval("SELECT now()")
            if row is None:
                raise WorkflowError("request_in_progress", 409, "The request is still being processed.", 1)
            if row["state"] == "completed" and row["expires_at"] <= now:
                await con.execute(
                    "UPDATE glance_request_idempotency SET payload_hash=$2,state='in_progress',response_status=NULL,"
                    "response_body=NULL,lease_until=now()+interval '2 minutes',lease_token=$3::uuid,"
                    "created_at=now(),updated_at=now(),expires_at=now()+interval '24 hours' WHERE id=$1::uuid",
                    row["id"], digest, lease_token)
                return "claimed", RequestClaim(row["id"], lease_token)
            if not secrets.compare_digest(bytes(row["payload_hash"]), digest):
                raise WorkflowError("idempotency_conflict", 409, "This key was already used for a different request.")
            if row["state"] == "completed":
                body = row["response_body"]
                if isinstance(body, str):
                    body = json.loads(body)
                return "replay", {"status": int(row["response_status"]), "body": body}
            if row["lease_until"] is not None and row["lease_until"] <= now:
                await con.execute(
                    "UPDATE glance_request_idempotency SET lease_until=now()+interval '2 minutes',"
                    "lease_token=$2::uuid,updated_at=now() WHERE id=$1::uuid",
                    row["id"], lease_token)
                return "claimed", RequestClaim(row["id"], lease_token)
            raise WorkflowError("request_in_progress", 409, "The request is still being processed.", 1)


async def complete_request(con, claim: RequestClaim, response_status: int, response_body: dict) -> None:
    result = await con.execute(
        "UPDATE glance_request_idempotency SET state='completed',response_status=$3,response_body=$4::jsonb,"
        "lease_until=NULL,lease_token=NULL,updated_at=now(),expires_at=now()+interval '24 hours' "
        "WHERE id=$1::uuid AND state='in_progress' AND lease_token=$2::uuid",
        claim.id, claim.lease_token, response_status, json.dumps(response_body, ensure_ascii=False))
    if result != "UPDATE 1":
        raise WorkflowError("request_in_progress", 409, "The request is still being processed.", 1)


async def complete_error(pool, claim: RequestClaim, response_status: int,
                         code: str, message: str) -> dict:
    body = {"error": {"code": code, "message": message}}
    async with pool.acquire() as con:
        async with con.transaction():
            await complete_request(con, claim, response_status, body)
    return body


async def release_request(pool, claim: RequestClaim) -> None:
    await pool.execute(
        "DELETE FROM glance_request_idempotency WHERE id=$1::uuid AND state='in_progress' AND lease_token=$2::uuid",
        claim.id, claim.lease_token)


async def scope_case(pool, salon_id: str, handoff_id: str, owner_identity: str) -> bool:
    """Resource lookup before idempotency; the scoped service hides cross-salon IDs."""
    from .handoffs import get_glance_handoff_context
    case = await get_glance_handoff_context(pool, salon_id, handoff_id, owner_identity, limit=1)
    return case is not None


async def load_revisable_draft(pool, salon_id: str, handoff_id: str, draft_id: str,
                               owner_identity: str, expected_version: int) -> dict:
    async with pool.acquire() as con:
        async with con.transaction():
            await _verified_owner(con, salon_id, owner_identity)
            case = await con.fetchrow(
                "SELECT id::text,client_id::text,status,version FROM handoffs "
                "WHERE id=$1::uuid AND salon_id=$2::uuid", handoff_id, salon_id)
            if case is None:
                raise WorkflowError("case_not_found", 404, "Case not found.")
            if case["status"] != "open" or int(case["version"]) != int(expected_version):
                raise WorkflowError("stale_case_version", 409, "The case changed. Refresh it before revising.")
            draft = await con.fetchrow(
                "SELECT id::text,source,requested_by,based_case_version,body,expires_at,consumed_at,superseded_at "
                "FROM handoff_drafts WHERE id=$1::uuid AND handoff_id=$2::uuid AND salon_id=$3::uuid "
                "AND client_id=$4::uuid", draft_id, handoff_id, salon_id, case["client_id"])
            if draft is None or draft["requested_by"] != owner_identity:
                raise WorkflowError("draft_not_found", 404, "Draft not found.")
            if draft["superseded_at"] is not None:
                raise WorkflowError("draft_superseded", 409, "This draft has already been superseded.")
            if draft["consumed_at"] is not None:
                raise WorkflowError("draft_consumed", 409, "This draft has already been approved.")
            if draft["expires_at"] <= await con.fetchval("SELECT now()"):
                raise WorkflowError("draft_expired", 409, "This draft has expired.")
            if draft["source"] != "ai":
                raise WorkflowError("draft_not_revisable", 409, "Only an AI draft can be revised.")
            if int(draft["based_case_version"]) != int(expected_version):
                raise WorkflowError("stale_case_version", 409, "The draft no longer matches this case version.")
            return dict(draft)


async def create_manual_draft(pool, salon_id: str, handoff_id: str, owner_identity: str,
                              expected_version: int, text: str, claim: RequestClaim,
                              response_builder) -> tuple[int, dict]:
    async with pool.acquire() as con:
        async with con.transaction():
            await _verified_owner(con, salon_id, owner_identity)
            case = await con.fetchrow(
                "SELECT id::text,client_id::text,status,version FROM handoffs "
                "WHERE id=$1::uuid AND salon_id=$2::uuid FOR UPDATE", handoff_id, salon_id)
            if case is None:
                raise WorkflowError("case_not_found", 404, "Case not found.")
            if case["status"] != "open" or int(case["version"]) != int(expected_version):
                raise WorkflowError("stale_case_version", 409, "The case changed. Refresh it before drafting.")
            draft = await insert_draft(
                con, handoff_id=handoff_id, salon_id=salon_id, client_id=case["client_id"],
                expected_version=expected_version, owner_identity=owner_identity, body=text,
                model_id="owner_manual", source="owner", ttl_minutes=GLANCE_DRAFT_TTL_MINUTES)
            body = {"data": response_builder(draft)}
            await complete_request(con, claim, 201, body)
            return 201, body


async def save_generated_draft(pool, salon_id: str, handoff_id: str, owner_identity: str,
                               expected_version: int, generated: dict, claim: RequestClaim,
                               response_builder, *, old_draft_id: str | None = None,
                               instruction: str | None = None) -> tuple[int, dict]:
    async with pool.acquire() as con:
        async with con.transaction():
            await _verified_owner(con, salon_id, owner_identity)
            case = await con.fetchrow(
                "SELECT id::text,client_id::text,status,version FROM handoffs "
                "WHERE id=$1::uuid AND salon_id=$2::uuid FOR UPDATE", handoff_id, salon_id)
            if case is None:
                raise WorkflowError("case_not_found", 404, "Case not found.")
            if case["status"] != "open" or int(case["version"]) != int(expected_version):
                raise WorkflowError("stale_case_version", 409, "The case changed while the draft was being prepared.")
            if old_draft_id:
                old = await con.fetchrow(
                    "SELECT source,requested_by,based_case_version,expires_at,consumed_at,superseded_at "
                    "FROM handoff_drafts WHERE id=$1::uuid AND handoff_id=$2::uuid AND salon_id=$3::uuid "
                    "AND client_id=$4::uuid FOR UPDATE", old_draft_id, handoff_id, salon_id, case["client_id"])
                if old is None or old["requested_by"] != owner_identity:
                    raise WorkflowError("draft_not_found", 404, "Draft not found.")
                if old["superseded_at"] is not None:
                    raise WorkflowError("draft_superseded", 409, "This draft has already been superseded.")
                if old["consumed_at"] is not None:
                    raise WorkflowError("draft_consumed", 409, "This draft has already been approved.")
                if old["expires_at"] <= await con.fetchval("SELECT now()"):
                    raise WorkflowError("draft_expired", 409, "This draft has expired.")
                if old["source"] != "ai":
                    raise WorkflowError("draft_not_revisable", 409, "Only an AI draft can be revised.")
                if int(old["based_case_version"]) != int(expected_version):
                    raise WorkflowError("stale_case_version", 409, "The draft no longer matches this case version.")

            draft = await insert_draft(
                con, handoff_id=handoff_id, salon_id=salon_id, client_id=case["client_id"],
                expected_version=expected_version, owner_identity=owner_identity,
                body=generated["body"], model_id=generated["model_id"], source="ai",
                instruction=instruction, ttl_minutes=GLANCE_DRAFT_TTL_MINUTES)
            if old_draft_id:
                await con.execute(
                    "UPDATE handoff_drafts SET superseded_at=now() WHERE id=$1::uuid AND superseded_at IS NULL",
                    old_draft_id)
            body = {"data": response_builder(draft)}
            await complete_request(con, claim, 200, body)
            return 200, body


def utc_iso(value: datetime) -> str:
    if value.tzinfo is None:
        value = value.replace(tzinfo=timezone.utc)
    return value.astimezone(timezone.utc).isoformat(timespec="seconds").replace("+00:00", "Z")
