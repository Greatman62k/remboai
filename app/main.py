import asyncio
import hashlib
import json
import logging
from contextlib import asynccontextmanager
from pathlib import Path
from uuid import UUID

import httpx
from fastapi import BackgroundTasks, FastAPI, Header, HTTPException, Request
from fastapi.responses import FileResponse, PlainTextResponse
from pydantic import BaseModel, Field, ValidationError

from . import hardening, logs
from .budget import Budget
from .config import get_settings
from .ctx import Ctx
from .db import make_pool, migrate
from .hardening import safe_equals, startup_checks
from .inbound import inbound
from .llm import Extractor
from .mpesa import Daraja
from .payments import process_callback, run_sweeper
from .telegram import Channels, Telegram, parse_update, run_poller
from .vision import StyleMatcher
from .voice import Transcriber
from .whatsapp import Message, WhatsApp, explain, mask, parse_failures, parse_messages, verify_signature
from .ownerapi import router as owner_api
from .webhook_jobs import QueueCapacityError, enqueue_jobs, webhook_worker, list_dead_jobs, requeue_dead_job, purge_expired_data
from .onboarding import verify_onboarding_schema, create_setup_invite, get_setup_invite, csrf_for_token, parse_setup_form, complete_setup_invite, setup_headers, setup_form, setup_complete_page, setup_unavailable_page, InviteConflict, InviteUnavailable, OnboardingError
from .token_crypto import verify_stored_tokens

logs.setup()
log = logging.getLogger("rembo")
MAX_BODY = 256 * 1024
_OPS_FAIL_WINDOW = 60
_OPS_FAIL_LIMIT = 5
_MAX_BIGINT = 9223372036854775807
_OPS_FAILURES: dict[str, tuple[float, int]] = {}
_OPS_INCR = """
local n = redis.call('INCR', KEYS[1])
if n == 1 then redis.call('EXPIRE', KEYS[1], ARGV[1]) end
return n
"""

class OpsInviteRequest(BaseModel):
    wa_phone_number_id: str = Field(min_length=1, max_length=128)
    wa_business_number: str = Field(min_length=7, max_length=32)
    expires_hours: int | None = Field(default=None, ge=1, le=30*24)

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
            now = asyncio.get_running_loop().time()
            start, cnt = _OPS_FAILURES.get(key, (now, 0))
            if now - start >= _OPS_FAIL_WINDOW:
                start, cnt = now, 0
            cnt += 1
            _OPS_FAILURES[key] = (start, cnt)
            n = cnt
    except Exception:
        n = 1
    if n > _OPS_FAIL_LIMIT:
        raise HTTPException(429)
    raise HTTPException(401)

@asynccontextmanager
async def lifespan(app: FastAPI):
    s = get_settings()
    startup_checks(s)
    pool = await make_pool(s.database_url, max_size=s.db_pool_max)
    for name in await migrate(pool):
        log.info("applied migration %s", name)
    await verify_onboarding_schema(pool)
    await verify_stored_tokens(pool, s)
    http = httpx.AsyncClient(timeout=httpx.Timeout(10.0, connect=3.0))
    wa = WhatsApp(http, s.graph_version)
    app.state.settings, app.state.pool, app.state.wa = s, pool, wa
    redis_client = None
    try:
        from redis.asyncio import from_url as redis_from_url
        redis_client = redis_from_url(s.redis_url, decode_responses=False, socket_connect_timeout=2, socket_timeout=2)
        await redis_client.ping()
        log.info("redis connected %s", s.redis_url)
    except Exception as e:
        log.warning("redis unavailable (%s): using local fallback", e.__class__.__name__)
        if redis_client is not None:
            try:
                await redis_client.aclose()
            except Exception:
                pass
            redis_client = None
    budget = Budget(pool, s)
    llm = Extractor(http, s, budget)
    tg = Telegram(http, s.telegram_bot_token) if s.telegram_bot_token else None
    app.state.ctx = Ctx(s, pool, Channels(wa, tg), llm, redis_client, Daraja(http, s), Transcriber(http, s, budget), StyleMatcher(http, s, llm.endpoints, budget))
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
    yield
    for t in workers + [sweeper, poller, retention_task]:
        if t:
            t.cancel()
    await asyncio.gather(*(t for t in workers + [sweeper, poller, retention_task] if t), return_exceptions=True)
    await http.aclose()
    if redis_client is not None:
        try:
            await redis_client.aclose()
        except Exception:
            pass
    await pool.close()

