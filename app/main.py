import asyncio
import hashlib
import json
import logging
from contextlib import asynccontextmanager
from pathlib import Path
from uuid import UUID

import httpx
from fastapi import BackgroundTasks, FastAPI, Header, HTTPException, Request
from fastapi.responses import PlainTextResponse
from pydantic import BaseModel, Field, ValidationError

from . import hardening, logs
from .ai.budget import Budget
from .config import get_settings
from .ctx import Ctx
from .db import make_pool, migrate
from .hardening import safe_equals, startup_checks
from .inbound import inbound
from .ai.llm import Extractor
from .mpesa import Daraja, parse_callback
from .payments import run_sweeper
from .channels.telegram import Channels, Telegram, parse_update, run_poller
from .ai.vision import StyleMatcher
from .ai.voice import Transcriber
from .channels.whatsapp import Message, WhatsApp, explain, mask, parse_failures, parse_messages, verify_signature

from .agent import AgentModel
from .webhook_jobs import QueueCapacityError, enqueue_jobs, webhook_worker, list_dead_jobs, requeue_dead_job, purge_expired_data
from .metrics import observe_request, render as metrics_render, update_queue_depth

from .token_crypto import verify_stored_tokens

logs.setup()
log = logging.getLogger("rembo")
MAX_BODY = 256 * 1024
_OPS_FAIL_WINDOW = 60
_OPS_FAIL_LIMIT = 5
_MAX_BIGINT = 9223372036854775807
_OPS_FAILURES: dict[str, tuple[float, int]] = {}
_OPS_FAIL_MAX_KEYS = 4096


def _redis_label(url: str) -> str:
    """The Redis host for startup logs. The URL carries the password, which must never reach a log line."""
    from urllib.parse import urlsplit
    try:
        host = urlsplit(url or "").hostname
    except ValueError:
        host = None
    return host or "redis"


def _ops_note_fail(key: str, now: float) -> int:
    """Count one failed ops-key attempt for rate limiting. Old windows are pruned and the table is capped,
    so scanner traffic cannot grow it without bound."""
    if key not in _OPS_FAILURES and len(_OPS_FAILURES) >= _OPS_FAIL_MAX_KEYS:
        for old, (started, _) in list(_OPS_FAILURES.items()):
            if now - started >= _OPS_FAIL_WINDOW:
                _OPS_FAILURES.pop(old, None)
        while len(_OPS_FAILURES) >= _OPS_FAIL_MAX_KEYS:
            _OPS_FAILURES.pop(next(iter(_OPS_FAILURES)), None)
    started, count = _OPS_FAILURES.get(key, (now, 0))
    if now - started >= _OPS_FAIL_WINDOW:
        started, count = now, 0
    count += 1
    _OPS_FAILURES[key] = (started, count)
    return count
_OPS_INCR = """
local n = redis.call('INCR', KEYS[1])
if n == 1 then redis.call('EXPIRE', KEYS[1], ARGV[1]) end
return n
"""

async def _check_ops_key(ctx, provided: str | None, request: Request) -> None:
    expected = getattr(ctx.settings, "ops_api_key", "") or ""
    if not expected:
        raise HTTPException(404)
    if provided and safe_equals(provided, expected):
        return
    ip = (request.client.host if request.client else "unknown")
    key = f"ops_fail:{ip}"
    try:
        redis = getattr(ctx, "redis", None)
        if redis is not None:
            n = int(await redis.eval(_OPS_INCR, 1, key, _OPS_FAIL_WINDOW))
        else:
            import time as _clock
            n = _ops_note_fail(key, _clock.monotonic())
    except Exception:
        n = 1
    if n > _OPS_FAIL_LIMIT:
        raise HTTPException(429)
    raise HTTPException(401)

