import pytest
from fastapi.testclient import TestClient
from app.main import app
from app.metrics import http_requests_total

def test_metrics_requires_auth(monkeypatch):
    c = TestClient(app)
    r = c.get("/metrics")
    assert r.status_code in (404, 401, 500)

def test_metrics_with_ctx():
    from types import SimpleNamespace
    from app.config import Settings
    import asyncpg
    settings = Settings(_env_file=None, database_url="postgresql://x", wa_app_secret="s", mpesa_callback_secret="a"*16, ops_api_key="0123456789abcdef"*4)
    app.state.ctx = SimpleNamespace(settings=settings, pool=None)
    import app.main as main
    orig = main.update_queue_depth
    async def noop(pool): pass
    main.update_queue_depth = noop
    c = TestClient(app)
    r = c.get("/metrics", headers={"x-ops-key": "0123456789abcdef"*4})
    assert r.status_code == 200
    assert b"rembo_http_requests_total" in r.content or b"TYPE" in r.content
    r = c.get("/metrics", headers={"x-ops-key": "wrong"})
    assert r.status_code == 401
    main.update_queue_depth = orig
    del app.state.ctx

def test_route_names_never_leak_secrets_or_raw_paths():
    from app.main import route_name
    assert route_name("/mpesa/callback/s3cr3t") == "/mpesa/callback/{secret}"
    assert route_name("/setup/abc123") == "other"
    assert route_name("/owner/login/abc123") == "other"
    assert route_name("/ops/webhook-jobs/42/retry") == "/ops/webhook-jobs/{job_id}"
    assert route_name("/ops/payment-reviews") == "/ops/payment-reviews"
    assert route_name("/webhook") == "/webhook"
    assert route_name("/health") == "/health"
    assert route_name("/wp-admin") == "other"
    assert "s3cr3t" not in route_name("/mpesa/callback/s3cr3t")


def test_metrics_middleware_counts():
    from types import SimpleNamespace
    from app.config import Settings
    settings = Settings(_env_file=None, database_url="postgresql://x", wa_app_secret="s", mpesa_callback_secret="a"*16, ops_api_key="0123456789abcdef"*4)
    app.state.ctx = SimpleNamespace(settings=settings, pool=None)
    import app.main as main
    async def noop(pool): pass
    orig = main.update_queue_depth
    main.update_queue_depth = noop
    c = TestClient(app)
    c.get("/health")
    r = c.get("/metrics", headers={"x-ops-key": "0123456789abcdef"*4})
    assert r.status_code == 200
    assert b"health" in r.content or b"http_requests" in r.content
    main.update_queue_depth = orig
    del app.state.ctx
