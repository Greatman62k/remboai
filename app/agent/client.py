"""One step of the agent conversation with a Meta model.

Two wire formats, one interface:
  tools - native function calling (OpenAI-style `tools` / `tool_calls`), which Meta's Model API documents for Muse Spark.
  json  - the same loop with the tool call written as one JSON object, for hosts whose open-weights deployment
          has no tool parser. `auto` tries tools first and drops to json for that backend if the host refuses `tools`.

Endpoints, circuit breaker and daily budget are shared with the extraction client, so a model outage is one outage."""
from __future__ import annotations

import json
import logging
import re
import time as clock
from dataclasses import dataclass, field

import httpx

from app.ai.llm import _breaker_note, _breaker_open, strip_reasoning
from app.agent.policy import load_policy

log = logging.getLogger("rembo.agent")
_NO_TOOLS: set[str] = set()      # backends that refused native tool calling in this process


class AgentUnavailable(Exception):
    """No backend could take this step (all down, resting, rate-limited, or the daily budget is spent)."""


@dataclass
class Call:
    id: str
    name: str
    args: dict
    bad_args: bool = False


@dataclass
class Step:
    text: str = ""
    calls: list[Call] = field(default_factory=list)
    assistant: dict = field(default_factory=dict)   # the message to append to the transcript
    backend: str = ""
    model: str = ""
    ms: int = 0
    protocol: str = "tools"


def _json_object(content: str) -> dict:
    content = re.sub(r"```(?:json)?", "", content or "").strip()
    m = re.search(r"\{.*\}", content, re.S)
    if not m:
        raise ValueError("no JSON object")
    return json.loads(m.group(0))


def json_protocol_note(tools: list[dict], policy: dict | None = None) -> str:
    protocol = (policy or load_policy())["tool_protocol"]
    if not tools:
        return protocol["reply_without_tools"]
    lines = list(protocol["tool_steps"])
    for t in tools:
        f = t["function"]
        lines.append(f'- {f["name"]}: {f["description"]} args schema: {json.dumps(f["parameters"])}')
    return "\n".join(lines)


def to_json_transcript(messages: list[dict]) -> list[dict]:
    """The tools-format transcript, rewritten for a model that has no native tool calling."""
    out: list[dict] = []
    names: dict[str, str] = {}
    for m in messages:
        if m["role"] == "assistant" and m.get("tool_calls"):
            calls = []
            for c in m["tool_calls"]:
                names[c["id"]] = c["function"]["name"]
                calls.append({"tool": c["function"]["name"], "args": _loads(c["function"].get("arguments"))})
            out.append({"role": "assistant", "content": json.dumps(calls[0] if len(calls) == 1 else {"tools": calls})})
        elif m["role"] == "tool":
            out.append({"role": "user", "content": f"TOOL RESULT ({names.get(m.get('tool_call_id'), 'tool')}): {m['content']}"})
        elif m["role"] == "assistant":
            out.append({"role": "assistant", "content": json.dumps({"reply": m.get("content") or ""})})
        else:
            out.append({"role": m["role"], "content": m.get("content") or ""})
    return out


def _loads(raw) -> dict:
    if isinstance(raw, dict):
        return raw
    try:
        v = json.loads(raw or "{}")
        return v if isinstance(v, dict) else {}
    except ValueError:
        return {}


