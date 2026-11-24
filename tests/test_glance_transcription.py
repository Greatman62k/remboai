import asyncio
import hashlib
import json
import os
import wave
from io import BytesIO

import httpx
import pytest

from app.ai.audio import MAX_PCM_BYTES
from app.ai.voice import Transcript
from app.glance_api import PREFIX
from app.glance_sessions import exchange_pairing_code, issue_pairing_code
from app.main import app
from tests.harness import build

DSN = os.environ.get("TEST_DATABASE_URL")
needs_db = pytest.mark.skipif(not DSN, reason="needs TEST_DATABASE_URL")
OWNER = "tg:700000031"
HMAC_KEY = "0123456789abcdef" * 4
PATH = PREFIX + "/transcriptions"


def _settings(**overrides):
    values = {
        "glance_enabled": True,
        "glance_pairing_hmac_keyring": json.dumps({"1": HMAC_KEY}),
        "glance_pairing_hmac_active_version": 1,
        "glance_session_ttl_seconds": 604800,
        "glance_exchange_window_seconds": 900,
        "glance_exchange_source_limit": 12,
        "glance_exchange_owner_limit": 10,
        "glance_pairing_generation_window_seconds": 3600,
        "glance_pairing_generation_limit": 5,
        "glance_read_limit_per_minute": 120,
        "glance_write_limit_per_minute": 20,
        "glance_transcription_limit_per_minute": 6,
        "glance_cors_origins": "https://glance.test",
        "ops_api_key": "0123456789abcdef" * 4,
    }
    values.update(overrides)
    return values


async def _built(**overrides):
    pool, outside, salon_id = await build(DSN, **_settings(**overrides))
    await pool.execute("UPDATE salons SET owner_wa_id=$2,tg_bot_id='tg:111' WHERE id=$1::uuid", salon_id, OWNER)
    return pool, outside, salon_id, app.state.settings


class FakeVoice:
    enabled = True

    def __init__(self, result=Transcript("Transcript for the owner only.", "test", 1), error=None):
        self.result = result
        self.error = error
        self.calls = []
        self.cancelled = False

    async def transcribe(self, audio, mime, lang, salon_id=None):
        self.calls.append((audio, mime, lang, salon_id))
        if self.error:
            if self.error == "wait":
                try:
                    await asyncio.Event().wait()
                except asyncio.CancelledError:
                    self.cancelled = True
                    raise
            raise RuntimeError(self.error)
        return self.result


async def _client():
    return httpx.AsyncClient(transport=httpx.ASGITransport(app=app), base_url="http://test")


async def _pair(pool, settings, salon_id):
    issued = await issue_pairing_code(pool, settings, OWNER, salon_id, "tg:111")
    return await exchange_pairing_code(pool, settings, issued["code"], "Test G2", "192.0.2.90")


def _headers(token, content_type="application/octet-stream"):
    return {"Authorization": f"Bearer {token}", "Content-Type": content_type}


@needs_db
@pytest.mark.asyncio
async def test_transcription_rejects_missing_invalid_expired_revoked_and_unauthorized_sessions_before_asr():
    pool, _, salon_id, settings = await _built()
    voice = FakeVoice()
    app.state.ctx.voice = voice
    client = await _client()
    try:
        missing = await client.post(PATH, content=b"\0\0", headers={"Content-Type": "application/octet-stream"})
        invalid = await client.post(PATH, content=b"\0\0", headers=_headers("not-a-session"))
        assert missing.status_code == invalid.status_code == 401

        expired = await _pair(pool, settings, salon_id)
        await pool.execute("UPDATE glance_sessions SET created_at=now()-interval '2 seconds',"
                           "expires_at=now()-interval '1 second' WHERE token_hash=$1",
                           hashlib.sha256(expired["access_token"].encode()).digest())
        response = await client.post(PATH, content=b"\0\0", headers=_headers(expired["access_token"]))
        assert response.status_code == 401 and response.json()["error"]["code"] == "session_expired"

        revoked = await _pair(pool, settings, salon_id)
        await pool.execute("UPDATE glance_sessions SET revoked_at=now() WHERE token_hash=$1",
                           hashlib.sha256(revoked["access_token"].encode()).digest())
        response = await client.post(PATH, content=b"\0\0", headers=_headers(revoked["access_token"]))
        assert response.status_code == 403 and response.json()["error"]["code"] == "session_revoked"

        unauthorized = await _pair(pool, settings, salon_id)
        await pool.execute("UPDATE salons SET owner_wa_id='tg:700000099' WHERE id=$1::uuid", salon_id)
        response = await client.post(PATH, content=b"\0\0", headers=_headers(unauthorized["access_token"]))
        assert response.status_code == 403 and response.json()["error"]["code"] == "owner_not_authorized"
        assert voice.calls == []
    finally:
        await client.aclose()
        await pool.close()


