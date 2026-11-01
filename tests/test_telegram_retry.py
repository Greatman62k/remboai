import httpx
import pytest

from app.channels.telegram import Telegram


@pytest.mark.asyncio
async def test_connect_failures_are_retried_then_succeed(monkeypatch):
    calls = {"n": 0}

    def handler(request):
        calls["n"] += 1
        if calls["n"] < 3:
            raise httpx.ConnectError("boom", request=request)
        return httpx.Response(200, json={"ok": True, "result": {"message_id": 1}})

    async def no_sleep(_):
        return None
    monkeypatch.setattr("app.channels.telegram.asyncio.sleep", no_sleep)
    async with httpx.AsyncClient(transport=httpx.MockTransport(handler)) as http:
        assert await Telegram(http, "1:abc").send_text("5", "hi") is True
    assert calls["n"] == 3


@pytest.mark.asyncio
async def test_connect_failure_gives_up_after_three_tries(monkeypatch):
    calls = {"n": 0}

    def handler(request):
        calls["n"] += 1
        raise httpx.ConnectTimeout("slow", request=request)

    async def no_sleep(_):
        return None
    monkeypatch.setattr("app.channels.telegram.asyncio.sleep", no_sleep)
    async with httpx.AsyncClient(transport=httpx.MockTransport(handler)) as http:
        assert await Telegram(http, "1:abc").send_text("5", "hi") is False
    assert calls["n"] == 3


@pytest.mark.asyncio
async def test_read_errors_are_not_retried():
    calls = {"n": 0}

    def handler(request):
        calls["n"] += 1
        raise httpx.ReadTimeout("late", request=request)

    async with httpx.AsyncClient(transport=httpx.MockTransport(handler)) as http:
        assert await Telegram(http, "1:abc").send_text("5", "hi") is False
    assert calls["n"] == 1   # the message may already have been sent: never repeat it