class AgentModel:
    def __init__(self, http: httpx.AsyncClient, settings, budget=None, endpoints=None, pool=None):
        self.http, self.s, self.budget, self.endpoints, self.pool = http, settings, budget, endpoints, pool
        self.calls_made = 0

    def _protocol(self, backend: str) -> str:
        want = (getattr(self.s, "agent_protocol", "auto") or "auto").lower()
        if want == "json" or (want == "auto" and backend in _NO_TOOLS):
            return "json"
        return "tools"

    def _body(self, model: str, system: str, messages: list[dict], tools: list[dict], protocol: str) -> dict:
        role = self.s.llm_system_role
        if protocol == "json":
            policy = load_policy(getattr(self.s, "agent_policy_path", None))
            body = {"model": model, "messages": [{"role": role, "content": system + "\n\n" + json_protocol_note(tools, policy)}, *to_json_transcript(messages)]}
        else:
            body = {"model": model, "messages": [{"role": role, "content": system}, *messages]}
            if tools:
                body["tools"] = tools
                body["tool_choice"] = "auto"
        effort = getattr(self.s, "agent_reasoning_effort", "") or ""
        if effort:
            body["reasoning_effort"] = effort
        return body

    async def _log(self, backend: str, model: str, ms: int, ok: bool, usage: dict | None = None) -> None:
        if self.pool is None:
            return
        try:
            await self.pool.execute(
                "INSERT INTO model_calls (backend, model, step, tokens_in, tokens_out, latency_ms, ok) VALUES ($1,$2,'agent',$3,$4,$5,$6)",
                backend, model, (usage or {}).get("prompt_tokens"), (usage or {}).get("completion_tokens"), ms, ok)
        except Exception:
            log.warning("could not log an agent model call")

    async def step(self, system: str, messages: list[dict], tools: list[dict]) -> Step:
        if self.budget is not None and not await self.budget.take("text"):
            raise AgentUnavailable("daily model budget spent")
        last: Exception | None = None
        for backend, base, key, model, _attempts in self.endpoints():
            if _breaker_open(backend, self.s):
                continue
            protocol = self._protocol(backend)
            for _ in range(2):          # a second pass only when this host refuses native tools
                t0 = clock.perf_counter()
                try:
                    r = await self.http.post(base.rstrip("/") + "/chat/completions", json=self._body(model, system, messages, tools, protocol),
                                             headers={"Authorization": f"Bearer {key}"}, timeout=self.s.agent_call_timeout_s)
                    if r.status_code in (400, 404, 422) and protocol == "tools" and tools and (getattr(self.s, "agent_protocol", "auto") or "auto") == "auto":
                        log.warning("backend %s refused native tool calling (%s); using JSON steps for it", backend, r.status_code)
                        _NO_TOOLS.add(backend)
                        protocol = "json"
                        continue
                    r.raise_for_status()
                    body = r.json()
                    ms = int((clock.perf_counter() - t0) * 1000)
                    step = self._parse(body["choices"][0]["message"], protocol)
                    step.backend, step.model, step.ms, step.protocol = backend, model, ms, protocol
                    _breaker_note(backend, True, self.s)
                    await self._log(backend, model, ms, True, body.get("usage"))
                    self.calls_made += 1
                    return step
                except (httpx.HTTPError, KeyError, IndexError, ValueError) as e:
                    last = e
                    log.warning("agent step on %s failed: %s", backend, e.__class__.__name__)
                    _breaker_note(backend, False, self.s)
                    await self._log(backend, model, int((clock.perf_counter() - t0) * 1000), False)
                    break
        raise AgentUnavailable(f"no backend answered ({last.__class__.__name__ if last else 'all resting'})")

    def _parse(self, msg: dict, protocol: str) -> Step:
        content = strip_reasoning(msg.get("content") or "")
        if not msg.get("tool_calls") and (protocol == "json" or content.lstrip().startswith("{")):
            raw = content
            try:
                obj = _json_object(raw)
            except ValueError:
                return Step(text=raw, assistant={"role": "assistant", "content": raw})     # plain words: treat as the reply
            if protocol != "json" and "tool" not in obj:
                text = str(obj.get("reply") or obj.get("say") or raw).strip()
                return Step(text=text, assistant={"role": "assistant", "content": text})
            if "tool" in obj:
                call = Call("call_" + str(self.calls_made), str(obj.get("tool")), obj.get("args") if isinstance(obj.get("args"), dict) else {})
                return Step(calls=[call], assistant={"role": "assistant", "content": None, "tool_calls": [
                    {"id": call.id, "type": "function", "function": {"name": call.name, "arguments": json.dumps(call.args)}}]})
            text = str(obj.get("reply") or obj.get("say") or "").strip()
            return Step(text=text, assistant={"role": "assistant", "content": text})
        calls = []
        for i, c in enumerate(msg.get("tool_calls") or []):
            fn = c.get("function") or {}
            raw_args = fn.get("arguments")
            try:
                args = json.loads(raw_args) if isinstance(raw_args, str) and raw_args.strip() else (raw_args or {})
                bad = not isinstance(args, dict)
            except ValueError:
                args, bad = {}, True
            calls.append(Call(c.get("id") or f"call_{self.calls_made}_{i}", str(fn.get("name") or ""), args if isinstance(args, dict) else {}, bad))
        text = strip_reasoning(msg.get("content") or "")
        assistant: dict = {"role": "assistant", "content": text or None}
        if calls:
            assistant["tool_calls"] = [{"id": c.id, "type": "function", "function": {"name": c.name, "arguments": json.dumps(c.args)}} for c in calls]
        return Step(text=text, calls=calls, assistant=assistant)
