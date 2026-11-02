"""The language model reads a message and fills a small checked form. It never decides availability, price or payment.

Endpoints are tried in order (Meta's own API first, then an optional Muse Spark Open host). If every endpoint fails,
plain keyword rules answer, so the receptionist never goes silent."""
from __future__ import annotations

import json
import logging
import re
import time as clock
from dataclasses import dataclass
from datetime import date, timedelta
from typing import Literal

import httpx
from pydantic import BaseModel, ValidationError

from app import lexicon, smalltalk
from app.ratelimit import RateLimiter
from app.timeparse import parse_day, parse_time, parse_window

log = logging.getLogger("rembo.llm")

Intent = Literal["book", "choose_slot", "cancel", "price_list", "hours", "greeting", "other"]


class Extraction(BaseModel):
    intent: Intent = "other"
    service: str | None = None
    date: str | None = None
    window: Literal["morning", "afternoon", "evening"] | None = None
    time: str | None = None
    choice: int | None = None
    language: Literal["en", "sw"] = "en"
    far_date: bool = False


@dataclass
class Result:
    ex: Extraction
    source: str
    model: str = ""
    ms: int = 0
    degraded: bool = False


def _failure_kind(exc: BaseException, status: int | None) -> str:
    """What went wrong with a model call, in words the logs and the operator can act on."""
    if status in (401, 403, 407):
        return "auth"
    if status == 429:
        return "rate_limited"
    if status is not None and status >= 500:
        return "server"
    if isinstance(exc, httpx.TimeoutException):
        return "timeout"
    if isinstance(exc, httpx.NetworkError):
        return "network"
    return "other"


def build_prompt(
    today: date, services: list[str], state: str, history: list[dict] | None = None, policy: dict | None = None
) -> str:
    from app.agent.policy import load_policy

    cal = "\n".join(f"{(today + timedelta(days=i)).strftime('%a')} = {(today + timedelta(days=i)).isoformat()}" for i in range(14))
    configured = (policy or load_policy())["legacy_booking_extractor"]
    hist = ""
    if history:
        lines = []
        for turn in history[-4:]:
            role = turn.get("role", "user")
            text = (turn.get("text") or "")[:200]
            lines.append(f"{role}: {text}")
        if lines:
            hist = "\nRecent conversation:\n" + "\n".join(lines) + "\n"
    time_guide = ", ".join(f"{phrase}={time}" for phrase, time in configured["swahili_time_guide"].items())
    return f"""{configured['identity']}
{"\n".join((policy or load_policy())["prompt_wrapper"])}
{json.dumps(configured, ensure_ascii=False, indent=2)}

Today is {today.strftime('%A')} {today.isoformat()} (Africa/Nairobi).
Swahili clock guide: {time_guide}
Calendar for the next 14 days:
{cal}
{hist}
{configured['menu_instruction']}
{json.dumps(services, ensure_ascii=False)}
Current conversation state: {state}
"""


def _json_from(content: str) -> dict:
    content = re.sub(r"```(?:json)?", "", content or "").strip()
    m = re.search(r"\{.*\}", content, re.S)
    if not m:
        raise ValueError("no JSON object in reply")
    return json.loads(m.group(0))


def sanitize(ex: Extraction, today: date) -> Extraction:
    if ex.date:
        try:
            d = date.fromisoformat(ex.date)
            if not today <= d <= today + timedelta(days=60):
                ex.far_date = True
                ex.date = None
            else:
                ex.far_date = False
        except ValueError:
            ex.date = None
            ex.far_date = False
    if ex.time and not re.fullmatch(r"([01]\d|2[0-3]):[0-5]\d", ex.time):
        ex.time = None
    if ex.choice is not None and ex.choice not in (1, 2, 3):
        ex.choice = None
    return ex


