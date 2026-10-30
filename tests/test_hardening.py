import logging
import time

import pytest
from fastapi import FastAPI, Request
from fastapi.responses import PlainTextResponse
from fastapi.testclient import TestClient

from app.hardening import BodyLimit, IngressRateLimit, MaskFilter, callback_secret_ok, install_log_masking, mask_phones, ops_api_key_ok, redact_log_secrets, safe_equals, startup_checks


def test_safe_equals():
    assert safe_equals("secret", "secret") is True
    assert safe_equals("secret", "wrong") is False
    assert safe_equals(None, "x") is False
    assert safe_equals("x", None) is False
    assert safe_equals("", "") is True
    assert safe_equals("caf\xe9", "caf\xe9") is True
    assert safe_equals("caf\xe9", "cafe") is False
    assert safe_equals("a" * 1000, "a" * 1000) is True
    assert safe_equals("\U0001f600", "\U0001f600") is True
    assert safe_equals("\U0001f600", "\U0001f601") is False


def test_callback_secret_ok():
    class S:
        pass
    s = S()
    for weak in ["", "change-me", "secret", "password", "short", "aaaaaaaa", "abababababababab"]:
        s.mpesa_callback_secret = weak
        assert callback_secret_ok(s) is False, weak
    s.mpesa_callback_secret = "effaf554118a08d5bafbff7e59e8f62f7273a699eb24388f"
    assert callback_secret_ok(s) is True
    s.mpesa_callback_secret = "abcd1234efgh5678ijkl9012"
    assert callback_secret_ok(s) is True
    s.mpesa_callback_secret = "1234567890123456"
    assert callback_secret_ok(s) is True


def test_ops_api_key_ok():
    class S:
        pass
    s = S()
    s.ops_api_key = ""
    assert ops_api_key_ok(s) is True
    s.ops_api_key = "changeme"
    assert ops_api_key_ok(s) is False
    s.ops_api_key = "a" * 64
    assert ops_api_key_ok(s) is False
    s.ops_api_key = "0123456789abcdef" * 4
    assert ops_api_key_ok(s) is True
    s.ops_api_key = "deadbeef" * 8 + "0123456789ab"
    assert len(s.ops_api_key) >= 64
    assert ops_api_key_ok(s) is True


def test_startup_checks_sandbox_allows_weak_but_logs(monkeypatch, caplog):
    class S:
        mpesa_env = "sandbox"
        mpesa_callback_secret = "weak"
        wa_app_secret = "short"
        wa_verify_token = "x"
        ingress_rate_burst = 120
        ingress_rate_global = 600
        ingress_rate_window_s = 60
        ops_api_key = ""
        mpesa_verify_callbacks = True
    with caplog.at_level(logging.ERROR):
        problems = startup_checks(S())
    assert len(problems) >= 3
    assert any("MPESA_CALLBACK_SECRET" in p for p in problems)
    assert any("WA_APP_SECRET" in p for p in problems)


def test_startup_checks_production_refuses_weak():
    class S:
        mpesa_env = "production"
        mpesa_callback_secret = "weak"
        wa_app_secret = "a" * 16
        wa_verify_token = "b" * 8
        ingress_rate_burst = 120
        ingress_rate_global = 600
        ingress_rate_window_s = 60
        ops_api_key = ""
        mpesa_verify_callbacks = True
    try:
        startup_checks(S())
        assert False, "should raise"
    except RuntimeError as e:
        assert "MPESA_CALLBACK_SECRET" in str(e)


def test_startup_checks_production_ok():
    class S:
        mpesa_env = "production"
        mpesa_callback_secret = "effaf554118a08d5bafbff7e59e8f62f7273a699eb24388f"
        wa_app_secret = "a" * 16
        wa_verify_token = "b" * 8
        ingress_rate_burst = 120
        ingress_rate_global = 600
        ingress_rate_window_s = 60
        ops_api_key = ""
        mpesa_verify_callbacks = True
        token_encryption_key = "a"*64
    assert startup_checks(S()) == []


def test_mask_phones():
    assert mask_phones("call 254712345678 now") == "call 2547*****678 now"
    assert mask_phones("+254712345678") == "+2547*****678"
    assert mask_phones("id 12345") == "id 12345"
    assert mask_phones("Meta id 1234567890123456") == "Meta id 1234567890123456"


def test_redact_log_secrets():
    assert "[REDACTED]" in redact_log_secrets("https://x?access_token=secret123")
    assert "[REDACTED]" in redact_log_secrets("Bearer abc.def.ghi-jkl")
    assert "[REDACTED]" in redact_log_secrets("api_key=supersecret")


def test_mask_filter():
    f = MaskFilter()
    record = logging.LogRecord("test", logging.INFO, "", 0, "call 254712345678 Bearer token12345678", (), None)
    f.filter(record)
    assert "*" in record.msg
    assert "token12345678" not in record.msg


def test_body_limit_content_length():
    app = FastAPI()

    @app.post("/x")
    async def x(request: Request):
        return PlainTextResponse("ok")

    app.add_middleware(BodyLimit, max_bytes=10)
    c = TestClient(app)
    r = c.post("/x", content=b"a" * 5)
    assert r.status_code == 200
    r = c.post("/x", content=b"a" * 11)
    assert r.status_code == 413


