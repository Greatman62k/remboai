"""The model writes replies for unlinked chats; code retains scope and routing control.

This assistant has no tools or business data. Registration and identity actions
stay in the router, and explicit out-of-scope requests never reach the model.
"""
from __future__ import annotations

import hashlib
import json
import logging
import re
from datetime import datetime, timezone

from app import guard, lexicon, smalltalk
from app.agent.grounding import Facts
from app.booking_flow import save_stranger
from app.channels import outbound
from app.channels.whatsapp import mask
from app.messages import t
from app.ratelimit import RateLimiter

from . import _trace, drive, enabled
from .policy import PolicyError, bare_request_is_out_of_scope, load_policy, matches_any, render_assistant_prompt

log = logging.getLogger("rembo.agent.bare")
_LEXICON = lexicon.load()
_PRODUCT_TERMS = _LEXICON.pattern(*(lexicon.STRONG + lexicon.SPECIFIC))
_BOOKING_FOLLOWUPS = _LEXICON.pattern("booking_followups")


def _request_is_out_of_scope(policy: dict, text: str, *, has_context: bool = False) -> bool:
    """Use explicit blocks, the shared salon lexicon, and context-only booking follow-ups."""
    rules = policy["bare_assistant"]
    if matches_any(rules["out_of_scope_patterns"], text):
        return True
    if (smalltalk.opener(text) is not None or smalltalk.thanks(text) is not None
            or smalltalk.asks_help(text)):
        return False
    if _PRODUCT_TERMS.search(text):
        return False
    if has_context and _BOOKING_FOLLOWUPS.search(text):
        return False
    return bare_request_is_out_of_scope(policy, text, has_context=has_context)


def _history(rows: list[dict], current_text: str, policy: dict) -> tuple[list[dict], bool]:
    """Keep only safe, allowed user/assistant pairs and report usable product context."""
    previous = list(rows or [])
    if previous and isinstance(previous[-1], dict):
        last = previous[-1]
        last_role = "assistant" if last.get("role") == "assistant" else "user"
        last_text = str(last.get("text") or last.get("content") or "").strip()
        if last_role == "user" and last_text == current_text[:500]:
            previous.pop()

    rules = policy["bare_assistant"]
    messages: list[dict] = []
    context = False
    pending_user: str | None = None
    for row in previous[-24:]:
        if not isinstance(row, dict):
            continue
        role = row.get("role")
        text = str(row.get("text") or row.get("content") or "").strip()
        if not text:
            continue
        if role != "assistant":
            if pending_user is not None:
                context = False
            pending_user = None
            if guard.injection(text) or _request_is_out_of_scope(
                policy, text, has_context=context
            ):
                context = False
                continue
            pending_user = text[:500]
            continue

        if pending_user is None:
            continue
        if matches_any(rules["unsafe_reply_patterns"], text) or matches_any(
            rules["out_of_scope_patterns"], text
        ):
            pending_user = None
            context = False
            continue
        messages.extend((
            {"role": "user", "content": pending_user},
            {"role": "assistant", "content": text[:500]},
        ))
        pending_user = None
        context = True

    messages = messages[-12:]
    while messages and messages[0]["role"] != "user":
        messages.pop(0)
    return messages, context


def _clean(text: str) -> str:
    body = re.sub(r"[ \t]*—[ \t]*", ", ", text or "")
    body = re.sub(r"[ \t]*–[ \t]*", "-", body)
    body = re.sub(r"(?m)^[ \t]{0,3}#{1,6}[ \t]+", "", body)
    body = body.replace("`", "").strip()
    return body


class _BareToolbox:
    def __init__(self, facts: Facts):
        self.facts = facts
        self.outcomes = []

    async def run(self, _name: str, _args: dict) -> dict:
        return {"error": "this assistant has no tools"}


