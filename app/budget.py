"""A daily ceiling on metered calls. When a ceiling is reached the feature degrades instead of spending more:
text understanding falls back to keyword rules, photo matching shows the menu, voice notes ask the customer to type.
Counted in Postgres so a restart does not reset it; the day is the Nairobi calendar day. A limit of 0 means no limit."""
from __future__ import annotations

import logging

log = logging.getLogger("rembo.budget")


class Budget:
    def __init__(self, pool, settings):
        self.pool = pool
        self.limits = {"text": settings.llm_daily_limit, "vision": settings.vision_daily_limit, "voice": settings.voice_daily_limit}
        self._said: set[tuple[str, str]] = set()

    async def take(self, kind: str) -> bool:
        """Count one call. False means today's ceiling is reached and the call must not be made."""
        limit = self.limits.get(kind, 0)
        if limit <= 0:
            return True
        try:
            row = await self.pool.fetchrow(
                "INSERT INTO model_usage (day, kind, calls) VALUES ((now() AT TIME ZONE 'Africa/Nairobi')::date, $1, 1) "
                "ON CONFLICT (day, kind) DO UPDATE SET calls = model_usage.calls + 1 WHERE model_usage.calls < $2 "
                "RETURNING day, calls", kind, limit)
        except Exception:  # noqa: BLE001  a counting problem must not take the receptionist down
            log.exception("could not count a %s call; allowing it", kind)
            return True
        if row is None:
            day = str(await self.pool.fetchval("SELECT (now() AT TIME ZONE 'Africa/Nairobi')::date"))
            if (day, kind) not in self._said:
                self._said.add((day, kind))
                log.error("DAILY LIMIT REACHED for %s calls (%d): falling back until tomorrow", kind, limit)
            return False
        return True

    async def today(self) -> dict[str, int]:
        rows = await self.pool.fetch("SELECT kind, calls FROM model_usage WHERE day = (now() AT TIME ZONE 'Africa/Nairobi')::date")
        return {r["kind"]: r["calls"] for r in rows}

