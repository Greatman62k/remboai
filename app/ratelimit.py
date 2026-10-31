from __future__ import annotations

from datetime import datetime, timezone

_INCR = """
local c = redis.call('INCR', KEYS[1])
if c == 1 then redis.call('EXPIRE', KEYS[1], ARGV[1]) end
return c
"""

TOO_FAST = (
    "You're sending messages very quickly. Please wait a minute and try again.\n"
    "Unatuma ujumbe kwa haraka sana. Tafadhali subiri dakika moja ujaribu tena."
)


class RateLimiter:
    def __init__(self, redis, settings=None):
        g = lambda k, d: getattr(settings, k, d) if settings is not None else d
        self.redis = redis
        self.burst = int(g("rate_burst", 10))
        self.window = int(g("rate_window_s", 60))
        self.daily = int(g("rate_daily", 300))
        self.llm_daily = int(g("llm_daily_per_salon", 5000))

    async def _incr(self, key: str, ttl: int) -> int:
        if self.redis is None:
            return 1
        try:
            return int(await self.redis.eval(_INCR, 1, key, ttl))
        except Exception:
            return 1

    async def check_sender(self, salon_id: str, wa_id: str) -> str:
        if self.burst > 0:
            n = await self._incr(f"rl:b:{salon_id}:{wa_id}", self.window)
            if n > self.burst:
                return "notify" if n == self.burst + 1 else "drop"
        if self.daily > 0:
            d = await self._incr(f"rl:d:{salon_id}:{wa_id}", 86400)
            if d > self.daily:
                return "notify" if d == self.daily + 1 else "drop"
        return "ok"

    async def llm_allowed(self, salon_id: str, kind: str = "llm") -> bool:
        if self.llm_daily <= 0:
            return True
        day = datetime.now(timezone.utc).strftime("%Y%m%d")
        return await self._incr(f"rl:{kind}:{salon_id}:{day}", 172800) <= self.llm_daily
