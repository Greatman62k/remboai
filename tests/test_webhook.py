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
from app.channels.whatsapp import WhatsApp

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


def test_responses_carry_baseline_security_headers():
    async def run():
        app.state.settings = SETTINGS
        async with web(app) as c:
            r = await c.get("/health")
            assert r.status_code == 200
            assert r.headers["x-content-type-options"] == "nosniff"
            assert r.headers["x-frame-options"] == "DENY"
            assert r.headers["referrer-policy"] == "no-referrer"
    asyncio.run(run())


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
def test_a_message_is_processed_exactly_once_even_if_meta_redelivers():
    from tests.harness import build, client, payload, post

    async def run():
        pool, out, _ = await build(DSN)
        out.script["prices?"] = {"intent": "price_list"}
        async with client() as c:
            assert (await post(c, payload("254712345678", "prices?", mid="wamid.7"))).status_code == 200
            assert (await post(c, payload("254712345678", "prices?", mid="wamid.7"))).status_code == 200
        assert out.calls == [] and len(out.sent) == 1
        assert out.sent[0]["to"] == "254712345678" and out.sent[0]["_auth"] == "Bearer TOK" and "Fade" in out.sent[0]["text"]["body"] and "KES 500" in out.sent[0]["text"]["body"]
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
        assert await db.migrate(pool, tmp_path) == []
        (tmp_path / "003_bad.sql").write_text("CREATE TABLE c (z int); SELECT * FROM missing_table;")
        with pytest.raises(Exception):
            await db.migrate(pool, tmp_path)
        assert await pool.fetchval("SELECT to_regclass('c')") is None
        await pool.close()
    asyncio.run(run())