@needs_db
@pytest.mark.asyncio
async def test_transcription_disabled_glance_and_asr_do_not_call_provider():
    pool, _, salon_id, settings = await _built(glance_enabled=False)
    voice = FakeVoice()
    app.state.ctx.voice = voice
    client = await _client()
    try:
        response = await client.post(PATH, content=b"\0\0", headers={"Content-Type": "application/octet-stream"})
        assert response.status_code == 404
        assert response.json()["error"]["code"] == "not_found"
        settings.glance_enabled = True
        paired = await _pair(pool, settings, salon_id)
        voice.enabled = False
        response = await client.post(PATH, content=b"\0\0", headers=_headers(paired["access_token"]))
        assert response.status_code == 503
        assert response.json()["error"]["code"] == "transcription_disabled"
        assert voice.calls == []
    finally:
        await client.aclose()
        await pool.close()


@needs_db
@pytest.mark.asyncio
async def test_transcription_validates_content_type_empty_odd_and_content_length():
    pool, _, salon_id, settings = await _built(glance_transcription_limit_per_minute=20)
    voice = FakeVoice()
    app.state.ctx.voice = voice
    paired = await _pair(pool, settings, salon_id)
    client = await _client()
    token = paired["access_token"]
    try:
        cases = [
            (b"\0\0", "application/json", None, 415, "invalid_content_type"),
            (b"", "application/octet-stream", None, 400, "empty_audio"),
            (b"\0", "application/octet-stream", None, 422, "invalid_audio"),
            (b"\0\0\0\0", "application/octet-stream", "2", 422, "invalid_audio"),
            (b"\0\0", "application/octet-stream", "4", 422, "invalid_audio"),
            (b"\0\0", "application/octet-stream", "invalid", 422, "invalid_audio"),
        ]
        for payload, content_type, length, status, code in cases:
            headers = _headers(token, content_type)
            if length is not None:
                headers["Content-Length"] = length
            response = await client.post(PATH, content=payload, headers=headers)
            assert response.status_code == status
            assert response.json()["error"]["code"] == code
            assert response.headers["cache-control"] == "no-store"
            assert response.headers.get("x-request-id")
        assert voice.calls == []
    finally:
        await client.aclose()
        await pool.close()


@needs_db
@pytest.mark.asyncio
async def test_transcription_wraps_pcm_and_returns_bounded_transcript_without_persisting_or_logging(caplog):
    pool, _, salon_id, settings = await _built()
    transcript = "PRIVATE TRANSCRIPT SHOULD NOT BE LOGGED 992233"
    voice = FakeVoice(Transcript(transcript, "internal-model", 3))
    app.state.ctx.voice = voice
    paired = await _pair(pool, settings, salon_id)
    client = await _client()
    audio = b"PCM_SENTINEL_8899!"
    try:
        response = await client.post(PATH, content=audio, headers=_headers(paired["access_token"]))
        assert response.status_code == 200
        assert response.json()["data"]["transcript"] == transcript
        assert response.json()["data"]["duration_ms"] >= 0
        assert response.headers["cache-control"] == "no-store"
        assert response.headers.get("x-request-id")
        assert len(voice.calls) == 1
        wav_audio, mime, lang, asr_salon_id = voice.calls[0]
        assert mime == "audio/wav" and lang is None and asr_salon_id == salon_id
        with wave.open(BytesIO(wav_audio), "rb") as wav:
            assert wav.getnchannels() == 1
            assert wav.getframerate() == 16_000
            assert wav.getsampwidth() == 2
            assert wav.readframes(wav.getnframes()) == audio
        assert transcript not in caplog.text
        assert "PCM_SENTINEL_8899!" not in caplog.text
        assert await pool.fetchval("SELECT count(*) FROM handoff_events") == 0
        assert await pool.fetchval("SELECT count(*) FROM handoff_drafts") == 0
        assert await pool.fetchval("SELECT count(*) FROM outbound_outbox") == 0
    finally:
        await client.aclose()
        await pool.close()


@needs_db
@pytest.mark.asyncio
async def test_transcription_accepts_exact_maximum_and_rejects_oversize_chunked_body_before_asr():
    pool, _, salon_id, settings = await _built(glance_transcription_limit_per_minute=10)
    voice = FakeVoice()
    app.state.ctx.voice = voice
    paired = await _pair(pool, settings, salon_id)
    client = await _client()
    audio = bytes(MAX_PCM_BYTES)
    try:
        accepted = await client.post(PATH, content=audio, headers=_headers(paired["access_token"]))
        assert accepted.status_code == 200
        assert len(voice.calls) == 1
        assert len(voice.calls[0][0]) == MAX_PCM_BYTES + 44

        async def chunks_without_length():
            yield bytes(MAX_PCM_BYTES)
            yield b"\0"

        oversized = await client.post(PATH, content=chunks_without_length(),
                                      headers=_headers(paired["access_token"]))
        assert oversized.status_code == 413
        assert oversized.json()["error"]["code"] == "audio_too_large"
        assert oversized.headers["cache-control"] == "no-store"
        assert oversized.headers["x-request-id"] == oversized.json()["request_id"]
        assert len(voice.calls) == 1
    finally:
        await client.aclose()
        await pool.close()