@asynccontextmanager
async def lifespan(app: FastAPI):
    s = get_settings()
    try:
        logs.setup(getattr(s, "log_level", "INFO"), getattr(s, "log_format", "text"), getattr(s, "log_file", ""))
    except Exception:
        pass
    startup_checks(s)
    pool = await make_pool(s.database_url, max_size=s.db_pool_max)
    for name in await migrate(pool):
        log.info("applied migration %s", name)
    await verify_stored_tokens(pool, s)
    http = httpx.AsyncClient(timeout=httpx.Timeout(15.0, connect=10.0))
    wa = WhatsApp(http, s.graph_version)
    app.state.settings, app.state.pool, app.state.wa = s, pool, wa
    redis_client = None
    try:
        from redis.asyncio import from_url as redis_from_url
        redis_client = redis_from_url(s.redis_url, decode_responses=False, socket_connect_timeout=2, socket_timeout=2)
        await redis_client.ping()
        log.info("redis connected %s", _redis_label(s.redis_url))
    except Exception as e:
        log.warning("redis unavailable (%s): using local fallback", e.__class__.__name__)
        if redis_client is not None:
            try:
                await redis_client.aclose()
            except Exception:
                pass
            redis_client = None
    budget = Budget(pool, s)
    llm = Extractor(http, s, budget, pool, redis_client)
    voice = Transcriber(http, s, budget, redis_client)
    tg = Telegram(http, s.telegram_bot_token) if s.telegram_bot_token else None
    agent_model = AgentModel(http, s, budget, llm.endpoints, pool) if getattr(s, "agent_mode", "off") == "on" else None
    app.state.ctx = Ctx(s, pool, Channels(wa, tg), llm, redis_client, Daraja(http, s), voice, StyleMatcher(http, s, llm.endpoints, budget), agent_model)
    if agent_model is not None:
        log.info("agent mode ON: the model drives the receptionist through tools (protocol %s)", s.agent_protocol)
    app.state.redis = redis_client
    app.state.tg = tg
    poller = None
    if tg:
        mode = s.telegram_mode if s.telegram_mode != "auto" else ("webhook" if s.public_base_url.startswith("https://") else "poll")
        if mode == "webhook":
            if not s.telegram_webhook_secret or not await tg.set_webhook(f"{s.public_base_url.rstrip('/')}/telegram/webhook", s.telegram_webhook_secret):
                log.error("Telegram webhook was not set")
        elif mode == "poll":
            poller = asyncio.create_task(run_poller(app.state.ctx, tg, inbound))
    if not app.state.ctx.daraja.configured:
        log.warning("M-Pesa is not fully configured")
    sweeper = asyncio.create_task(run_sweeper(app.state.ctx))
    workers = []
    for _ in range(max(1, int(getattr(s, "webhook_workers", 2)))):
        workers.append(asyncio.create_task(webhook_worker(app.state.ctx)))
    retention_task = asyncio.create_task(_retention_loop(app.state.ctx))
    reminder_task = asyncio.create_task(_reminder_loop(app.state.ctx))
    yield
    for t in workers + [sweeper, poller, retention_task, reminder_task]:
        if t:
            t.cancel()
    await asyncio.gather(*(t for t in workers + [sweeper, poller, retention_task, reminder_task] if t), return_exceptions=True)
    await http.aclose()
    if redis_client is not None:
        try:
            await redis_client.aclose()
        except Exception:
            pass
    await pool.close()

async def _reminder_loop(ctx):
    while True:
        try:
            await asyncio.sleep(60)
            if getattr(ctx.settings, 'reminder_enabled', True):
                from .reminders import run_reminders
                await run_reminders(ctx)
                try:
                    from .waitlist import sweep_expired
                    await sweep_expired(ctx)
                except Exception:
                    log.exception('waitlist sweep failed')
                try:
                    from .reminders import release_silent
                    await release_silent(ctx)
                except Exception:
                    log.exception('silent release failed')
        except asyncio.CancelledError:
            raise
        except Exception:
            log.exception('reminder loop failed')

async def _retention_loop(ctx):
    while True:
        try:
            await asyncio.sleep(24*3600)
            await purge_expired_data(
                ctx.pool,
                webhook_days=ctx.settings.webhook_retention_days,
                inbound_days=ctx.settings.inbound_retention_days,
                agent_days=ctx.settings.agent_retention_days,
                handoff_days=ctx.settings.handoff_retention_days,
                callback_days=ctx.settings.mpesa_callback_retention_days,
                unmatched_raw_days=ctx.settings.unmatched_raw_retention_days,
            )
            try:
                from .lifecycle import sweep
                await sweep(ctx)
            except Exception:
                log.exception("lifecycle sweep failed")
            try:
                from .reminders import run_reminders
                await run_reminders(ctx)
            except Exception:
                log.exception("reminders failed in retention loop")
        except asyncio.CancelledError:
            raise
        except Exception:
            log.exception("retention purge failed")