async def _retention_loop(ctx):
    while True:
        try:
            await asyncio.sleep(24*3600)
            await purge_expired_data(ctx.pool, webhook_days=ctx.settings.webhook_retention_days, inbound_days=ctx.settings.inbound_retention_days)
        except asyncio.CancelledError:
            raise
        except Exception:
            log.exception("retention purge failed")

app = FastAPI(lifespan=lifespan, docs_url=None, redoc_url=None, openapi_url=None)
hardening.install(app)
app.include_router(owner_api)
WEB = Path(__file__).parent / "web"
PAGE_HEADERS = {"Content-Security-Policy": "default-src 'none'; script-src 'self'; style-src 'self'; connect-src 'self'; img-src 'self'; base-uri 'none'; form-action 'none'; frame-ancestors 'none'", "Cache-Control": "no-store", "Referrer-Policy": "no-referrer", "X-Content-Type-Options": "nosniff", "X-Frame-Options": "DENY"}

@app.get("/owner")
@app.get("/owner/login/{token}")
async def owner_page(token: str = ""):
    return FileResponse(WEB / "owner.html", headers=PAGE_HEADERS)

@app.get("/owner/static/{name}")
async def owner_static(name: str):
    files = {"owner.js": "text/javascript", "owner.css": "text/css"}
    ctype = files.get(name)
    if ctype is None:
        raise HTTPException(404)
    return FileResponse(WEB / name, media_type=ctype, headers=PAGE_HEADERS)

@app.get("/health")
async def health():
    return {"ok": True}

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
    if expected and q.get("hub.mode") == "subscribe" and q.get("hub.verify_token") == expected:
        return PlainTextResponse(q.get("hub.challenge", ""))
    raise HTTPException(403)

async def handle(app: FastAPI, msg: Message) -> None:
    await inbound(app.state.ctx, msg)

async def _drain(ctx):
    from .webhook_jobs import claim_next, process_job
    while True:
        job = await claim_next(ctx.pool)
        if job is None:
            break
        try:
            await process_job(ctx, job)
        except Exception:
            from .webhook_jobs import retry_job
            await retry_job(ctx.pool, job, Exception("drain failed"))

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
        bg.add_task(handle, request.app, msg)
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
        try:
            from app.mpesa import parse_callback
            out = parse_callback(payload)
            await request.app.state.pool.execute("INSERT INTO mpesa_callbacks (checkout_request_id, body) VALUES ($1, $2)", out.checkout_request_id if out else None, payload)
        except Exception:
            pass
        event_id = hashlib.sha256(raw).hexdigest()
        try:
            await enqueue_jobs(request.app.state.pool, "mpesa", [(event_id, payload)], max_pending=request.app.state.settings.webhook_max_pending, max_pending_per_sender=request.app.state.settings.webhook_max_pending_per_sender)
        except QueueCapacityError:
            raise HTTPException(429)
        except Exception:
            log.exception("enqueue mpesa failed")
            raise HTTPException(500)
        await _drain(request.app.state.ctx)
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
        bg.add_task(handle, request.app, msg)
        bg.add_task(_drain, request.app.state.ctx)
    return {"ok": True}

@app.get("/ops/payment-reviews")
async def payment_reviews(request: Request, x_ops_key: str | None = Header(default=None)):
    ctx = request.app.state.ctx
    await _check_ops_key(ctx, x_ops_key, request)
    rows = await ctx.pool.fetch("SELECT * FROM payment_reviews WHERE status IN ('pending_review','refund_due') ORDER BY created_at")
    return {"items": [dict(r) for r in rows]}