def test_body_limit_streamed():
    app = FastAPI()

    @app.post("/x")
    async def x(request: Request):
        body = await request.body()
        return PlainTextResponse(f"len={len(body)}")

    app.add_middleware(BodyLimit, max_bytes=20)
    c = TestClient(app)
    r = c.post("/x", content=b"a"*15)
    assert r.status_code == 200
    r = c.post("/x", content=b"a"*25)
    assert r.status_code == 413


def test_ingress_local_fallback():
    from app import hardening as h
    h._INGRESS_LOCAL.clear()
    app = FastAPI()

    @app.get("/ok")
    async def ok():
        return PlainTextResponse("ok")

    class FakeSettings:
        ingress_rate_burst = 2
        ingress_rate_global = 10
        ingress_rate_window_s = 60
        mpesa_env = "sandbox"
        redis_url = ""

    class FakeCtx:
        settings = FakeSettings()
        redis = None

    app.add_middleware(IngressRateLimit)
    app.add_middleware(BodyLimit, max_bytes=256*1024)

    async def _fake_lifespan(a):
        yield
    app.router.lifespan_context = _fake_lifespan

    @app.get("/health")
    async def health(request: Request):
        return PlainTextResponse("ok")

    client = TestClient(app)
    app.state = type("S", (), {"ctx": FakeCtx()})()
    r = client.get("/ok", headers={"X-Forwarded-For": "1.1.1.1"})
    assert r.status_code in (200, 429, 503)


def test_ingress_per_source_limit():
    from app import hardening as h
    h._INGRESS_LOCAL.clear()
    app = FastAPI()

    @app.get("/data")
    async def data():
        return PlainTextResponse("ok")

    class Settings:
        ingress_rate_burst = 2
        ingress_rate_global = 100
        ingress_rate_window_s = 60
        mpesa_env = "sandbox"
    class Ctx:
        settings = Settings()
        redis = None
    app.add_middleware(IngressRateLimit)
    c = TestClient(app)
    c.app.state = type("S", (), {"ctx": Ctx()})()
    ok = 0
    blocked = 0
    for i in range(4):
        r = c.get("/data")
        if r.status_code == 200:
            ok += 1
        elif r.status_code == 429:
            blocked += 1
    assert ok == 2
    assert blocked == 2


def test_ingress_global_limit():
    from app import hardening as h
    h._INGRESS_LOCAL.clear()
    app = FastAPI()
    @app.get("/g")
    async def g():
        return PlainTextResponse("ok")
    class Settings:
        ingress_rate_burst = 100
        ingress_rate_global = 2
        ingress_rate_window_s = 60
        mpesa_env = "sandbox"
    class Ctx:
        settings = Settings()
        redis = None
    app.add_middleware(IngressRateLimit)
    c = TestClient(app)
    c.app.state = type("S", (), {"ctx": Ctx()})()
    statuses = [c.get("/g").status_code for _ in range(4)]
    assert statuses.count(200) == 2
    assert statuses.count(429) == 2


def test_ingress_production_503_when_redis_down():
    from app import hardening as h
    h._INGRESS_LOCAL.clear()
    app = FastAPI()
    @app.get("/p")
    async def p():
        return PlainTextResponse("ok")
    class Settings:
        ingress_rate_burst = 120
        ingress_rate_global = 600
        ingress_rate_window_s = 60
        mpesa_env = "production"
    class Ctx:
        settings = Settings()
        redis = None
    app.add_middleware(IngressRateLimit)
    c = TestClient(app)
    c.app.state = type("S", (), {"ctx": Ctx()})()
    r = c.get("/p")
    assert r.status_code == 503
    assert b"ingress protection" in r.content


def test_install_wires_middleware():
    app = FastAPI()
    hardening = __import__("app.hardening", fromlist=["install"])
    hardening.install(app)
    assert len(app.user_middleware) >= 3


def test_client_ip_keeps_ipv6_hosts_whole():
    from app.hardening import _client_ip
    def scope(fwd, direct="10.0.0.5"):
        return {"client": (direct, 1), "headers": [(b"x-forwarded-for", fwd.encode())]}
    assert _client_ip(scope("2001:db8::1", "10.0.0.5"), "10.0.0.0/8") == "2001:db8::1"
    assert _client_ip(scope("[2001:db8::1]:4123", "10.0.0.5"), "10.0.0.0/8") == "2001:db8::1"
    assert _client_ip(scope("203.0.113.7:4123", "10.0.0.5"), "10.0.0.0/8") == "203.0.113.7"
    assert _client_ip(scope("203.0.113.7", "10.0.0.5"), "10.0.0.0/8") == "203.0.113.7"
    assert _client_ip(scope("2001:db8::1", "198.51.100.9"), "10.0.0.0/8") == "198.51.100.9"


def test_ops_key_message_matches_the_check():
    from app.hardening import startup_checks
    from types import SimpleNamespace
    s = SimpleNamespace(mpesa_env="production", mpesa_callback_secret="x" * 16, wa_app_secret="x" * 16,
                        wa_verify_token="verifyme", mpesa_verify_callbacks=True, ops_api_key="a" * 64,
                        token_encryption_key="", ingress_rate_burst=1, ingress_rate_global=1, ingress_rate_window_s=1)
    try:
        startup_checks(s)
        assert False, "weak production key must refuse to start"
    except RuntimeError as e:
        assert "64" in str(e) and "8 distinct" in str(e)