def _coerce(d: dict) -> Extraction:
    d = {k: v for k, v in d.items() if k in Extraction.model_fields}
    for k, v in list(d.items()):
        if v in ("", "null", "none", "None"):
            d[k] = None
    if d.get("intent") is None:
        d["intent"] = "other"
    if isinstance(d.get("choice"), str) and d["choice"].isdigit():
        d["choice"] = int(d["choice"])
    return Extraction(**d)


_LX = lexicon.load()
_SW_MARKERS, _CANCEL, _PRICE, _HOURS = (_LX.pattern(c) for c in ("sw_markers", "cancel", "price", "hours"))


def rule_extract(text: str, today: date, services: list[dict], state: str = "idle") -> Extraction:
    """Keyword fallback. It fills the same form, so the conversation (which remembers earlier answers) still completes a booking."""
    t = text.lower().strip()
    sw = bool(_SW_MARKERS.search(t))
    lang = "sw" if sw else "en"
    if state == "offered" and re.fullmatch(r"[1-3]", t):
        return Extraction(intent="choose_slot", choice=int(t), language=lang)
    if _CANCEL.search(t):
        return Extraction(intent="cancel", language=lang)
    if re.search(r"\b(service|services|huduma|orodha|menu)\b", t):
        return Extraction(intent="price_list", language=lang)
    if _PRICE.search(t) and not re.search(r"\bsaa\s+ngapi\b", t):
        return Extraction(intent="price_list", language=lang)
    if _HOURS.search(t):
        return Extraction(intent="hours", language=lang)
    svc = next((s["name"] for s in services if any(k and re.search(rf"\b{re.escape(k.lower())}\b", t) for k in [s["name"], *s.get("aliases", [])])), None)
    day, window, at = parse_day(t, today), parse_window(t), parse_time(t)
    if svc or day or window or at:
        greeted = smalltalk.detect(t)[0]
        if greeted and greeted.startswith("good") and not (svc or day or at):
            return Extraction(intent="greeting", language=lang)
        return Extraction(intent="book", service=svc, date=day.isoformat() if day else None, window=window,
                          time=at.strftime("%H:%M") if at else None, language=lang)
    if smalltalk.detect(t) != (None, False):
        return Extraction(intent="greeting", language=lang)
    return Extraction(intent="other", language=lang)


_BREAKER: dict[str, dict] = {}


def _breaker_open(label: str, settings) -> bool:
    b = _BREAKER.get(label)
    if not b:
        return False
    import time as _t
    if _t.monotonic() < b["rest_until"]:
        return True
    return False


def _breaker_note(label: str, ok: bool, settings) -> None:
    import time as _t
    trips = max(1, int(getattr(settings, "llm_breaker_trips", 3) or 3))
    cooldown = max(1, int(getattr(settings, "llm_breaker_cooldown_s", 60) or 60))
    b = _BREAKER.setdefault(label, {"fails": 0, "rest_until": 0.0})
    if ok:
        b["fails"] = 0
        b["rest_until"] = 0.0
    else:
        b["fails"] += 1
        if b["fails"] >= trips:
            b["rest_until"] = _t.monotonic() + cooldown
            log.error("model backend %s tripped: resting %ds after %d failures", label, cooldown, b["fails"])


_REASONING = re.compile(r"<think>.*?</think>", re.S | re.I)


def strip_reasoning(content: str) -> str:
    """Reasoning text must never reach a customer or the JSON parser."""
    return _REASONING.sub("", content or "").strip()