@app.post("/ops/salon-setup-invites")
async def create_invite(body: OpsInviteRequest, request: Request, x_ops_key: str | None = Header(default=None)):
    ctx = request.app.state.ctx
    await _check_ops_key(ctx, x_ops_key, request)
    try:
        token, invite = await create_setup_invite(ctx.pool, body.wa_phone_number_id, body.wa_business_number, body.expires_hours or ctx.settings.salon_setup_invite_hours if hasattr(ctx.settings, 'salon_setup_invite_hours') else 168)
    except InviteConflict as e:
        raise HTTPException(409, str(e))
    except OnboardingError as e:
        raise HTTPException(422, str(e))
    base = ctx.settings.public_base_url.rstrip('/') if ctx.settings.public_base_url else ''
    url = f"{base}/setup/{token}" if base else f"/setup/{token}"
    return {"token": token, "url": url, "expires_at": invite.expires_at.isoformat()}

@app.get("/ops/unmatched-payments")
async def unmatched_payments(request: Request, x_ops_key: str | None = Header(default=None)):
    ctx = request.app.state.ctx
    await _check_ops_key(ctx, x_ops_key, request)
    rows = await ctx.pool.fetch("SELECT * FROM unmatched_payments WHERE status='open' ORDER BY created_at DESC")
    return {"items": [dict(r) for r in rows]}

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
    row = await ctx.pool.fetchrow("SELECT * FROM payment_reviews WHERE id=$1::uuid", str(rid))
    if row is None:
        raise HTTPException(404, "Payment review not found")
    if row["status"] not in ("pending_review", "refund_due"):
        raise HTTPException(409, "Payment review has already been resolved")
    await ctx.pool.execute("UPDATE payment_reviews SET status=$2, resolution_reference=$3, resolution_note=$4, resolved_at=now(), updated_at=now() WHERE id=$1::uuid", str(rid), body.action, body.resolution_reference, body.resolution_note)
    if body.action == "refunded":
        await ctx.pool.execute("UPDATE payments SET status='refunded', updated_at=now() WHERE id=$1::uuid", row["payment_id"])
    return {"id": str(rid), "status": body.action}

@app.get("/ops/webhook-jobs/dead")
async def dead_jobs(request: Request, x_ops_key: str | None = Header(default=None)):
    ctx = request.app.state.ctx
    await _check_ops_key(ctx, x_ops_key, request)
    return {"items": await list_dead_jobs(ctx.pool)}

@app.get("/setup/{token}")
async def setup_page(token: str, request: Request):
    ctx = request.app.state.ctx
    invite = await get_setup_invite(ctx.pool, token)
    if invite is None:
        from fastapi.responses import HTMLResponse
        return HTMLResponse(setup_unavailable_page(), status_code=404, headers=setup_headers())
    from fastapi.responses import HTMLResponse
    return HTMLResponse(setup_form(token, invite.wa_business_number), headers=setup_headers())

@app.post("/setup/{token}")
async def setup_submit(token: str, request: Request):
    ctx = request.app.state.ctx
    invite = await get_setup_invite(ctx.pool, token)
    if invite is None:
        from fastapi.responses import HTMLResponse
        return HTMLResponse(setup_unavailable_page(), status_code=404, headers=setup_headers())
    form_data = await request.form()
    form = dict(form_data)
    if form.get("csrf") != csrf_for_token(token):
        raise HTTPException(403, "Invalid CSRF token")
    try:
        setup = parse_setup_form(form, invite.wa_business_number)
    except OnboardingError as e:
        from fastapi.responses import HTMLResponse
        return HTMLResponse(setup_form(token, invite.wa_business_number, error=str(e), values=form), status_code=422, headers=setup_headers())
    try:
        salon = await complete_setup_invite(ctx.pool, token, setup)
    except InviteConflict as e:
        from fastapi.responses import HTMLResponse
        return HTMLResponse(setup_form(token, invite.wa_business_number, error=str(e), values=form), status_code=409, headers=setup_headers())
    except OnboardingError as e:
        from fastapi.responses import HTMLResponse
        if not await get_setup_invite(ctx.pool, token):
            return HTMLResponse(setup_unavailable_page(), status_code=404, headers=setup_headers())
        return HTMLResponse(setup_form(token, invite.wa_business_number, error=str(e), values=form), status_code=422, headers=setup_headers())
    from fastapi.responses import HTMLResponse
    return HTMLResponse(setup_complete_page(salon), headers=setup_headers())

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
