"""Glance security, schema and public-route checks in the setup doctor."""
import json
import os

import httpx
import pytest

from app.doctor import _glance_checks
from app.main import app
from tests.harness import build

DSN = os.environ.get("TEST_DATABASE_URL")
needs_db = pytest.mark.skipif(not DSN, reason="needs TEST_DATABASE_URL")
HMAC_KEY = "0123456789abcdef" * 4
ORIGIN = "https://glance.test"


@needs_db
@pytest.mark.asyncio
async def test_doctor_reports_disabled_glance_and_database_readiness_without_network_calls():
    pool, _, _ = await build(DSN, till=False)
    requests = []

    def handler(request):
        requests.append(request)
        return httpx.Response(500)

    async with httpx.AsyncClient(transport=httpx.MockTransport(handler)) as http:
        checks = {check.name: check for check in await _glance_checks(app.state.ctx.settings, http, pool)}
    try:
        assert checks["Glance configuration"].ok and checks["Glance configuration"].warn
        assert "disabled" in checks["Glance configuration"].detail
        assert checks["Glance database"].ok
        assert "047–049" in checks["Glance database"].detail
        assert requests == []
    finally:
        await pool.close()


@needs_db
@pytest.mark.asyncio
async def test_doctor_checks_enabled_glance_security_schema_https_auth_and_cors():
    pool, _, _ = await build(
        DSN,
        till=False,
        glance_enabled=True,
        glance_pairing_hmac_keyring=json.dumps({"1": HMAC_KEY}),
        glance_pairing_hmac_active_version=1,
        glance_session_ttl_seconds=604800,
        glance_exchange_window_seconds=900,
        glance_exchange_source_limit=12,
        glance_exchange_owner_limit=10,
        glance_pairing_generation_window_seconds=3600,
        glance_pairing_generation_limit=5,
        glance_read_limit_per_minute=120,
        glance_write_limit_per_minute=20,
        glance_transcription_limit_per_minute=6,
        glance_cors_origins=ORIGIN,
        public_base_url="https://glance.test",
    )
    requests = []

    def handler(request):
        requests.append(request)
        if request.method == "OPTIONS":
            return httpx.Response(204, headers={
                "access-control-allow-origin": ORIGIN,
                "access-control-allow-methods": "GET, HEAD, OPTIONS",
                "access-control-allow-headers": "Authorization, Content-Type",
            })
        return httpx.Response(401, json={"error": {"code": "authentication_required"}}, headers={
            "access-control-allow-origin": ORIGIN,
            "cache-control": "no-store",
            "x-request-id": "doctor-request-id",
        })

    async with httpx.AsyncClient(transport=httpx.MockTransport(handler)) as http:
        checks = {check.name: check for check in await _glance_checks(app.state.ctx.settings, http, pool)}
    try:
        assert checks["Glance configuration"].ok
        assert checks["Glance database"].ok
        assert checks["Glance CORS"].ok
        assert checks["Glance HTTPS API"].ok
        assert [request.method for request in requests] == ["OPTIONS", "GET"]
        assert requests[0].headers["origin"] == ORIGIN
        assert requests[1].headers["origin"] == ORIGIN
        assert "authorization" not in requests[1].headers
        details = " ".join(check.detail for check in checks.values())
        assert HMAC_KEY not in details
    finally:
        await pool.close()


@needs_db
@pytest.mark.asyncio
async def test_doctor_fails_enabled_glance_closed_for_missing_keyring_or_wildcard_origin():
    pool, _, _ = await build(
        DSN,
        till=False,
        glance_enabled=True,
        glance_pairing_hmac_keyring="",
        glance_pairing_hmac_active_version=0,
        glance_cors_origins="*",
        public_base_url="https://glance.test",
    )
    requests = []

    def handler(request):
        requests.append(request)
        return httpx.Response(204)

    async with httpx.AsyncClient(transport=httpx.MockTransport(handler)) as http:
        checks = {check.name: check for check in await _glance_checks(app.state.ctx.settings, http, pool)}
    try:
        assert not checks["Glance configuration"].ok
        assert checks["Glance database"].ok
        assert "secret" not in checks["Glance configuration"].detail.lower()
        assert requests == []
    finally:
        await pool.close()
