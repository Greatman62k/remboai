"""Small authenticated HTTP boundary for the Day 1 Glance vertical slice."""
from __future__ import annotations

import logging
import re
import unicodedata
from datetime import datetime, timezone
from uuid import UUID
from typing import Literal

from fastapi import APIRouter, Depends, FastAPI, Header, Query, Request
from fastapi.exceptions import RequestValidationError
from fastapi.responses import JSONResponse, Response
from pydantic import BaseModel, ConfigDict, Field
from starlette.exceptions import HTTPException as StarletteHTTPException

from . import glance_sessions, glance_workflows, handoff_drafts, handoff_outbox, handoffs
from .hardening import client_ip

log = logging.getLogger("rembo.glance")
PREFIX = "/api/v1/glance"
_BIDI = {"\u061c", "\u200e", "\u200f", "\u202a", "\u202b", "\u202c", "\u202d", "\u202e",
         "\u2066", "\u2067", "\u2068", "\u2069"}
_PHONE_LIKE = re.compile(r"(?<!\w)\+?\d[\d ().-]{7,}\d(?!\w)")
_REFERENCE_LIKE = re.compile(r"(?<![A-Z0-9])[A-Z0-9]{10}(?![A-Z0-9])")
_EMAIL_LIKE = re.compile(r"(?<![\w.+-])[\w.+-]{1,64}@[\w.-]{1,190}\.[A-Za-z]{2,}(?![\w.-])")
_URL_LIKE = re.compile(r"\bhttps?://\S+", re.I)


def _is_glance_path(path: str) -> bool:
    return path == PREFIX or path.startswith(PREFIX + "/")


class GlanceAPIError(RuntimeError):
    def __init__(self, code: str, status_code: int, message: str, retry_after: int | None = None):
        super().__init__(code)
        self.code = code
        self.status_code = status_code
        self.message = message
        self.retry_after = retry_after


class ExchangeBody(BaseModel):
    model_config = ConfigDict(extra="forbid")
    code: str
    device_label: str


class ExpectedVersionBody(BaseModel):
    model_config = ConfigDict(extra="forbid")
    expected_version: int = Field(ge=1)


class ManualDraftBody(ExpectedVersionBody):
    text: str


class ReviseDraftBody(ExpectedVersionBody):
    instruction: str


class ReplyBody(ExpectedVersionBody):
    draft_id: str
    text: str


class ResolveBody(ExpectedVersionBody):
    reason: Literal["handled_in_person", "customer_updated", "no_action_needed", "other"]
    note: str | None = None
    acknowledge_unknown_reply: bool = False


router = APIRouter(prefix=PREFIX, tags=["glance"])


def utc_timestamp(value: datetime | str | None) -> str | None:
    if value is None:
        return None
    if isinstance(value, str):
        try:
            value = datetime.fromisoformat(value.replace("Z", "+00:00"))
        except ValueError:
            return None
    if value.tzinfo is None:
        value = value.replace(tzinfo=timezone.utc)
    return value.astimezone(timezone.utc).isoformat(timespec="seconds").replace("+00:00", "Z")


def sanitize_display_text(value: str | None, limit: int | None = None) -> str:
    text = unicodedata.normalize("NFC", str(value or ""))
    kept = []
    for char in text:
        category = unicodedata.category(char)
        if char in _BIDI or category in {"Cf", "Cs", "Co", "Cn"}:
            continue
        if category == "Cc" and char not in "\n\t":
            continue
        kept.append(char)
    text = "".join(kept)
    text = _PHONE_LIKE.sub("[contact redacted]", text)
    text = _EMAIL_LIKE.sub("[contact redacted]", text)
    text = _URL_LIKE.sub("[link redacted]", text)
    text = _REFERENCE_LIKE.sub("[reference redacted]", text)
    text = "\n".join(" ".join(line.split()) for line in text.splitlines())
    if limit is not None:
        text = text[:limit]
    return text.strip()


def _safe_customer_label(value: str | None) -> str:
    label = sanitize_display_text(value)
    # A client name should never fall back to a phone number stored in `clients.name`.
    if not label or not re.search(r"[A-Za-z\u00c0-\uffff]", label):
        return "Customer"
    return label[:24].strip() or "Customer"