class Extractor:
    def __init__(self, http: httpx.AsyncClient, settings, budget=None, pool=None, redis=None):
        self.http, self.s, self.budget, self.pool, self.redis = http, settings, budget, pool, redis

    def endpoints(self) -> list[tuple[str, str, str, int]]:
        primary = getattr(self.s, "llm_backend", "") or "primary"
        e = [(primary, self.s.llm_base_url, self.s.llm_api_key, self.s.llm_model, 2)]
        if self.s.llm_fallback_base_url and self.s.llm_fallback_model:
            e.append((getattr(self.s, "llm_fallback_backend", "") or primary,
                      self.s.llm_fallback_base_url, self.s.llm_fallback_api_key, self.s.llm_fallback_model, 1))
        return e

    async def _log_call(self, backend: str, model: str, ms: int, ok: bool, tokens_in: int | None = None, tokens_out: int | None = None) -> None:
        if self.pool is None:
            return
        try:
            await self.pool.execute(
                "INSERT INTO model_calls (backend, model, step, tokens_in, tokens_out, latency_ms, ok) VALUES ($1,$2,'extract',$3,$4,$5,$6)",
                backend, model, tokens_in, tokens_out, ms, ok)
        except Exception:
            log.warning("could not log a model call")

    def _body(self, model: str, text: str, today: date, services: list[str], state: str, history: list[dict] | None = None) -> dict:
        from app.agent.policy import load_policy

        body: dict = {"model": model, "messages": [
            {"role": self.s.llm_system_role, "content": build_prompt(
                today, services, state, history, load_policy(getattr(self.s, "agent_policy_path", None)))},
            {"role": "user", "content": f'Message: "{text[:500]}"'}]}
        if self.s.llm_reasoning_effort:
            body["reasoning_effort"] = self.s.llm_reasoning_effort
        if self.s.llm_json_mode == "json_object":
            body["response_format"] = {"type": "json_object"}
        elif self.s.llm_json_mode == "json_schema":
            body["response_format"] = {"type": "json_schema", "json_schema": {"name": "extraction", "schema": Extraction.model_json_schema()}}
        return body

    async def extract(self, text: str, *, today: date, services: list[dict], state: str, history: list[dict] | None = None,
                      salon_id: str | None = None) -> Result:
        names = [s["name"] for s in services]
        if self.budget is not None and not await self.budget.take("text"):
            return Result(sanitize(rule_extract(text, today, services, state), today), "keywords", degraded=True)
        if salon_id and self.redis is not None and not await RateLimiter(self.redis, self.s).llm_allowed(salon_id):
            log.warning("salon %s hit its daily model allowance; answering from keyword rules", salon_id)
            return Result(sanitize(rule_extract(text, today, services, state), today), "keywords", degraded=True)
        for backend, base, key, model, attempts in self.endpoints():
            if _breaker_open(backend, self.s):
                log.warning("model backend %s resting after failures; trying the next one", backend)
                continue
            for attempt in range(attempts):
                t0 = clock.perf_counter()
                try:
                    r = await self.http.post(base.rstrip("/") + "/chat/completions", json=self._body(model, text, today, names, state, history),
                                             headers={"Authorization": f"Bearer {key}"}, timeout=self.s.llm_timeout_s)
                    r.raise_for_status()
                    body = r.json()
                    content = strip_reasoning(body["choices"][0]["message"]["content"])
                    ex = sanitize(_coerce(_json_from(content)), today)
                    ms = int((clock.perf_counter() - t0) * 1000)
                    usage = body.get("usage") or {}
                    _breaker_note(backend, True, self.s)
                    await self._log_call(backend, model, ms, True, usage.get("prompt_tokens"), usage.get("completion_tokens"))
                    return Result(ex, "model", model, ms)
                except (httpx.HTTPError, KeyError, IndexError, ValueError, ValidationError) as e:
                    status = e.response.status_code if isinstance(e, httpx.HTTPStatusError) and e.response is not None else None
                    kind = _failure_kind(e, status)
                    if kind == "auth":
                        log.error("model %s rejected our API key (%s): check the key for backend %s", model, status, backend)
                    else:
                        log.warning("model %s attempt %s failed: %s (%s)", model, attempt + 1, e.__class__.__name__, kind)
                    _breaker_note(backend, False, self.s)
                    await self._log_call(backend, model, int((clock.perf_counter() - t0) * 1000), False)
        return Result(sanitize(rule_extract(text, today, services, state), today), "keywords", degraded=True)