app = FastAPI(lifespan=lifespan, docs_url=None, redoc_url=None, openapi_url=None)
hardening.install(app)

_METRICS_EXACT = {"/health", "/ready", "/metrics", "/webhook", "/telegram/webhook",
                  "/ops/payment-reviews", "/ops/unmatched-payments", "/ops/webhook-jobs/dead"}
_METRICS_PREFIXES = (("/mpesa/callback/", "/mpesa/callback/{secret}"),
                     ("/ops/webhook-jobs/", "/ops/webhook-jobs/{job_id}"),
                     ("/ops/payment-reviews/", "/ops/payment-reviews/{review_id}"),
                     ("/ops/unmatched-payments/", "/ops/unmatched-payments/{payment_id}"))

def route_name(path: str) -> str:
    """Templated route for metrics labels. Raw paths must never become labels: /mpesa/callback/<secret>
    carries a secret, and anything else unlisted is scanner noise."""
    if path in _METRICS_EXACT:
        return path
    for prefix, name in _METRICS_PREFIXES:
        if path.startswith(prefix):
            return name
    return "other"

@app.middleware("http")
async def metrics_middleware(request: Request, call_next):
    import time as _t
    start = _t.time()
    response = await call_next(request)
    try:
        observe_request(request.method, route_name(request.url.path), response.status_code, _t.time() - start)
    except Exception:
        pass
    return response
@app.get("/health")
async def health():
    return {"ok": True}

@app.get("/model-info")
async def model_info(request: Request, x_ops_key: str | None = Header(default=None)):
    """Which model backend is active, for open-weights prize verification. Gated like the other ops endpoints."""
    ctx = request.app.state.ctx
    await _check_ops_key(ctx, x_ops_key, request)
    s = ctx.settings
    return {"backend": getattr(s, "llm_backend", "primary"), "model": s.llm_model,
            "fallback_backend": getattr(s, "llm_fallback_backend", "") or None, "fallback_model": s.llm_fallback_model or None,
            "quantization": getattr(s, "llm_quantization", "") or None, "host": getattr(s, "llm_host", "") or None,
            "agent_mode": getattr(s, "agent_mode", "off"), "agent_protocol": getattr(s, "agent_protocol", "auto")}

@app.get("/ready")
async def ready(request: Request):
    try:
        await request.app.state.pool.fetchval("SELECT 1")
    except Exception:
        raise HTTPException(503)
    return {"ok": True}

@app.get("/webhook")
async def verify(request: Request):
    q = request.query_params
    expected = request.app.state.settings.wa_verify_token
    if expected and q.get("hub.mode") == "subscribe" and safe_equals(q.get("hub.verify_token"), expected):
        return PlainTextResponse(q.get("hub.challenge", ""))
    raise HTTPException(403)

async def _drain(ctx):
    from .webhook_jobs import claim_next, run_claimed
    while True:
        job = await claim_next(ctx.pool)
        if job is None:
            break
        await run_claimed(ctx, job)

@app.post("/webhook")
async def receive(request: Request, bg: BackgroundTasks):
    raw = await request.body()
    if len(raw) > MAX_BODY:
        raise HTTPException(413)
    secret = request.app.state.settings.wa_app_secret
    if not secret or not verify_signature(raw, request.headers.get("x-hub-signature-256"), secret):
        raise HTTPException(403)
    try:
        payload = json.loads(raw)
    except ValueError:
        raise HTTPException(400)
    for f in parse_failures(payload):
        log.error("DELIVERY FAILED to %s code=%s %s | %s", mask(f.to), f.code, f.title, explain(f.code))
    events = []
    for msg in parse_messages(payload):
        events.append((msg.id, {"id": msg.id, "sender": msg.sender, "phone_number_id": msg.phone_number_id, "kind": msg.kind, "text": msg.text, "media_id": msg.media_id, "name": msg.name, "reply_id": msg.reply_id, "wa_id": msg.sender}))
    if events:
        try:
            await enqueue_jobs(request.app.state.pool, "whatsapp", events, max_pending=request.app.state.settings.webhook_max_pending, max_pending_per_sender=request.app.state.settings.webhook_max_pending_per_sender)
        except QueueCapacityError:
            raise HTTPException(429)
        except Exception:
            log.exception("enqueue whatsapp failed")
            raise HTTPException(500)
        bg.add_task(_drain, request.app.state.ctx)
    return {"status": "ok"}