def _safe_error(request: Request, status: int, code: str, message: str,
                retry_after: int | None = None) -> JSONResponse:
    request_id = getattr(request.state, "glance_request_id", "req_unavailable")
    headers = {"Cache-Control": "no-store", "X-Request-ID": request_id}
    if retry_after is not None:
        headers["Retry-After"] = str(max(1, int(retry_after)))
    elif status in (429, 503):
        headers["Retry-After"] = "1"
    return JSONResponse({"error": {"code": code, "message": message}, "request_id": request_id},
                        status_code=status, headers=headers)


async def _handle_glance_auth_error(request: Request, exc: glance_sessions.GlanceAuthError):
    if not _is_glance_path(request.url.path):
        return JSONResponse({"detail": "Forbidden"}, status_code=exc.status_code)
    return _safe_error(request, exc.status_code, exc.code, exc.message, exc.retry_after)


async def _handle_glance_api_error(request: Request, exc: GlanceAPIError):
    return _safe_error(request, exc.status_code, exc.code, exc.message, exc.retry_after)


async def _handle_http_error(request: Request, exc: StarletteHTTPException):
    if not _is_glance_path(request.url.path):
        return JSONResponse({"detail": exc.detail}, status_code=exc.status_code, headers=exc.headers)
    if exc.status_code == 404:
        code, message = "not_found", "The requested resource was not found."
    elif exc.status_code == 405:
        code, message = "method_not_allowed", "This method is not supported."
    else:
        code, message = "request_rejected", "The request could not be completed."
    return _safe_error(request, exc.status_code, code, message,
                       int((exc.headers or {}).get("Retry-After", "1")) if exc.status_code in (429, 503) else None)


async def _handle_validation_error(request: Request, exc: RequestValidationError):
    if not _is_glance_path(request.url.path):
        return JSONResponse({"detail": exc.errors()}, status_code=422)
    return _safe_error(request, 422, "invalid_request", "The request is invalid.")


async def _handle_unexpected_error(request: Request, exc: Exception):
    if not _is_glance_path(request.url.path):
        raise exc
    log.error("glance request failed request_id=%s error=%s",
              getattr(request.state, "glance_request_id", "req_unavailable"), exc.__class__.__name__)
    return _safe_error(request, 500, "internal_error", "The request could not be completed.")


def _origin_settings(settings) -> tuple[list[str], bool]:
    raw = getattr(settings, "glance_cors_origins", "") or ""
    origins = [origin.strip() for origin in raw.split(",") if origin.strip()]
    wildcard = bool(getattr(settings, "glance_cors_wildcard_bearer", False))
    return origins, wildcard


def _cors_headers(origin: str | None, allowed: bool, wildcard: bool) -> list[tuple[bytes, bytes]]:
    headers: list[tuple[bytes, bytes]] = []
    if origin and allowed:
        allow_origin = b"*" if wildcard else origin.encode("latin-1", "replace")
        headers.append((b"access-control-allow-origin", allow_origin))
        if not wildcard:
            headers.append((b"vary", b"Origin"))
        headers.append((b"access-control-expose-headers", b"Retry-After, X-Request-ID"))
    return headers


