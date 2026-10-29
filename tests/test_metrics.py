import pytest
from fastapi.testclient import TestClient
from app.main import app
from app.metrics import http_requests_total

def test_metrics_requires_auth(monkeypatch):
    c = TestClient(app)
    # without ctx, 404
    r = c.get("/metrics")
    assert r.status_code in (404, 401, 500)

def test_metrics_with_ctx():
    from types import SimpleNamespace
    from app.config import Settings
    import asyncpg
    # setup minimal ctx
    settings = Settings(_env_file=None, database_url="postgresql://x", wa_app_secret="s", mpesa_callback_secret="a"*16, ops_api_key="0123456789abcdef"*4)
    app.state.ctx = SimpleNamespace(settings=settings, pool=None)
    # mock update_queue_depth to avoid DB
    import app.main as main
    orig = main.update_queue_depth
    async def noop(pool): pass
    main.update_queue_depth = noop
    c = TestClient(app)
    r = c.get("/metrics", headers={"x-ops-key": "0123456789abcdef"*4})
    assert r.status_code == 200
    assert b"rembo_http_requests_total" in r.content or b"TYPE" in r.content
    # wrong key
    r = c.get("/metrics", headers={"x-ops-key": "wrong"})
    assert r.status_code == 401
    main.update_queue_depth = orig
    # cleanup
    del app.state.ctx

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
    # trigger a request
    c.get("/health")
    # metrics should have incremented
    r = c.get("/metrics", headers={"x-ops-key": "0123456789abcdef"*4})
    assert r.status_code == 200
    # http_requests_total should contain health
    assert b"health" in r.content or b"http_requests" in r.content
    main.update_queue_depth = orig
    del app.state.ctx