@app.post("/mpesa/callback/{secret}")
async def mpesa_callback(secret: str, request: Request, bg: BackgroundTasks):
    expected = request.app.state.settings.mpesa_callback_secret
    if not expected or not safe_equals(secret, expected):
        raise HTTPException(404)
    raw = await request.body()
    if len(raw) > 100_000:
        raise HTTPException(413)
    try:
        payload = json.loads(raw)
    except ValueError:
        payload = None
    if isinstance(payload, dict):
        out = parse_callback(payload)
        if out is not None:
            try:
                await request.app.state.pool.execute(
                    "INSERT INTO mpesa_callbacks (checkout_request_id, body) VALUES ($1, $2) ON CONFLICT DO NOTHING",
                    out.checkout_request_id, payload)
            except Exception:
                pass
        event_id = out.checkout_request_id if out and out.checkout_request_id else hashlib.sha256(raw).hexdigest()
        try:
            await enqueue_jobs(request.app.state.pool, "mpesa", [(event_id, payload)], max_pending=request.app.state.settings.webhook_max_pending, max_pending_per_sender=request.app.state.settings.webhook_max_pending_per_sender)
        except QueueCapacityError:
            raise HTTPException(429)
        except Exception:
            log.exception("enqueue mpesa failed")
            raise HTTPException(500)
        bg.add_task(_drain, request.app.state.ctx)
    return {"ResultCode": 0, "ResultDesc": "Accepted"}

@app.post("/telegram/webhook")
async def telegram_webhook(request: Request, bg: BackgroundTasks):
    secret = request.app.state.settings.telegram_webhook_secret
    sent = request.headers.get("x-telegram-bot-api-secret-token", "")
    tg = request.app.state.tg
    if tg is None or not secret or not safe_equals(sent, secret):
        raise HTTPException(404)
    raw = await request.body()
    if len(raw) > MAX_BODY:
        raise HTTPException(413)
    try:
        update = json.loads(raw)
    except ValueError:
        raise HTTPException(400)
    msg, cb = parse_update(update if isinstance(update, dict) else {}, tg.bot_key)
    if cb:
        bg.add_task(tg.answer_callback, cb)
    if msg:
        events = [(msg.id, {"id": msg.id, "sender": msg.sender, "phone_number_id": msg.phone_number_id, "kind": msg.kind, "text": msg.text, "media_id": msg.media_id, "name": msg.name, "reply_id": msg.reply_id})]
        try:
            await enqueue_jobs(request.app.state.pool, "telegram", events, max_pending=request.app.state.settings.webhook_max_pending, max_pending_per_sender=request.app.state.settings.webhook_max_pending_per_sender)
        except QueueCapacityError:
            raise HTTPException(429)
        bg.add_task(_drain, request.app.state.ctx)
    return {"ok": True}

@app.get("/ops/agent-trace")
async def agent_trace(request: Request, limit: int = 20, x_ops_key: str | None = Header(default=None)):
    """The last agent turns: what the customer wrote, which tools the model called, what code answered, and what was sent."""
    await _check_ops_key(request.app.state.ctx, x_ops_key, request)
    rows = await request.app.state.pool.fetch(
        "SELECT created_at, sender, user_text, steps, final_text, outcome, grounded, problems, backend, model, model_calls, ms "
        "FROM agent_turns ORDER BY id DESC LIMIT $1", max(1, min(int(limit), 100)))
    def _j(v):
        return v if isinstance(v, (list, dict)) or v is None else json.loads(v)
    return [{"at": r["created_at"].isoformat(), "customer": r["sender"], "said": r["user_text"], "tools": _j(r["steps"]), "sent": r["final_text"],
             "outcome": r["outcome"], "grounded": r["grounded"], "rejected_for": _j(r["problems"]), "backend": r["backend"], "model": r["model"],
             "model_calls": r["model_calls"], "ms": r["ms"]} for r in rows]