class GlanceTransportMiddleware:
    """Adds request IDs/no-store and handles unauthenticated, narrow CORS preflights."""
    METHODS = {"GET", "POST", "DELETE", "OPTIONS"}
    HEADERS = {"authorization", "content-type", "idempotency-key"}

    def __init__(self, app):
        self.app = app

    async def __call__(self, scope, receive, send):
        if scope["type"] != "http" or not _is_glance_path(scope.get("path", "")):
            await self.app(scope, receive, send)
            return
        import secrets
        request_id = "req_" + secrets.token_hex(12)
        state = scope.setdefault("state", {})
        state["glance_request_id"] = request_id
        headers = {key.lower(): value for key, value in scope.get("headers", [])}
        origin_raw = headers.get(b"origin")
        origin = origin_raw.decode("latin-1") if origin_raw else None
        settings = getattr(getattr(scope.get("app"), "state", None), "settings", None)
        if settings is None:
            from .config import get_settings
            settings = get_settings()
        origins, configured_wildcard = _origin_settings(settings)
        # Pairing exchange is authorized by a short code, not a bearer session.
        # It therefore always needs an exact packaged origin, even when the
        # authenticated read/delete routes use the documented wildcard fallback.
        pairing_exchange = scope.get("path") == PREFIX + "/pairing/exchange"
        exact_origin = bool(origin and origin in origins)
        wildcard = bool(configured_wildcard and not pairing_exchange and not exact_origin)
        allowed_origin = bool(exact_origin or (origin and wildcard))

        async def send_start(message):
            if message.get("type") != "http.response.start":
                await send(message)
                return
            extras = [(b"x-request-id", request_id.encode()), (b"cache-control", b"no-store")]
            if message.get("status") in (429, 503) and b"retry-after" not in {k.lower() for k, _ in message.get("headers", [])}:
                extras.append((b"retry-after", b"1"))
            extras.extend(_cors_headers(origin, allowed_origin, wildcard))
            existing = {key.lower() for key, _ in message.get("headers", [])}
            message["headers"] = [(k, v) for k, v in message.get("headers", []) if k.lower() not in {x[0] for x in extras}]
            message["headers"].extend((k, v) for k, v in extras if k.lower() not in existing or k in (b"x-request-id", b"cache-control"))
            await send(message)

        if scope["method"].upper() == "OPTIONS":
            req_method = headers.get(b"access-control-request-method", b"").decode("latin-1").upper()
            req_headers = {item.strip().lower() for item in headers.get(b"access-control-request-headers", b"").decode("latin-1").split(",") if item.strip()}
            if not allowed_origin or req_method not in self.METHODS or not req_headers.issubset(self.HEADERS):
                body = ("{\"error\":{\"code\":\"cors_rejected\",\"message\":\"Cross-origin request is not allowed.\"},"
                        f"\"request_id\":\"{request_id}\"}}").encode()
                cors = _cors_headers(None, False, wildcard)
                out = [(b"content-type", b"application/json"), (b"content-length", str(len(body)).encode()),
                       (b"cache-control", b"no-store"), (b"x-request-id", request_id.encode())]
                out.extend(cors)
                await send({"type": "http.response.start", "status": 403, "headers": out})
                await send({"type": "http.response.body", "body": body})
                return
            preflight = _cors_headers(origin, True, wildcard)
            preflight += [
                (b"access-control-allow-methods", b"GET, POST, DELETE, OPTIONS"),
                (b"access-control-allow-headers", b"Authorization, Content-Type, Idempotency-Key"),
                (b"access-control-max-age", b"600"),
                (b"content-length", b"0"),
                (b"cache-control", b"no-store"),
                (b"x-request-id", request_id.encode()),
            ]
            await send({"type": "http.response.start", "status": 204, "headers": preflight})
            await send({"type": "http.response.body", "body": b""})
            return
        if origin and not allowed_origin:
            body = ("{\"error\":{\"code\":\"cors_rejected\",\"message\":\"Cross-origin request is not allowed.\"},"
                    f"\"request_id\":\"{request_id}\"}}").encode()
            await send({"type": "http.response.start", "status": 403, "headers": [
                (b"content-type", b"application/json"), (b"content-length", str(len(body)).encode()),
                (b"cache-control", b"no-store"), (b"x-request-id", request_id.encode())]})
            await send({"type": "http.response.body", "body": body})
            return
        await self.app(scope, receive, send_start)


async def require_session(request: Request) -> glance_sessions.GlanceSession:
    glance_sessions.require_enabled(request.app.state.settings)
    header = request.headers.get("authorization", "")
    parts = header.split()
    if len(parts) != 2 or parts[0].lower() != "bearer":
        raise glance_sessions.GlanceAuthError("invalid_session", 401, "A valid session is required")
    session = await glance_sessions.authenticate_session(request.app.state.pool, request.app.state.settings, parts[1])
    if request.method == "GET":
        await glance_sessions.check_read_quota(request.app.state.pool, request.app.state.settings, session)
    request.state.glance_session = session
    return session


