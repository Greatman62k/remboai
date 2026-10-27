import json
import logging
from contextlib import asynccontextmanager

import httpx
from fastapi import BackgroundTasks, FastAPI, HTTPException, Request
from fastapi.responses import PlainTextResponse

from .config import get_settings
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
    app.state.settings, app.state.pool, app.state.wa = s, pool, WhatsApp(http, s.graph_version)
    yield
    await http.aclose()
    await pool.close()


app = FastAPI(lifespan=lifespan, docs_url=None, redoc_url=None, openapi_url=None)


@app.get("/health")
async def health():
    return {"ok": True}


@app.get("/webhook")
async def verify(request: Request):
    q = request.query_params
    expected = request.app.state.settings.wa_verify_token
    if q.get("hub.mode") == "subscribe" and q.get("hub.verify_token") == expected:
        return PlainTextResponse(q.get("hub.challenge", ""))
    raise HTTPException(403)


async def handle(app: FastAPI, msg: Message) -> None:
    """Runs after the 200 has gone back to Meta. Day 1: echo, once per message id."""
    try:
        fresh = await app.state.pool.fetchval(
            "INSERT INTO inbound_messages (wa_message_id, wa_id) VALUES ($1, $2) ON CONFLICT DO NOTHING RETURNING 1", msg.id, msg.sender)
        if not fresh:
            return
        log.info("message from %s kind=%s", mask(msg.sender), msg.kind)
        reply = f"Habari! I received: {msg.text}" if msg.text else "Habari! I can read text messages for now."
        await app.state.wa.send_text(msg.phone_number_id, app.state.settings.wa_access_token, msg.sender, reply)
    except Exception:
        log.exception("failed handling message %s", msg.id)


@app.post("/webhook")
async def receive(request: Request, bg: BackgroundTasks):
    raw = await request.body()
    if len(raw) > MAX_BODY:
        raise HTTPException(413)
    if not verify_signature(raw, request.headers.get("x-hub-signature-256"), request.app.state.settings.wa_app_secret):
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
