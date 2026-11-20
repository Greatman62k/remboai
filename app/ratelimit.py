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

    async def _peek(self, key: str) -> int:
        if self.redis is None:
            return 0
        try:
            return int(await self.redis.get(key) or 0)
        except Exception:
            return 0

    async def check_sender(self, salon_id: str, wa_id: str, factor: int = 1, count: bool = True) -> str:
        """"ok", "notify" (the first message over the line) or "drop". `factor` raises both ceilings, for a shop owner answering a queue.
        count=False judges the counters as they stand, without adding this message a second time."""
        take = self._incr if count else (lambda key, ttl: self._peek(key))
        burst, daily = self.burst * factor, self.daily * factor
        if self.burst > 0:
            n = await take(f"rl:b:{salon_id}:{wa_id}", self.window)
            if n > burst:
                return "notify" if n == burst + 1 else "drop"
        if self.daily > 0:
            d = await take(f"rl:d:{salon_id}:{wa_id}", 86400)
            if d > daily:
                return "notify" if d == daily + 1 else "drop"
        return "ok"

    async def llm_allowed(self, scope_id: str, kind: str = "llm", limit: int | None = None) -> bool:
        daily_limit = self.llm_daily if limit is None else int(limit)
        if daily_limit <= 0:
            return True
        day = datetime.now(timezone.utc).strftime("%Y%m%d")
        return await self._incr(f"rl:{kind}:{scope_id}:{day}", 172800) <= daily_limit