def _canonical_uuid(value: str, code: str = "case_not_found") -> str:
    try:
        return str(UUID(value))
    except (TypeError, ValueError, AttributeError):
        raise GlanceAPIError(code, 404, "The requested resource was not found.")


async def _begin_write(request: Request, session: glance_sessions.GlanceSession,
                       case_id: str, key: str | None, payload: dict,
                       resource_path: str) -> tuple[glance_workflows.RequestClaim | None, JSONResponse | None]:
    try:
        key = glance_workflows.canonical_idempotency_key(key)
    except glance_workflows.WorkflowError as exc:
        raise GlanceAPIError(exc.code, exc.status, exc.message, exc.retry_after)
    try:
        exists = await glance_workflows.scope_case(
            request.app.state.pool, session.salon_id, case_id, session.owner_identity)
    except PermissionError:
        raise glance_sessions.GlanceAuthError("owner_not_authorized", 403, "Owner no longer controls this salon")
    if not exists:
        raise GlanceAPIError("case_not_found", 404, "Case not found.")
    try:
        kind, result = await glance_workflows.claim_request(
            request.app.state.pool, session.salon_id, "POST", resource_path, key, payload)
    except glance_workflows.WorkflowError as exc:
        raise GlanceAPIError(exc.code, exc.status, exc.message, exc.retry_after)
    if kind == "replay":
        return None, JSONResponse(result["body"], status_code=result["status"])
    return result, None


def _normalize_text(value: str, maximum: int) -> str:
    try:
        return glance_workflows.canonical_text(value, maximum=maximum)
    except glance_workflows.WorkflowError as exc:
        raise GlanceAPIError(exc.code, exc.status, exc.message, exc.retry_after)


async def _cache_write_error(request: Request, claim: glance_workflows.RequestClaim,
                             status: int, code: str, message: str,
                             retry_after: int | None = None) -> JSONResponse:
    body = {"error": {"code": code, "message": message},
            "request_id": getattr(request.state, "glance_request_id", "req_unavailable")}
    try:
        async with request.app.state.pool.acquire() as con:
            async with con.transaction():
                await glance_workflows.complete_request(con, claim, status, body)
    except glance_workflows.WorkflowError as exc:
        return _safe_error(request, exc.status, exc.code, exc.message, exc.retry_after)
    headers = {"Cache-Control": "no-store", "X-Request-ID": body["request_id"]}
    if retry_after is not None:
        headers["Retry-After"] = str(max(1, int(retry_after)))
    return JSONResponse(body, status_code=status, headers=headers)


def _draft_response(draft: dict, source: str) -> dict:
    return {
        "draft_id": draft["id"],
        "based_on_version": int(draft["based_case_version"]),
        "text": draft["body"],
        "source": source,
        "expires_at": utc_timestamp(draft["expires_at"]),
    }


def _workflow_error(exc: Exception) -> tuple[int, str, str] | None:
    if isinstance(exc, PermissionError):
        return 403, "owner_not_authorized", "Owner no longer controls this salon."
    if isinstance(exc, glance_workflows.WorkflowError):
        return exc.status, exc.code, exc.message
    if isinstance(exc, handoff_drafts.DraftTimeout):
        return 504, "draft_timeout", "Draft generation took too long."
    if isinstance(exc, handoff_drafts.InstructionConflict):
        return 422, "instruction_conflicts_with_record", "The instruction conflicts with a recorded business fact."
    if isinstance(exc, handoff_drafts.UnsafeDraft):
        return 422, "draft_rejected", "The draft could not pass safety checks."
    if isinstance(exc, handoff_outbox.StaleCaseVersion):
        return 409, "stale_case_version", "The case or draft changed. Refresh it and try again."
    return None


@router.post("/pairing/exchange")
async def exchange(body: ExchangeBody, request: Request):
    source = client_ip(request, getattr(request.app.state.settings, "trusted_proxy_cidrs", "") or "")
    result = await glance_sessions.exchange_pairing_code(
        request.app.state.pool, request.app.state.settings, body.code, body.device_label, source)
    return {"data": {"access_token": result["access_token"], "expires_at": utc_timestamp(result["expires_at"]),
                     "salon": result["salon"]}}


