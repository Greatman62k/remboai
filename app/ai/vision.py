"""Photo matching. A customer sends a picture of the hairstyle they want; Muse Spark (Meta's multimodal model) says which of THIS
salon's services it needs. The model only suggests: the name is checked against the salon's own menu in code, and price and
duration always come from the menu, never from the model. The photo is held in memory for one request and never stored."""
from __future__ import annotations

import base64
import json
import logging
import struct
import time as clock
import zlib
from dataclasses import dataclass

import httpx
from pydantic import ValidationError

from .llm import _json_from

log = logging.getLogger("rembo.vision")
MAX_IMAGE = 4_000_000
OK_TYPES = {"image/jpeg", "image/png", "image/webp"}
MIN_CONFIDENCE = 0.5


@dataclass(frozen=True, slots=True)
class StyleGuess:
    service: str | None
    style: str
    confidence: float
    model: str
    ms: int


def build_prompt(services: list[str], policy: dict | None = None) -> str:
    from app.agent.policy import load_policy

    configured = (policy or load_policy())["vision_matcher"]
    return f"""{configured['identity']}
{"\n".join((policy or load_policy())["prompt_wrapper"])}
{json.dumps(configured, ensure_ascii=False, indent=2)}
Available salon services:
{json.dumps(services, ensure_ascii=False)}"""


def make_png(rgb: tuple[int, int, int] = (200, 30, 30), size: int = 16) -> bytes:
    """A tiny solid-colour PNG, used by the setup check to prove the model really receives images."""
    def chunk(kind: bytes, data: bytes) -> bytes:
        return struct.pack(">I", len(data)) + kind + data + struct.pack(">I", zlib.crc32(kind + data) & 0xFFFFFFFF)
    raw = b"".join(b"\x00" + bytes(rgb) * size for _ in range(size))
    return b"\x89PNG\r\n\x1a\n" + chunk(b"IHDR", struct.pack(">IIBBBBB", size, size, 8, 2, 0, 0, 0)) + chunk(b"IDAT", zlib.compress(raw)) + chunk(b"IEND", b"")


class StyleMatcher:
    def __init__(self, http: httpx.AsyncClient, settings, endpoints, budget=None):
        self.http, self.s, self._endpoints, self.budget = http, settings, endpoints, budget

    @property
    def enabled(self) -> bool:
        return bool(self.s.vision_enabled and self.s.llm_api_key)

    def body(self, model: str, prompt: str, image: bytes, mime: str, caption: str = "") -> dict:
        url = f"data:{mime};base64,{base64.b64encode(image).decode()}"
        body: dict = {"model": model, "messages": [
            {"role": self.s.llm_system_role, "content": prompt},
            {"role": "user", "content": [{"type": "text", "text": f'Customer note: "{caption[:200]}"' if caption else "Which service does this look need?"},
                                         {"type": "image_url", "image_url": {"url": url}}]}]}
        if self.s.llm_reasoning_effort:
            body["reasoning_effort"] = self.s.llm_reasoning_effort
        return body

    async def match(self, image: bytes, mime: str, services: list[str], caption: str = "") -> StyleGuess | None:
        mime = mime.split(";")[0].strip().lower()
        if not self.enabled or mime not in OK_TYPES or not 0 < len(image) <= MAX_IMAGE:
            return None
        if self.budget is not None and not await self.budget.take("vision"):
            return None
        for _backend, base, key, model, _ in self._endpoints():
            t0 = clock.perf_counter()
            try:
                from app.agent.policy import load_policy

                prompt = build_prompt(services, load_policy(getattr(self.s, "agent_policy_path", None)))
                r = await self.http.post(base.rstrip("/") + "/chat/completions", json=self.body(model, prompt, image, mime, caption),
                                         headers={"Authorization": f"Bearer {key}"}, timeout=self.s.llm_timeout_s * 2)
                r.raise_for_status()
                d = _json_from(r.json()["choices"][0]["message"]["content"])
                conf = max(0.0, min(1.0, float(d.get("confidence") or 0)))
                svc = d.get("service")
                return StyleGuess(svc if isinstance(svc, str) and svc.strip() else None, str(d.get("style") or "")[:60], conf, model, int((clock.perf_counter() - t0) * 1000))
            except (httpx.HTTPError, KeyError, IndexError, ValueError, TypeError, AttributeError, ValidationError) as e:
                log.warning("photo matching on %s failed: %s", model, e.__class__.__name__)
        return None