@needs_db
@pytest.mark.asyncio
async def test_transcription_provider_none_empty_and_provider_exception_have_stable_errors(caplog):
    pool, _, salon_id, settings = await _built(glance_transcription_limit_per_minute=10)
    voice = FakeVoice(None)
    app.state.ctx.voice = voice
    paired = await _pair(pool, settings, salon_id)
    client = await _client()
    try:
        response = await client.post(PATH, content=b"\0\0", headers=_headers(paired["access_token"]))
        assert response.status_code == 503
        assert response.json()["error"]["code"] == "transcription_unavailable"

        voice.result = Transcript("  \n ", "internal-model", 1)
        response = await client.post(PATH, content=b"\0\0", headers=_headers(paired["access_token"]))
        assert response.status_code == 422
        assert response.json()["error"]["code"] == "empty_transcript"

        voice.error = "provider-secret-detail-do-not-expose"
        response = await client.post(PATH, content=b"\0\0", headers=_headers(paired["access_token"]))
        assert response.status_code == 503
        assert response.json()["error"]["code"] == "transcription_unavailable"
        assert "provider-secret-detail-do-not-expose" not in response.text
        assert "provider-secret-detail-do-not-expose" not in caplog.text
    finally:
        await client.aclose()
        await pool.close()


@needs_db
@pytest.mark.asyncio
async def test_transcription_timeout_cancels_inference_task(monkeypatch):
    from app import glance_api

    pool, _, salon_id, settings = await _built()
    voice = FakeVoice(error="wait")
    app.state.ctx.voice = voice
    paired = await _pair(pool, settings, salon_id)
    client = await _client()
    monkeypatch.setattr(glance_api, "TRANSCRIPTION_TIMEOUT_SECONDS", 0.02)
    try:
        response = await client.post(PATH, content=b"\0\0", headers=_headers(paired["access_token"]))
        assert response.status_code == 504
        assert response.json()["error"]["code"] == "transcription_timeout"
        assert voice.cancelled is True
        assert len(voice.calls) == 1
    finally:
        await client.aclose()
        await pool.close()


@needs_db
@pytest.mark.asyncio
async def test_transcription_quota_is_per_session_and_rejects_before_asr():
    pool, _, salon_id, settings = await _built(glance_transcription_limit_per_minute=1)
    voice = FakeVoice()
    app.state.ctx.voice = voice
    paired = await _pair(pool, settings, salon_id)
    client = await _client()
    try:
        first = await client.post(PATH, content=b"\0\0", headers=_headers(paired["access_token"]))
        second = await client.post(PATH, content=b"\0\0", headers=_headers(paired["access_token"]))
        assert first.status_code == 200
        assert second.status_code == 429
        assert second.json()["error"]["code"] == "rate_limited"
        assert second.headers.get("retry-after")
        assert len(voice.calls) == 1
    finally:
        await client.aclose()
        await pool.close()


@needs_db
@pytest.mark.asyncio
async def test_transcription_cors_preflight_is_unauthenticated_but_request_still_needs_session():
    pool, _, _, _ = await _built()
    client = await _client()
    try:
        preflight = await client.options(PATH, headers={
            "Origin": "https://glance.test",
            "Access-Control-Request-Method": "POST",
            "Access-Control-Request-Headers": "authorization,content-type",
        })
        assert preflight.status_code == 204
        assert preflight.headers["access-control-allow-origin"] == "https://glance.test"
        assert "Authorization" in preflight.headers["access-control-allow-headers"]
        assert "Content-Type" in preflight.headers["access-control-allow-headers"]
        unauthenticated = await client.post(PATH, content=b"\0\0", headers={
            "Origin": "https://glance.test", "Content-Type": "application/octet-stream"})
        assert unauthenticated.status_code == 401
    finally:
        await client.aclose()
        await pool.close()


class _WarmupHTTP:
    def __init__(self):
        self.calls = []

    async def get(self, url, *, headers, timeout):
        self.calls.append((url, headers, timeout))
        return httpx.Response(200, request=httpx.Request("GET", url))


@needs_db
@pytest.mark.asyncio
async def test_asr_warmup_is_ops_key_protected_and_probes_health_without_audio():
    pool, _, _, settings = await _built()
    client = await _client()
    voice = type("Voice", (), {
        "enabled": True,
        "http": _WarmupHTTP(),
        "auth_headers": {"X-Asr-Token": "server-only"},
    })()
    app.state.ctx.voice = voice
    try:
        denied = await client.post("/ops/asr/warmup")
        assert denied.status_code == 401
        assert voice.http.calls == []

        ready = await client.post("/ops/asr/warmup", headers={"X-Ops-Key": settings.ops_api_key})
        assert ready.status_code == 200
        assert ready.json()["status"] == "ready"
        assert ready.headers["cache-control"] == "no-store"
        assert voice.http.calls[0][0] == "https://asr.test/health"
        assert voice.http.calls[0][1] == {"X-Asr-Token": "server-only"}
        assert voice.http.calls[0][2] == settings.asr_timeout_s
    finally:
        await client.aclose()
        await pool.close()