@router.delete("/sessions/current", status_code=204)
async def delete_current_session(request: Request):
    glance_sessions.require_enabled(request.app.state.settings)
    header = request.headers.get("authorization", "")
    parts = header.split()
    if len(parts) != 2 or parts[0].lower() != "bearer":
        raise glance_sessions.GlanceAuthError("invalid_session", 401, "A valid session is required")
    session = await glance_sessions.authenticate_session(
        request.app.state.pool, request.app.state.settings, parts[1], allow_revoked=True)
    await glance_sessions.revoke_current_session(request.app.state.pool, session.id)
    return Response(status_code=204)


@router.get("/inbox")
async def get_inbox(request: Request, limit: int = Query(default=20, ge=1, le=20),
                    session: glance_sessions.GlanceSession = Depends(require_session)):
    try:
        rows = await handoffs.list_open_handoffs(
            request.app.state.pool, session.salon_id, session.owner_identity, limit=limit)
    except PermissionError:
        raise glance_sessions.GlanceAuthError("owner_not_authorized", 403, "Owner no longer controls this salon")
    open_count = int(rows[0].get("open_count", len(rows))) if rows else 0
    items = [{
        "case_id": row["id"],
        "version": int(row["version"]),
        "status": row["status"],
        "category": sanitize_display_text(row["category"], 40),
        "created_at": utc_timestamp(row["created_at"]),
        "customer_label": _safe_customer_label(row.get("customer_name")),
        "channel": row["channel"],
        "preview": sanitize_display_text(row.get("preview"), 120),
    } for row in rows]
    return {"data": {"items": items, "open_count": open_count, "has_more": open_count > len(items), "next_cursor": None}}


@router.get("/cases/{case_id}")
async def get_case(case_id: str, request: Request,
                   session: glance_sessions.GlanceSession = Depends(require_session)):
    canonical_id = _canonical_uuid(case_id)
    try:
        case = await handoffs.get_glance_handoff_context(
            request.app.state.pool, session.salon_id, canonical_id, session.owner_identity, limit=11)
    except PermissionError:
        raise glance_sessions.GlanceAuthError("owner_not_authorized", 403, "Owner no longer controls this salon")
    if case is None:
        raise GlanceAPIError("case_not_found", 404, "Case not found.")
    messages = case["messages"]
    has_earlier = len(messages) == 11
    messages = messages[-10:]
    context = [{
        "sequence": int(message["sequence"]),
        "actor": message["actor_type"],
        "text": sanitize_display_text(message.get("body")),
        "created_at": utc_timestamp(message["created_at"]),
    } for message in messages]
    latest = case.get("latest_reply")
    latest_reply = ({"reply_id": latest["reply_id"], "state": latest["state"],
                     "updated_at": utc_timestamp(latest["updated_at"]),
                     "delivery": handoff_outbox.delivery_state(latest["channel"], latest["state"]),
                     "error_category": handoff_outbox._public_error_category(latest.get("last_error_category")),
                     "submitted_at": utc_timestamp(latest.get("submitted_at")),
                     "delivered_at": utc_timestamp(latest.get("delivered_at"))} if latest else None)
    return {"data": {
        "case_id": case["id"],
        "version": int(case["version"]),
        "status": case["status"],
        "category": sanitize_display_text(case["category"], 40),
        "channel": case["channel"],
        "customer_label": _safe_customer_label(case.get("customer_name")),
        "latest_reply": latest_reply,
        "has_earlier": has_earlier,
        "context": context,
    }}


