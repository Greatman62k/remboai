import pytest
from app.ratelimit import RateLimiter
from unittest.mock import AsyncMock


class FakeRedis:
    def __init__(self):
        self.store = {}
        self.ttl = {}
    async def eval(self, script, numkeys, key, ttl):
        c = self.store.get(key, 0) + 1
        self.store[key] = c
        return c


@pytest.mark.asyncio
async def test_burst_limit():
    redis = FakeRedis()
    class S:
        rate_burst = 2
        rate_window_s = 60
        rate_daily = 100
        llm_daily_per_salon = 10
    r = RateLimiter(redis, S())
    assert await r.check_sender("sid", "254712345678") == "ok"
    assert await r.check_sender("sid", "254712345678") == "ok"
    assert await r.check_sender("sid", "254712345678") == "notify"
    assert await r.check_sender("sid", "254712345678") == "drop"


@pytest.mark.asyncio
async def test_daily_limit():
    redis = FakeRedis()
    class S:
        rate_burst = 100
        rate_window_s = 60
        rate_daily = 2
        llm_daily_per_salon = 10
    r = RateLimiter(redis, S())
    assert await r.check_sender("sid", "254700000001") == "ok"
    assert await r.check_sender("sid", "254700000001") == "ok"
    assert await r.check_sender("sid", "254700000001") == "notify"
    assert await r.check_sender("sid", "254700000001") == "drop"


@pytest.mark.asyncio
async def test_llm_daily():
    redis = FakeRedis()
    class S:
        rate_burst = 10
        rate_window_s = 60
        rate_daily = 300
        llm_daily_per_salon = 2
    r = RateLimiter(redis, S())
    assert await r.llm_allowed("salon1") is True
    assert await r.llm_allowed("salon1") is True
    assert await r.llm_allowed("salon1") is False
    assert await r.llm_allowed("salon2") is True


@pytest.mark.asyncio
async def test_no_redis_fallback():
    class S:
        rate_burst = 2
        rate_window_s = 60
        rate_daily = 2
        llm_daily_per_salon = 2
    r = RateLimiter(None, S())
    assert await r.check_sender("sid", "2547") == "ok"
    assert await r.llm_allowed("salon") is True
