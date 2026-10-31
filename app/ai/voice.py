"""Voice notes. Customers send voice notes more than they type, and many speak Swahili or Sheng.
The audio goes to our own Omnilingual ASR service (Meta's open-source speech model, 1600+ languages) and only the text comes back.
Nothing is stored: the audio exists in memory for the length of one request."""
from __future__ import annotations

import logging
import time as clock
from dataclasses import dataclass

import httpx

from app.ratelimit import RateLimiter

log = logging.getLogger("rembo.voice")
MAX_TEXT = 500


@dataclass(frozen=True, slots=True)
class Transcript:
    text: str
    engine: str
    ms: int


class Transcriber:
    def __init__(self, http: httpx.AsyncClient, settings, budget=None, redis=None):
        self.http, self.s, self.budget, self.redis = http, settings, budget, redis

    @property
    def enabled(self) -> bool:
        return bool(self.s.asr_url)

    @property
    def auth_headers(self) -> dict:
        h = {"X-Asr-Token": self.s.asr_token}
        if self.s.asr_proxy_key:
            h |= {"Modal-Key": self.s.asr_proxy_key, "Modal-Secret": self.s.asr_proxy_secret}
        return h

    async def transcribe(self, audio: bytes, mime: str, lang: str | None, salon_id: str | None = None) -> Transcript | None:
        """lang is an Omnilingual code such as 'swh_Latn', or None to let the model decide."""
        if not self.enabled or (self.budget is not None and not await self.budget.take("voice")):
            return None
        if salon_id and self.redis is not None and not await RateLimiter(self.redis, self.s).llm_allowed(salon_id, kind="voice"):
            log.warning("salon %s hit its daily voice allowance", salon_id)
            return None
        t0 = clock.perf_counter()
        try:
            r = await self.http.post(self.s.asr_url, content=audio, params={"lang": lang} if lang else None, timeout=self.s.asr_timeout_s,
                                     headers={**self.auth_headers, "Content-Type": mime.split(";")[0] or "audio/ogg"})
            r.raise_for_status()
            text = " ".join(str(r.json().get("text", "")).split())[:MAX_TEXT]
        except (httpx.HTTPError, ValueError, AttributeError) as e:
            log.warning("speech recognition failed: %s", e.__class__.__name__)
            return None
        ms = int((clock.perf_counter() - t0) * 1000)
        if not text:
            return None
        log.info("heard a voice note: %d characters in %d ms", len(text), ms)
        return Transcript(text, "omnilingual-asr", ms)