async def turn(ctx, msg, prior_state: dict, history: list[dict]) -> bool:
    """Send one model answer, or one contextual fallback, for an unlinked chat turn."""
    if not enabled(ctx) or msg.kind != "text" or not (msg.text or "").strip():
        return False

    mid = outbound.current_message_id()
    if mid:
        done = await ctx.pool.fetchrow(
            "SELECT final_text, state_after FROM agent_turns WHERE message_id=$1 AND final_text IS NOT NULL", mid)
        if done is not None:
            saved = done["state_after"]
            if isinstance(saved, str):
                try:
                    saved = json.loads(saved)
                except ValueError:
                    saved = None
            if isinstance(saved, dict):
                await save_stranger(ctx, msg.sender, saved)
            await outbound.send_plain(ctx, msg.sender, done["final_text"])
            return True

    text = guard.clean(msg.text or "")[:1000]
    lang = smalltalk.language(text, "en")
    try:
        policy = load_policy(getattr(ctx.settings, "agent_policy_path", None))
    except PolicyError:
        log.exception("bare assistant policy is unavailable; leaving turn to the established fallback")
        return False
    configured = policy["bare_assistant"]
    blocked = guard.injection(text)
    messages, has_context = _history(history, text, policy)
    scope_redirect = not blocked and _request_is_out_of_scope(
        policy, text, has_context=has_context
    )
    limit = int(getattr(ctx.settings, "bare_llm_daily_per_sender", 30))
    scope = hashlib.sha256(msg.sender.encode("utf-8")).hexdigest()[:24]
    if blocked:
        await guard.strike(ctx.pool, msg.sender, "bare_agent_injection")
    rate_limited = False
    if not blocked and not scope_redirect and ctx.redis is not None:
        rate_limited = not await RateLimiter(ctx.redis, ctx.settings).llm_allowed(
            scope, kind="bare", limit=limit
        )
    allowed = not blocked and not scope_redirect and not rate_limited
    if allowed:
        messages.append({"role": "user", "content": text[:500]})

    system = render_assistant_prompt(policy, "bare_assistant")
    facts = Facts()
    facts.add(system)
    toolbox = _BareToolbox(facts)
    facts.add_user(*(item["content"] for item in messages if item["role"] == "user"))
    try:
        await ctx.wa.typing(msg.sender)
    except Exception:
        pass

    run = None
    model_error = False
    if allowed:
        try:
            run = await drive(ctx, system, messages, toolbox, [], policy["repair_prompts"]["bare_assistant"])
        except Exception:
            model_error = True
            log.exception("bare assistant failed; using the configured model failure reply")

    proposed = _clean(run.lead) if run and run.lead else ""
    unsafe_reply = bool(proposed and (
        matches_any(configured["unsafe_reply_patterns"], proposed)
        or matches_any(configured["out_of_scope_patterns"], proposed)
    ))
    if blocked or scope_redirect or unsafe_reply:
        reply_key = "out_of_scope_reply"
    elif rate_limited:
        reply_key = "rate_limited_reply"
    elif model_error or (run and run.unavailable):
        reply_key = "model_unavailable_reply"
    elif not proposed or len(proposed) > 500 or (run and run.problems):
        reply_key = "model_rejected_reply"
    else:
        reply_key = ""

    if reply_key:
        replies = configured.get(reply_key) or {}
        body = replies.get("sw" if lang == "sw" else "en") or t(lang, "main_assistant_fallback")
    else:
        body = proposed
    stamp = datetime.now(timezone.utc).isoformat()
    after = {**prior_state, "history": [*history, {"role": "assistant", "text": body[:500], "ts": stamp}]}
    await save_stranger(ctx, msg.sender, after)
    if blocked:
        outcome = "bare_guardrail"
    elif scope_redirect:
        outcome = "bare_scope_redirect"
    elif unsafe_reply:
        outcome = "bare_output_guardrail"
    elif rate_limited:
        outcome = "bare_rate_limited"
    elif model_error or (run and run.unavailable):
        outcome = "bare_unavailable"
    elif run and run.lead and not run.problems and not reply_key:
        outcome = "bare_agent"
    elif run and run.problems:
        outcome = "bare_grounding_fallback"
    else:
        outcome = "bare_fallback"
    problems = ["bare_output_guardrail: out-of-scope answer or code was suppressed"] if unsafe_reply else []
    await _trace(ctx, mid=mid, salon={"id": None}, sender=mask(msg.sender), user_text=text,
                 steps=run.steps if run else [], final_text=body, buttons=None, state_after=after,
                 outcome=outcome, grounded=False if unsafe_reply or (run and run.problems) else (True if not reply_key and run else None),
                 problems=problems or (run.problems if run else []), model=run.first_model if run else None,
                 calls=run.calls if run else 0, ms=run.ms if run else 0)
    await outbound.send_plain(ctx, msg.sender, body)
    return True