@router.post("/cases/{case_id}/drafts")
async def create_ai_draft(case_id: str, body: ExpectedVersionBody, request: Request,
                          idempotency_key: str | None = Header(default=None, alias="Idempotency-Key"),
                          session: glance_sessions.GlanceSession = Depends(require_session)):
    canonical_id = _canonical_uuid(case_id)
    resource = f"{PREFIX}/cases/{canonical_id}/drafts"
    claim, replay = await _begin_write(
        request, session, canonical_id, idempotency_key,
        {"expected_version": body.expected_version}, resource)
    if replay is not None:
        return replay
    try:
        try:
            await glance_sessions.check_write_quota(request.app.state.pool, request.app.state.settings, session)
        except glance_sessions.GlanceAuthError as exc:
            return await _cache_write_error(request, claim, exc.status_code, exc.code, exc.message, exc.retry_after)
        generated = await handoff_drafts.generate_draft_content(
            request.app.state.ctx, session.salon_id, canonical_id, session.owner_identity,
            body.expected_version, timeout_seconds=handoff_drafts.GLANCE_MODEL_TIMEOUT_SECONDS)
        if generated is None:
            return await _cache_write_error(request, claim, 404, "case_not_found", "Case not found.")
        status, response_body = await glance_workflows.save_generated_draft(
            request.app.state.pool, session.salon_id, canonical_id, session.owner_identity,
            body.expected_version, generated, claim,
            lambda draft: _draft_response(draft, "ai"))
        return JSONResponse(response_body, status_code=status)
    except Exception as exc:
        mapped = _workflow_error(exc)
        if mapped:
            return await _cache_write_error(request, claim, *mapped)
        from .agent.client import AgentUnavailable
        if isinstance(exc, AgentUnavailable):
            return await _cache_write_error(request, claim, 503, "draft_unavailable", "Draft generation is temporarily unavailable.")
        log.warning("glance draft failed request_id=%s error=%s",
                    getattr(request.state, "glance_request_id", "req_unavailable"), exc.__class__.__name__)
        return await _cache_write_error(request, claim, 503, "draft_unavailable", "Draft generation is temporarily unavailable.")


@router.post("/cases/{case_id}/drafts/manual", status_code=201)
async def create_manual_draft(case_id: str, body: ManualDraftBody, request: Request,
                              idempotency_key: str | None = Header(default=None, alias="Idempotency-Key"),
                              session: glance_sessions.GlanceSession = Depends(require_session)):
    canonical_id = _canonical_uuid(case_id)
    text = _normalize_text(body.text, 700)
    resource = f"{PREFIX}/cases/{canonical_id}/drafts/manual"
    claim, replay = await _begin_write(
        request, session, canonical_id, idempotency_key,
        {"expected_version": body.expected_version, "text": text}, resource)
    if replay is not None:
        return replay
    try:
        status, response_body = await glance_workflows.create_manual_draft(
            request.app.state.pool, session.salon_id, canonical_id, session.owner_identity,
            body.expected_version, text, claim, lambda draft: _draft_response(draft, "owner"))
        return JSONResponse(response_body, status_code=status)
    except glance_workflows.WorkflowError as exc:
        return await _cache_write_error(request, claim, exc.status, exc.code, exc.message)
    except PermissionError:
        return await _cache_write_error(request, claim, 403, "owner_not_authorized", "Owner no longer controls this salon.")


@router.post("/cases/{case_id}/drafts/{draft_id}/revise")
async def revise_draft(case_id: str, draft_id: str, body: ReviseDraftBody, request: Request,
                       idempotency_key: str | None = Header(default=None, alias="Idempotency-Key"),
                       session: glance_sessions.GlanceSession = Depends(require_session)):
    canonical_id = _canonical_uuid(case_id)
    try:
        canonical_draft_id = str(UUID(draft_id))
    except (TypeError, ValueError, AttributeError):
        raise GlanceAPIError("draft_not_found", 404, "Draft not found.")
    instruction = _normalize_text(body.instruction, 300)
    resource = f"{PREFIX}/cases/{canonical_id}/drafts/{canonical_draft_id}/revise"
    claim, replay = await _begin_write(
        request, session, canonical_id, idempotency_key,
        {"expected_version": body.expected_version, "instruction": instruction}, resource)
    if replay is not None:
        return replay
    try:
        old = await glance_workflows.load_revisable_draft(
            request.app.state.pool, session.salon_id, canonical_id, canonical_draft_id,
            session.owner_identity, body.expected_version)
        try:
            await glance_sessions.check_write_quota(request.app.state.pool, request.app.state.settings, session)
        except glance_sessions.GlanceAuthError as exc:
            return await _cache_write_error(request, claim, exc.status_code, exc.code, exc.message, exc.retry_after)
        generated = await handoff_drafts.generate_draft_content(
            request.app.state.ctx, session.salon_id, canonical_id, session.owner_identity,
            body.expected_version, instruction=instruction, previous_text=old["body"],
            timeout_seconds=handoff_drafts.GLANCE_MODEL_TIMEOUT_SECONDS)
        if generated is None:
            return await _cache_write_error(request, claim, 404, "case_not_found", "Case not found.")
        status, response_body = await glance_workflows.save_generated_draft(
            request.app.state.pool, session.salon_id, canonical_id, session.owner_identity,
            body.expected_version, generated, claim,
            lambda draft: _draft_response(draft, "ai"),
            old_draft_id=canonical_draft_id, instruction=instruction)
        return JSONResponse(response_body, status_code=status)
    except Exception as exc:
        mapped = _workflow_error(exc)
        if mapped:
            return await _cache_write_error(request, claim, *mapped)
        from .agent.client import AgentUnavailable
        if isinstance(exc, AgentUnavailable):
            return await _cache_write_error(request, claim, 503, "draft_unavailable", "Draft generation is temporarily unavailable.")
        log.warning("glance revision failed request_id=%s error=%s",
                    getattr(request.state, "glance_request_id", "req_unavailable"), exc.__class__.__name__)
        return await _cache_write_error(request, claim, 503, "draft_unavailable", "Draft generation is temporarily unavailable.")


