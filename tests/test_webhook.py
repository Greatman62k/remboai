import asyncio
import hashlib
import hmac
import json
import os
from types import SimpleNamespace

import httpx
import pytest

from app import db
from app.main import app
from app.whatsapp import WhatsApp

DSN = os.environ.get("TEST_DATABASE_URL")
needs_db = pytest.mark.skipif(not DSN, reason="needs TEST_DATABASE_URL")
SETTINGS = SimpleNamespace(wa_app_secret="s3cret", wa_verify_token="verify-me", wa_access_token="TOKEN", graph_version="v25.0")


def signed(payload: dict, secret: str = "s3cret"):
    raw = json.dumps(payload).encode()
    return raw, {"x-hub-signature-256": "sha256=" + hmac.new(secret.encode(), raw, hashlib.sha256).hexdigest(), "content-type": "application/json"}


def text_payload(text="nataka fade", mid="wamid.1"):
    return {"entry": [{"changes": [{"value": {"metadata": {"phone_number_id": "PID"}, "contacts": [{"wa_id": "254712345678", "profile": {"name": "W"}}],
                                              "messages": [{"from": "254712345678", "id": mid, "type": "text", "text": {"body": text}}]}}]}]}


def web(app_):
    return httpx.AsyncClient(transport=httpx.ASGITransport(app=app_), base_url="http://t")


def test_verification_handshake_and_forged_requests():
    async def run():
        app.state.settings = SETTINGS
        async with web(app) as c:
            ok = await c.get("/webhook", params={"hub.mode": "subscribe", "hub.verify_token": "verify-me", "hub.challenge": "42"})
            assert ok.status_code == 200 and ok.text == "42"
            assert (await c.get("/webhook", params={"hub.mode": "subscribe", "hub.verify_token": "nope", "hub.challenge": "1"})).status_code == 403
            raw, _ = signed(text_payload())
            assert (await c.post("/webhook", content=raw, headers={"x-hub-signature-256": "sha256=00"})).status_code == 403
            assert (await c.post("/webhook", content=raw)).status_code == 403
            bad, h = signed(text_payload())
            assert (await c.post("/webhook", content=b"not json", headers={**h, "x-hub-signature-256": "sha256=" + hmac.new(b"s3cret", b"not json", hashlib.sha256).hexdigest()})).status_code == 400
            assert (await c.get("/health")).json() == {"ok": True}
    asyncio.run(run())


@needs_db
def test_message_is_echoed_exactly_once_even_if_meta_redelivers():
    async def run():
        pool = await db.make_pool(DSN)
        await pool.execute("DROP SCHEMA public CASCADE; CREATE SCHEMA public;")
        await db.migrate(pool)
        sent = []

        def graph(req: httpx.Request) -> httpx.Response:
            sent.append((str(req.url), req.headers["authorization"], json.loads(req.content)))
            return httpx.Response(200, json={"messages": [{"id": "out"}]})

        http = httpx.AsyncClient(transport=httpx.MockTransport(graph))
        app.state.settings, app.state.pool, app.state.wa = SETTINGS, pool, WhatsApp(http, "v25.0")
        async with web(app) as c:
            raw, h = signed(text_payload("hello"))
            assert (await c.post("/webhook", content=raw, headers=h)).status_code == 200
            assert (await c.post("/webhook", content=raw, headers=h)).status_code == 200   # redelivery
        assert len(sent) == 1
        url, auth, body = sent[0]
        assert url == "https://graph.facebook.com/v25.0/PID/messages" and auth == "Bearer TOKEN"
        assert body["to"] == "254712345678" and "hello" in body["text"]["body"]
        await http.aclose()
        await pool.close()
    asyncio.run(run())


@needs_db
def test_migrations_apply_once_and_in_order(tmp_path):
    async def run():
        pool = await db.make_pool(DSN)
        await pool.execute("DROP SCHEMA public CASCADE; CREATE SCHEMA public;")
        (tmp_path / "001_a.sql").write_text("CREATE TABLE a (x int);")
        (tmp_path / "002_b.sql").write_text("CREATE TABLE b (y int);")
        assert await db.migrate(pool, tmp_path) == ["001_a.sql", "002_b.sql"]
        assert await db.migrate(pool, tmp_path) == []                        # nothing is applied twice
        (tmp_path / "003_bad.sql").write_text("CREATE TABLE c (z int); SELECT * FROM missing_table;")
        with pytest.raises(Exception):
            await db.migrate(pool, tmp_path)
        assert await pool.fetchval("SELECT to_regclass('c')") is None        # a failed migration leaves no half-applied table
        await pool.close()
    asyncio.run(run())
