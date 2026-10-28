import asyncio
import hmac
import json
import logging
from contextlib import asynccontextmanager

import httpx
from fastapi import BackgroundTasks, FastAPI, HTTPException, Request
from pathlib import Path

from fastapi.responses import FileResponse, PlainTextResponse

from .config import get_settings
from .inbound import inbound
from .ctx import Ctx
from .llm import Extractor
from .mpesa import Daraja
from .telegram import Channels, Telegram, parse_update, run_poller
from .vision import StyleMatcher
from .voice import Transcriber
from .ownerapi import router as owner_api
from .payments import process_callback, run_sweeper
from .db import make_pool, migrate
from .whatsapp import Message, WhatsApp, explain, mask, parse_failures, parse_messages, verify_signature

logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(name)s %(message)s")
log = logging.getLogger("rembo")
MAX_BODY = 1_000_000


@asynccontextmanager
async def lifespan(app: FastAPI):
    s = get_settings()
    pool = await make_pool(s.database_url)
    for name in await migrate(pool):
        log.info("applied migration %s", name)
    http = httpx.AsyncClient(timeout=httpx.Timeout(10.0, connect=3.0))
    wa = WhatsApp(http, s.graph_version)
    app.state.settings, app.state.pool, app.state.wa = s, pool, wa
    llm = Extractor(http, s)
    tg = Telegram(http, s.telegram_bot_token) if s.telegram_bot_token else None
    app.state.ctx = Ctx(s, pool, Channels(wa, tg), llm, Daraja(http, s), Transcriber(http, s), StyleMatcher(http, s, llm.endpoints))
    app.state.tg = tg
    poller = None
    if tg:
        mode = s.telegram_mode if s.telegram_mode != "auto" else ("webhook" if s.public_base_url.startswith("https://") else "poll")
        if mode == "webhook":
            if not s.telegram_webhook_secret or not await tg.set_webhook(f"{s.public_base_url.rstrip('/')}/telegram/webhook", s.telegram_webhook_secret):
                log.error("Telegram webhook was not set (needs PUBLIC_BASE_URL and TELEGRAM_WEBHOOK_SECRET)")
        elif mode == "poll":
            poller = asyncio.create_task(run_poller(app.state.ctx, tg, inbound))
    if not app.state.ctx.daraja.configured:
        log.warning("M-Pesa is not fully configured: deposits cannot be requested")
    sweeper = asyncio.create_task(run_sweeper(app.state.ctx))
    yield
    for task in (sweeper, poller):
        if task:
            task.cancel()
    await asyncio.gather(*(t for t in (sweeper, poller) if t), return_exceptions=True)
    await http.aclose()
    await pool.close()


app = FastAPI(lifespan=lifespan, docs_url=None, redoc_url=None, openapi_url=None)
app.include_router(owner_api)

WEB = Path(__file__).parent / "web"
# The owner page loads only its own files and talks only to its own API; the sign-in token never leaves in a Referer.
PAGE_HEADERS = {
    "Content-Security-Policy": "default-src 'none'; script-src 'self'; style-src 'self'; connect-src 'self'; img-src 'self'; base-uri 'none'; form-action 'none'; frame-ancestors 'none'",
    "Cache-Control": "no-store", "Referrer-Policy": "no-referrer", "X-Content-Type-Options": "nosniff", "X-Frame-Options": "DENY",
}


@app.get("/owner")
@app.get("/owner/login/{token}")
async def owner_page(token: str = ""):
    return FileResponse(WEB / "owner.html", headers=PAGE_HEADERS)


@app.get("/owner/static/{name}")
async def owner_static(name: str):
    files = {"owner.js": "text/javascript", "owner.css": "text/css"}
    if name not in files:
        raise HTTPException(404)
    return FileResponse(WEB / name, media_type=files[name], headers=PAGE_HEADERS)


@app.get("/health")
async def health():
    return {"ok": True}


@app.get("/ready")
async def ready(request: Request):
    """Healthy means the database answers; /health only says the process is up."""
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
    """Runs after the 200 has gone back to the channel."""
    await inbound(app.state.ctx, msg)


@app.post("/webhook")
async def receive(request: Request, bg: BackgroundTasks):
    raw = await request.body()
    if len(raw) > MAX_BODY:
        raise HTTPException(413)
    secret = request.app.state.settings.wa_app_secret
    if not secret or not verify_signature(raw, request.headers.get("x-hub-signature-256"), secret):   # an empty secret would make signatures forgeable
        raise HTTPException(403)
    try:
        payload = json.loads(raw)
    except ValueError:
        raise HTTPException(400)
    for f in parse_failures(payload):
        log.error("DELIVERY FAILED to %s code=%s %s | %s", mask(f.to), f.code, f.title, explain(f.code))
    for msg in parse_messages(payload):
        bg.add_task(handle, request.app, msg)
    return {"status": "ok"}


async def callback_task(ctx, payload: dict) -> None:
    try:
        await process_callback(ctx, payload)
    except Exception:
        log.exception("failed processing M-Pesa callback")  # the sweeper's status query will still settle it


@app.post("/mpesa/callback/{secret}")
async def mpesa_callback(secret: str, request: Request, bg: BackgroundTasks):
    """Safaricom posts the result of each STK prompt here. The secret in the path is how we know it is them."""
    expected = request.app.state.settings.mpesa_callback_secret
    if not expected or not hmac.compare_digest(secret.encode(), expected.encode()):
        raise HTTPException(404)
    raw = await request.body()
    if len(raw) > 100_000:
        raise HTTPException(413)
    try:
        payload = json.loads(raw)
    except ValueError:
        payload = None
    if isinstance(payload, dict):
        bg.add_task(callback_task, request.app.state.ctx, payload)
    return {"ResultCode": 0, "ResultDesc": "Accepted"}


@app.post("/telegram/webhook")
async def telegram_webhook(request: Request, bg: BackgroundTasks):
    """Telegram sends the secret we gave it in a header; without it the request is not from Telegram."""
    secret = request.app.state.settings.telegram_webhook_secret
    sent = request.headers.get("x-telegram-bot-api-secret-token", "")
    tg = request.app.state.tg
    if tg is None or not secret or not hmac.compare_digest(sent.encode(), secret.encode()):
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
        bg.add_task(handle, request.app, msg)
    return {"ok": True}