@router.post("/cases/{case_id}/replies", status_code=202)
async def approve_reply(case_id: str, body: ReplyBody, request: Request,
                        idempotency_key: str | None = Header(default=None, alias="Idempotency-Key"),
                        session: glance_sessions.GlanceSession = Depends(require_session)):
    canonical_id = _canonical_uuid(case_id)
    try:
        draft_id = str(UUID(body.draft_id))
    except (TypeError, ValueError, AttributeError):
        raise GlanceAPIError("draft_not_found", 404, "Draft not found.")
    text = _normalize_text(body.text, 1000)
    try:
        key = glance_workflows.canonical_idempotency_key(idempotency_key)
    except glance_workflows.WorkflowError as exc:
        raise GlanceAPIError(exc.code, exc.status, exc.message, exc.retry_after)
    resource = f"{PREFIX}/cases/{canonical_id}/replies"
    claim, replay = await _begin_write(
        request, session, canonical_id, key,
        {"expected_version": body.expected_version, "draft_id": draft_id, "text": text}, resource)
    if replay is not None:
        return replay
    try:
        result = await handoff_outbox.approve_reply(
            request.app.state.pool, session.salon_id, canonical_id, session.owner_identity,
            body.expected_version, text, key, draft_id=draft_id, request_method="POST",
            resource_path=resource, session_id=session.id, device_label=session.device_label,
            idempotency_claim=claim)
    except handoff_outbox.IdempotencyConflict:
        return await _cache_write_error(request, claim, 409, "idempotency_conflict", "This key was already used for a different request.")
    except handoff_outbox.RequestInProgress:
        await glance_workflows.release_request(request.app.state.pool, claim)
        raise GlanceAPIError("request_in_progress", 409, "The request is still being processed.", 1)
    except handoff_outbox.UnresolvedReplyExists:
        return await _cache_write_error(request, claim, 409, "unresolved_reply_exists", "An outbound reply is already unresolved for this case.")
    except handoffs.LegacyUnresolvedReplyViolation:
        return await _cache_write_error(request, claim, 409, "multiple_unresolved_replies", "The reply state requires owner review.")
    except handoff_outbox.DraftExpired:
        return await _cache_write_error(request, claim, 409, "draft_expired", "This draft has expired.")
    except handoff_outbox.DraftConsumed:
        return await _cache_write_error(request, claim, 409, "draft_consumed", "This draft has already been approved.")
    except handoff_outbox.DraftSuperseded:
        return await _cache_write_error(request, claim, 409, "draft_superseded", "This draft has been superseded.")
    except handoff_outbox.DraftNotFound:
        return await _cache_write_error(request, claim, 404, "draft_not_found", "Draft not found.")
    except handoff_outbox.StaleCaseVersion:
        return await _cache_write_error(request, claim, 409, "stale_case_version", "The case or draft changed. Refresh it before approving.")
    except PermissionError:
        return await _cache_write_error(request, claim, 403, "owner_not_authorized", "Owner no longer controls this salon.")
    except ValueError:
        return await _cache_write_error(request, claim, 422, "invalid_text", "Reply text is invalid.")
    except Exception as exc:
        await glance_workflows.release_request(request.app.state.pool, claim)
        log.warning("glance approval failed request_id=%s error=%s",
                    getattr(request.state, "glance_request_id", "req_unavailable"), exc.__class__.__name__)
        return _safe_error(request, 503, "reply_unavailable", "The reply could not be queued.")
    if result is None:
        return await _cache_write_error(request, claim, 404, "case_not_found", "Case not found.")
    return JSONResponse(result["response_body"], status_code=result["response_status"])