@app.get("/ops/conversation-trace")
async def conversation_trace(request: Request, limit: int = 50, thread: str | None = None,
                             x_ops_key: str | None = Header(default=None)):
    """Recent full chat turns: sanitized inbound text, every attempted reply, and any model/tool evidence."""
    await _check_ops_key(request.app.state.ctx, x_ops_key, request)
    limit = max(1, min(int(limit), 100))
    if thread:
        rows = await request.app.state.pool.fetch(
            "SELECT c.message_id, c.thread, c.channel, c.message_kind, c.salon_id::text AS salon_id, c.user_text, c.created_at, "
            "a.steps, a.final_text, a.outcome, a.grounded, a.problems, a.backend, a.model, a.model_calls, a.ms, a.user_text AS agent_user_text "
            "FROM conversation_trace c LEFT JOIN agent_turns a ON a.message_id=c.message_id "
            "WHERE c.thread=$2 ORDER BY c.created_at DESC LIMIT $1", limit, thread)
    else:
        rows = await request.app.state.pool.fetch(
            "SELECT c.message_id, c.thread, c.channel, c.message_kind, c.salon_id::text AS salon_id, c.user_text, c.created_at, "
            "a.steps, a.final_text, a.outcome, a.grounded, a.problems, a.backend, a.model, a.model_calls, a.ms, a.user_text AS agent_user_text "
            "FROM conversation_trace c LEFT JOIN agent_turns a ON a.message_id=c.message_id "
            "ORDER BY c.created_at DESC LIMIT $1", limit)
    ids = [r["message_id"] for r in rows]
    replies = await request.app.state.pool.fetch(
        "SELECT message_id, sequence, message_kind, body, buttons, send_ok, created_at "
        "FROM conversation_trace_replies WHERE message_id=ANY($1::text[]) ORDER BY message_id, sequence", ids) if ids else []

    def _j(value):
        return value if isinstance(value, (list, dict)) or value is None else json.loads(value)

    by_turn: dict[str, list[dict]] = {}
    for reply in replies:
        by_turn.setdefault(reply["message_id"], []).append({
            "sequence": reply["sequence"], "kind": reply["message_kind"], "text": reply["body"],
            "buttons": _j(reply["buttons"]), "send_ok": reply["send_ok"], "at": reply["created_at"].isoformat(),
        })
    items = []
    for r in reversed(rows):
        has_agent = r["outcome"] is not None
        items.append({
            "at": r["created_at"].isoformat(), "message_id": r["message_id"], "thread": r["thread"],
            "channel": r["channel"], "salon_id": r["salon_id"], "kind": r["message_kind"],
            "said": r["agent_user_text"] or r["user_text"], "replies": by_turn.get(r["message_id"], []),
            "agent": ({"outcome": r["outcome"], "grounded": r["grounded"], "rejected_for": _j(r["problems"]),
                       "backend": r["backend"], "model": r["model"], "model_calls": r["model_calls"],
                       "ms": r["ms"], "steps": _j(r["steps"]), "proposed_reply": r["final_text"]}
                      if has_agent else None),
        })
    return {"items": items}

@app.get("/ops/payment-reviews")
async def payment_reviews(request: Request, x_ops_key: str | None = Header(default=None)):
    ctx = request.app.state.ctx
    await _check_ops_key(ctx, x_ops_key, request)
    rows = await ctx.pool.fetch("SELECT * FROM payment_reviews WHERE status IN ('pending_review','refund_due') ORDER BY created_at")
    return {"items": [dict(r) for r in rows]}

@app.get("/ops/unmatched-payments")
async def unmatched_payments(request: Request, x_ops_key: str | None = Header(default=None)):
    ctx = request.app.state.ctx
    await _check_ops_key(ctx, x_ops_key, request)
    rows = await ctx.pool.fetch("SELECT * FROM unmatched_payments WHERE status='open' ORDER BY created_at DESC")
    return {"items": [dict(r) for r in rows]}

class UnmatchedResolveBody(BaseModel):
    resolution_note: str | None = None

