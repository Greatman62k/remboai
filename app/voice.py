"""Voice notes. Customers send voice notes more than they type, and many speak Swahili or Sheng.
The audio goes to our own Omnilingual ASR service (Meta's open-source speech model, 1600+ languages) and only the text comes back.
Nothing is stored: the audio exists in memory for the length of one request."""
from __future__ import annotations

import logging
import time as clock
from dataclasses import dataclass

import httpx

log = logging.getLogger("rembo.voice")
MAX_TEXT = 500


@dataclass(frozen=True, slots=True)
class Transcript:
    text: str
    engine: str
    ms: int


class Transcriber:
    def __init__(self, http: httpx.AsyncClient, settings):
        self.http, self.s = http, settings

    @property
    def enabled(self) -> bool:
        return bool(self.s.asr_url)

    async def transcribe(self, audio: bytes, mime: str, lang: str | None) -> Transcript | None:
        """lang is an Omnilingual code such as 'swh_Latn', or None to let the model decide."""
        if not self.enabled:
            return None
        t0 = clock.perf_counter()
        try:
            r = await self.http.post(self.s.asr_url, content=audio, params={"lang": lang} if lang else None, timeout=self.s.asr_timeout_s,
                                     headers={"Authorization": f"Bearer {self.s.asr_token}", "Content-Type": mime.split(";")[0] or "audio/ogg"})
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