@router.get("/replies/{reply_id}")
async def get_reply(reply_id: str, request: Request,
                    session: glance_sessions.GlanceSession = Depends(require_session)):
    try:
        canonical_reply_id = str(UUID(reply_id))
    except (TypeError, ValueError, AttributeError):
        raise GlanceAPIError("reply_not_found", 404, "Reply not found.")
    result = await handoff_outbox.get_reply_status(request.app.state.pool, session.salon_id, canonical_reply_id)
    if result is None:
        raise GlanceAPIError("reply_not_found", 404, "Reply not found.")
    for field in ("created_at", "updated_at", "submitted_at", "delivered_at", "failed_at"):
        result[field] = utc_timestamp(result[field])
    return {"data": result}


@router.post("/cases/{case_id}/resolve")
async def resolve_case(case_id: str, body: ResolveBody, request: Request,
                       session: glance_sessions.GlanceSession = Depends(require_session)):
    canonical_id = _canonical_uuid(case_id)
    note = _normalize_text(body.note, 200) if body.note is not None else None
    reason = body.reason + (f": {note}" if note else "")
    try:
        result = await handoffs.resolve_handoff(
            request.app.state.pool, session.salon_id, canonical_id, session.owner_identity, reason,
            expected_version=body.expected_version,
            acknowledge_unknown_reply=body.acknowledge_unknown_reply,
            session_id=session.id, device_label=session.device_label, reason_code=body.reason,
            acknowledgement_note=note)
    except handoffs.CaseHasSendingReply:
        raise GlanceAPIError("reply_still_sending", 409, "A reply is currently being submitted.")
    except handoffs.UnknownReplyNeedsAcknowledgement:
        raise GlanceAPIError("unknown_reply_requires_acknowledgement", 409,
                             "A reply may already have reached the customer. Explicitly acknowledge it before resolving.")
    except handoffs.NoUnknownReply:
        raise GlanceAPIError("no_unknown_reply", 422, "There is no ambiguous reply to acknowledge.")
    except handoffs.CaseVersionConflict:
        raise GlanceAPIError("stale_case_version", 409, "The case changed. Refresh it before resolving.")
    except handoffs.LegacyUnresolvedReplyViolation:
        raise GlanceAPIError("multiple_unresolved_replies", 409, "The reply state requires owner review.")
    except PermissionError:
        raise glance_sessions.GlanceAuthError("owner_not_authorized", 403, "Owner no longer controls this salon")
    if result is None:
        raise GlanceAPIError("case_not_found", 404, "Case not found.")
    return {"data": {
        "case_id": result["id"],
        "status": result["status"],
        "resolved_at": utc_timestamp(result.get("resolved_at")),
        "version": int(result["version"]),
        "unknown_reply_acknowledged": bool(result.get("unknown_reply_acknowledged")),
        "cancelled_reply_ids": result.get("cancelled_reply_ids", []),
    }}


def install_glance_api(app: FastAPI) -> None:
    app.include_router(router)
    app.add_middleware(GlanceTransportMiddleware)
    app.add_exception_handler(glance_sessions.GlanceAuthError, _handle_glance_auth_error)
    app.add_exception_handler(GlanceAPIError, _handle_glance_api_error)
    app.add_exception_handler(StarletteHTTPException, _handle_http_error)
    app.add_exception_handler(RequestValidationError, _handle_validation_error)
    app.add_exception_handler(Exception, _handle_unexpected_error)