@app.post("/ops/unmatched-payments/{payment_id}/resolve")
async def resolve_unmatched(payment_id: str, body: UnmatchedResolveBody, request: Request, x_ops_key: str | None = Header(default=None)):
    """Close an unmatched payment once the money has been dealt with (refunded or matched by hand)."""
    ctx = request.app.state.ctx
    await _check_ops_key(ctx, x_ops_key, request)
    try:
        uid = UUID(payment_id)
    except ValueError:
        raise HTTPException(422, "Invalid unmatched payment ID")
    row = await ctx.pool.fetchrow("SELECT status FROM unmatched_payments WHERE id=$1::uuid", str(uid))
    if row is None:
        raise HTTPException(404, "Unmatched payment not found")
    if row["status"] != "open":
        raise HTTPException(409, "Unmatched payment has already been resolved")
    changed = await ctx.pool.execute(
        "UPDATE unmatched_payments SET status='resolved', resolution_note=$2, resolved_at=now() "
        "WHERE id=$1::uuid AND status='open'", str(uid), body.resolution_note)
    if not changed.endswith("1"):
        raise HTTPException(409, "Unmatched payment has already been resolved")
    return {"id": str(uid), "status": "resolved"}

class ResolveBody(BaseModel):
    action: str = Field(pattern="^(refunded|closed_no_refund)$")
    resolution_reference: str | None = None
    resolution_note: str | None = None

@app.post("/ops/payment-reviews/{review_id}/resolve")
async def resolve_review(review_id: str, body: ResolveBody, request: Request, x_ops_key: str | None = Header(default=None)):
    ctx = request.app.state.ctx
    await _check_ops_key(ctx, x_ops_key, request)
    try:
        rid = UUID(review_id)
    except ValueError:
        raise HTTPException(422, "Invalid payment review ID")
    async with ctx.pool.acquire() as conn:
        async with conn.transaction():
            row = await conn.fetchrow("SELECT payment_id, status FROM payment_reviews WHERE id=$1::uuid FOR UPDATE", str(rid))
            if row is None:
                raise HTTPException(404, "Payment review not found")
            if row["status"] not in ("pending_review", "refund_due"):
                raise HTTPException(409, "Payment review has already been resolved")
            changed = await conn.execute(
                "UPDATE payment_reviews SET status=$2, resolution_reference=$3, resolution_note=$4, "
                "resolved_at=now(), updated_at=now() WHERE id=$1::uuid AND status IN ('pending_review','refund_due')",
                str(rid), body.action, body.resolution_reference, body.resolution_note)
            if not changed.endswith("1"):
                raise HTTPException(409, "Payment review has already been resolved")
            if body.action == "refunded":
                await conn.execute("UPDATE payments SET status='refunded', updated_at=now() WHERE id=$1::uuid", row["payment_id"])
    return {"id": str(rid), "status": body.action}

@app.get("/ops/webhook-jobs/dead")
async def dead_jobs(request: Request, x_ops_key: str | None = Header(default=None)):
    ctx = request.app.state.ctx
    await _check_ops_key(ctx, x_ops_key, request)
    return {"items": await list_dead_jobs(ctx.pool)}

@app.post("/ops/webhook-jobs/{job_id}/retry")
async def retry_job(request: Request, job_id: str, x_ops_key: str | None = Header(default=None)):
    ctx = request.app.state.ctx
    await _check_ops_key(ctx, x_ops_key, request)
    if not job_id.isascii() or not job_id.isdecimal() or len(job_id) > 19:
        raise HTTPException(422, "Invalid webhook job ID")
    jid = int(job_id)
    if not 1 <= jid <= _MAX_BIGINT:
        raise HTTPException(422, "Invalid webhook job ID")
    if not await requeue_dead_job(ctx.pool, jid):
        raise HTTPException(404, "Dead webhook job not found")
    return {"id": jid, "status": "pending"}

@app.get("/metrics")
async def metrics(request: Request, x_ops_key: str | None = Header(default=None)):
    ctx = getattr(request.app.state, "ctx", None)
    if ctx is None:
        raise HTTPException(404)
    if not getattr(ctx.settings, "metrics_enabled", True):
        raise HTTPException(404)
    client_ip = str(request.client.host if request.client else "unknown")
    if client_ip not in ("127.0.0.1", "::1"):
        await _check_ops_key(ctx, x_ops_key, request)
    await update_queue_depth(ctx.pool)
    data, ctype = metrics_render()
    from fastapi.responses import Response
    return Response(content=data, media_type=ctype)
