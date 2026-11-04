"""The Meta model runs the receptionist; code keeps it honest.

One customer message becomes one agent turn:
  1. The model is briefed with the business's verified facts and the conversation, and given tools (find_slots, reserve_slot, ...).
  2. It decides what to do, step by step. Each tool call is validated and executed by code against the real diary and money path.
  3. Verified action results go back to the model, whose customer-facing draft is checked against those facts before it is sent.
  4. If the model is down, out of budget, or still ungroundable after repair, code uses the verified outcome text or deterministic
     fallback. The receptionist never goes silent and never states something unchecked.

Every turn is written to `agent_turns`, so it can be inspected (/ops/agent-trace) and a retried message resends the same reply."""
from __future__ import annotations

import json
import logging
import time as clock
from dataclasses import dataclass, field
from datetime import datetime
from zoneinfo import ZoneInfo

from app import booking_flow as bf
from app import guard, payments, smalltalk
from app.channels import outbound
from app.channels.whatsapp import mask
from app.ratelimit import RateLimiter

from .client import AgentModel, AgentUnavailable, request_evidence
from .grounding import MAX_REPLY, check
from .policy import PolicyError, load_policy, with_tool_descriptions
from app.messages import ensure_help_hint
from .prompt import system_prompt
from .tools import TOOLS, Outcome, Toolbox

log = logging.getLogger("rembo.agent")
__all__ = ["AgentModel", "turn", "owner_turn", "enabled"]


def enabled(ctx) -> bool:
    return getattr(ctx, "agent", None) is not None and getattr(ctx.settings, "agent_mode", "off") == "on"


def _history_messages(history: list[dict]) -> list[dict]:
    msgs = [{"role": "assistant" if h.get("role") == "assistant" else "user", "content": (h.get("text") or "")[:500]}
            for h in history[-10:] if (h.get("text") or "").strip()]
    while msgs and msgs[0]["role"] != "user":
        msgs.pop(0)
    return msgs


def _jload(raw, default):
    if isinstance(raw, (dict, list)):
        return raw
    try:
        return json.loads(raw) if raw else default
    except ValueError:
        return default


async def _resend(ctx, salon: dict, chat, row) -> None:
    """A retried message whose turn already finished: restore its state and say exactly what was said before."""
    after = _jload(row["state_after"], None)
    if isinstance(after, dict):
        await bf._save(ctx, salon["id"], chat.to, after)
    buttons = [tuple(b) for b in _jload(row["buttons"], [])]
    if buttons:
        await chat.buttons(row["final_text"], buttons)
    else:
        await chat.text(row["final_text"])


async def _trace(ctx, *, mid, salon, sender, user_text, steps, final_text, buttons, state_after, outcome, grounded, problems, model, calls, ms, model_requests=None) -> None:
    try:
        await ctx.pool.execute(
            "INSERT INTO agent_turns (message_id, salon_id, sender, user_text, steps, final_text, buttons, state_after, outcome, grounded, problems, backend, model, model_calls, ms, model_requests) "
            "VALUES ($1,$2::uuid,$3,$4,$5::jsonb,$6,$7::jsonb,$8::jsonb,$9,$10,$11::jsonb,$12,$13,$14,$15,$16::jsonb) "
            "ON CONFLICT (message_id) DO UPDATE SET steps=EXCLUDED.steps, final_text=EXCLUDED.final_text, buttons=EXCLUDED.buttons, "
            "state_after=EXCLUDED.state_after, outcome=EXCLUDED.outcome, grounded=EXCLUDED.grounded, problems=EXCLUDED.problems, model_calls=EXCLUDED.model_calls, ms=EXCLUDED.ms, model_requests=EXCLUDED.model_requests",
            mid, salon["id"], sender, user_text[:500], json.dumps(steps, ensure_ascii=False, default=str), final_text,
            json.dumps(buttons) if buttons else None, json.dumps(state_after, ensure_ascii=False, default=str) if state_after else None,
            outcome, grounded, json.dumps(problems), model[0] if model else None, model[1] if model else None, calls, ms,
            json.dumps(model_requests or [], ensure_ascii=False, default=str))
    except Exception:
        log.exception("could not write the agent trace")


def _compose(outcomes: list[Outcome], lead: str) -> tuple[str, list[tuple[str, str]]]:
    """Keep the model's natural reply and attach verified wording for binding outcomes."""
    buttons = next((o.buttons for o in reversed(outcomes) if o.buttons), [])
    if lead:
        body = lead.strip()
        prior_to_offer = []
        later_outcomes = []
        seen_offer = False
        for outcome in outcomes:
            if outcome.kind == "offered":
                seen_offer = True
            elif outcome.text and seen_offer:
                later_outcomes.append(outcome)
            elif outcome.text:
                prior_to_offer.append(outcome)
        # A tool may cancel the current booking and then offer alternatives.
        # State the completed cancellation before the model's offer summary.
        prefix = [o for o in prior_to_offer if any(x.kind == "offered" for x in outcomes)]
        suffix = [o for o in prior_to_offer if o not in prefix] + later_outcomes
        for outcome in prefix:
            if outcome.text.casefold() not in body.casefold():
                body = outcome.text + "\n\n" + body
        for outcome in suffix:
            # Offers use verified buttons; other outcomes need the code-written
            # facts and next steps alongside the model's natural wording.
            if outcome.text.casefold() not in body.casefold():
                body += "\n\n" + outcome.text
        return body, buttons
    fallback = "\n\n".join(outcome.text for outcome in outcomes if outcome.text)
    return fallback, buttons


@dataclass
class Run:
    lead: str = ""
    problems: list[str] = field(default_factory=list)
    steps: list[dict] = field(default_factory=list)
    unavailable: bool = False
    first_model: tuple[str, str] | None = None
    calls: int = 0
    ms: int = 0
    model_requests: list[dict] = field(default_factory=list)


async def drive(ctx, system: str, messages: list[dict], toolbox, tools: list[dict], repair_prompt: str) -> Run:
    """The loop shared by the customer and owner agents: the model picks tools until it has an answer, then the answer is checked."""
    s, model = ctx.settings, ctx.agent
    run = Run()
    t0 = clock.perf_counter()
    calls_before = model.calls_made
    final_text: str | None = None
    try:
        for _ in range(max(1, int(s.agent_max_steps))):
            if clock.perf_counter() - t0 > float(s.agent_turn_timeout_s):
                log.warning("agent turn out of time after %d steps", len(run.steps))
                break
            step = await model.step(system, messages, tools)
            run.first_model = run.first_model or (step.backend, step.model)
            run.model_requests.append(request_evidence(step))
            if not step.calls:
                final_text = step.text
                break
            messages.append(step.assistant)
            for i, call in enumerate(step.calls):
                t1 = clock.perf_counter()
                if i >= 3:
                    result = {"error": "too_many_calls_at_once"}
                elif call.bad_args:
                    result = {"error": "bad_arguments", "hint": "arguments must be a JSON object"}
                else:
                    result = await toolbox.run(call.name, call.args)
                packed = json.dumps(result, ensure_ascii=False, default=str)
                run.steps.append({"tool": call.name, "args": call.args, "result": packed[:600], "ms": int((clock.perf_counter() - t1) * 1000), "model_ms": step.ms})
                messages.append({"role": "tool", "tool_call_id": call.id, "content": packed})
        if final_text is not None:
            limit = 300 if any(o.mutating for o in toolbox.outcomes) else MAX_REPLY
            run.problems = check(final_text, toolbox.facts) + (["too long for a reply that follows a confirmation"] if len(final_text) > limit else [])
            if run.problems:
                log.warning("agent reply rejected (%s); asking once more", "; ".join(run.problems))
                messages.append({"role": "assistant", "content": final_text})
                messages.append({"role": "user", "content": repair_prompt.format(problems="; ".join(run.problems))})
                try:
                    again = await model.step(system, messages, [])
                    final_text = again.text
                    run.problems = check(final_text, toolbox.facts) + (["too long for a reply that follows a confirmation"] if len(final_text) > limit else [])
                except AgentUnavailable:
                    final_text = ""
            run.lead = (final_text or "").strip() if not run.problems else ""
    except AgentUnavailable as e:
        run.unavailable, run.problems, run.lead = True, [f"model unavailable: {e}"], ""
        log.warning("agent unavailable: %s", e)
    run.calls = model.calls_made - calls_before
    run.ms = int((clock.perf_counter() - t0) * 1000)
    return run


async def turn(ctx, salon: dict, client: dict, st: dict, services: list[dict], tz: ZoneInfo, now: datetime, chat, lang: str, msg) -> bool:
    """Handle one customer message with the model. False means nothing was done and the keyword flow should take the message."""
    if not enabled(ctx):
        return False
    s = ctx.settings
    mid = outbound.current_message_id()
    if mid:
        done = await ctx.pool.fetchrow("SELECT final_text, buttons, state_after FROM agent_turns WHERE message_id=$1 AND final_text IS NOT NULL", mid)
        if done is not None:
            log.info("agent turn for %s already finished: resending the same reply", mid)
            await _resend(ctx, salon, chat, done)
            return True
    if ctx.redis is not None and not await RateLimiter(ctx.redis, s).llm_allowed(salon["id"]):
        log.warning("salon %s hit its daily model allowance: agent skipped", salon["id"])
        return False
    try:
        await ctx.wa.typing(chat.to)
    except Exception:
        pass
    text = msg.text or ""
    rows = await ctx.pool.fetch(
        "SELECT ss.service_id::text AS sid, array_agg(st.name ORDER BY st.name) AS names FROM staff_services ss JOIN staff st ON st.id=ss.staff_id "
        "WHERE st.active AND ss.service_id = ANY($1::uuid[]) GROUP BY ss.service_id", [sv["id"] for sv in services])
    staff_by_service = {r["sid"]: list(r["names"]) for r in rows}
    acct = await payments.account(ctx.pool, salon["id"])
    deposit = int(salon.get("deposit_kes") or 0)
    if deposit <= 0:
        deposit_line = "Deposit: none is required."
    elif acct is None:
        deposit_line = f"Deposit: KES {deposit} is configured, but the business has no payment destination. Do not offer or confirm a deposit booking."
    elif acct.get("manual"):
        deposit_line = (f"Deposit: KES {deposit} (never more than the service price). Customers pay manually to "
                        f"{payments.destination_label(acct)}. The owner verifies each payment claim; RemboAI does not initiate or verify these transfers.")
    else:
        deposit_line = (f"Deposit: KES {deposit} (never more than the service price). M-PESA Daraja sends an STK prompt to the customer's chosen M-PESA phone when a slot is held, "
                        f"and verifies payment by callback or status query to {payments.destination_label(acct)}.")
    toolbox = Toolbox(ctx, salon, client, st, services, tz, now, lang, chat.to, text)
    view = dict(toolbox.st)
    view["_offered_view"] = toolbox.view_slots()
    try:
        policy = load_policy(getattr(s, "agent_policy_path", None))
    except PolicyError:
        log.exception("booking receptionist policy is unavailable; using the established conversation flow")
        return False
    first_reply = not any(h.get("role") == "assistant" for h in st.get("history", []) if isinstance(h, dict))
    help_example = sorted(services, key=lambda service: service["name"])[0]["name"] if services else "service"
    system = system_prompt(salon=salon, services=services, staff_by_service=staff_by_service, now=now, st=view, deposit_line=deposit_line,
                           channel="Telegram" if chat.to.startswith("tg:") else "WhatsApp", lang_hint="Swahili or Sheng" if lang == "sw" else "English",
                           policy=policy, first_reply=first_reply, help_example=help_example)
    tools = [] if smalltalk.asks_help(text) else with_tool_descriptions(TOOLS, policy, "booking_receptionist")
    facts = toolbox.facts
    facts.add(system, *[(h.get("text") or "") for h in st.get("history", [])[-10:] if h.get("role") == "assistant"])
    facts.add_user(*[(h.get("text") or "") for h in st.get("history", [])[-10:] if h.get("role") != "assistant"], text)
    facts.deposit_none = not (acct and deposit > 0)
    facts.prices.update(int(sv["price_kes"]) for sv in services)
    facts.prices.update(payments.deposit_amount(salon, sv) for sv in services)
    facts.prices.add(deposit)
    messages = _history_messages(st.get("history", []))
    if not messages or messages[-1]["role"] != "user":
        messages.append({"role": "user", "content": text[:500]})
    run = await drive(ctx, system, messages, toolbox, tools,
                      policy["repair_prompts"]["booking_receptionist"])
    outcomes = toolbox.outcomes
    if not outcomes and not run.lead:
        await _trace(ctx, mid=mid, salon=salon, sender=mask(chat.to), user_text=text, steps=run.steps, final_text=None, buttons=None, state_after=None,
                     outcome="unavailable" if run.unavailable else "keyword_flow", grounded=False if run.problems else None, problems=run.problems,
                     model=run.first_model, calls=run.calls, ms=run.ms, model_requests=run.model_requests)
        return False
    body, buttons = _compose(outcomes, run.lead)
    if not body:
        return False
    if first_reply:
        body = ensure_help_hint(body, lang)
    stamp = datetime.now(ZoneInfo("UTC")).isoformat()
    after = {**toolbox.st, "history": (toolbox.base_history + [{"role": "assistant", "text": body, "ts": stamp}])[-guard.MAX_HISTORY * 2:]}
    await _trace(ctx, mid=mid, salon=salon, sender=mask(chat.to), user_text=text, steps=run.steps, final_text=body, buttons=buttons, state_after=after,
                 outcome="agent" if run.lead else "templates", grounded=not run.problems, problems=run.problems,
                 model=run.first_model, calls=run.calls, ms=run.ms, model_requests=run.model_requests)
    if buttons:
        await bf._reply_buttons(ctx, salon["id"], chat.to, toolbox.st, chat, body, buttons)
    else:
        await bf._reply(ctx, salon["id"], chat.to, toolbox.st, chat, body)
    log.info("agent turn: %d model calls, tools=%s, %s, %d ms", run.calls, [x["tool"] for x in run.steps], "model text" if run.lead else "templates", run.ms)
    return True


async def owner_turn(ctx, salon: dict, to: str, text: str, tz: ZoneInfo, now: datetime, chat) -> bool:
    """The owner talks to the diary in plain words. False means the model could not help and the message goes on as before."""
    from .admin import ADMIN_TOOLS, AdminToolbox, admin_prompt
    if not enabled(ctx):
        return False
    raw = await ctx.pool.fetchval("SELECT state FROM conversations WHERE salon_id=$1::uuid AND wa_id=$2", salon["id"], to)
    saved = _jload(raw, {}) or {}
    history = saved.get("owner_history", []) if isinstance(saved, dict) else []
    history_at = saved.get("owner_history_at") if isinstance(saved, dict) else None
    if isinstance(history, list) and history and history_at:
        try:
            stamp = datetime.fromisoformat(history_at) if isinstance(history_at, str) else history_at
            if stamp.tzinfo is None:
                stamp = stamp.replace(tzinfo=ZoneInfo("UTC"))
            age = (datetime.now(ZoneInfo("UTC")) - stamp.astimezone(ZoneInfo("UTC"))).total_seconds()
            if not 0 <= age <= 600:
                history = []
        except (TypeError, ValueError):
            history = []
    elif not history_at:
        history = []
    try:
        policy = load_policy(getattr(ctx.settings, "agent_policy_path", None))
    except PolicyError:
        log.exception("business owner assistant policy is unavailable; using the established conversation flow")
        return False
    mid = outbound.current_message_id()
    if mid:
        done = await ctx.pool.fetchrow("SELECT final_text FROM agent_turns WHERE message_id=$1 AND final_text IS NOT NULL", mid)
        if done is not None:
            await chat.text(done["final_text"])
            return True
    try:
        await ctx.wa.typing(to)
    except Exception:
        pass
    toolbox = AdminToolbox(ctx, salon, to, tz, now, text)
    services = await bf._catalog(ctx, salon["id"])
    lang = smalltalk.language(text, "en")
    hint_at = saved.get("owner_help_hint_at") if isinstance(saved, dict) else None
    recent_hint = False
    if hint_at:
        try:
            stamp = datetime.fromisoformat(hint_at) if isinstance(hint_at, str) else hint_at
            if stamp.tzinfo is None:
                stamp = stamp.replace(tzinfo=ZoneInfo("UTC"))
            recent_hint = 0 <= (datetime.now(ZoneInfo("UTC")) - stamp.astimezone(ZoneInfo("UTC"))).total_seconds() <= 600
        except (TypeError, ValueError):
            recent_hint = False
    first_reply = not history and not recent_hint
    system = admin_prompt(salon, now, policy, services, lang=lang, first_reply=first_reply)
    toolbox.facts.prices.update(int(service["price_kes"]) for service in services)
    toolbox.facts.prices.add(int(salon.get("deposit_kes") or 0))
    tools = [] if smalltalk.asks_help(text) else with_tool_descriptions(ADMIN_TOOLS, policy, "salon_owner_assistant")
    history = history if isinstance(history, list) else []
    messages = [*history[-6:], {"role": "user", "content": text[:500]}]
    toolbox.facts.add(system)
    toolbox.facts.add_user(text, *[h["content"] for h in history[-6:] if h["role"] == "user"])
    run = await drive(ctx, system, messages, toolbox, tools,
                      policy["repair_prompts"]["salon_owner_assistant"])
    body, _ = _compose(toolbox.outcomes, run.lead)
    if not body:
        await _trace(ctx, mid=mid, salon=salon, sender="owner", user_text=text, steps=run.steps, final_text=None, buttons=None, state_after=None,
                     outcome="unavailable" if run.unavailable else "keyword_flow", grounded=False if run.problems else None, problems=run.problems,
                     model=run.first_model, calls=run.calls, ms=run.ms, model_requests=run.model_requests)
        return False
    if first_reply:
        body = ensure_help_hint(body, lang)
    await _trace(ctx, mid=mid, salon=salon, sender="owner", user_text=text, steps=run.steps, final_text=body, buttons=None, state_after=None,
                 outcome="agent" if run.lead else "templates", grounded=not run.problems, problems=run.problems,
                 model=run.first_model, calls=run.calls, ms=run.ms, model_requests=run.model_requests)
    new_hist = [*history[-4:], {"role": "user", "content": text[:300]}, {"role": "assistant", "content": body[:500]}]
    history_at = datetime.now(ZoneInfo("UTC")).isoformat()
    await ctx.pool.execute(
        "INSERT INTO conversations (salon_id, wa_id, state) VALUES ($1::uuid,$2,$3::jsonb) ON CONFLICT (salon_id, wa_id) DO UPDATE SET state="
        "COALESCE(conversations.state,'{}'::jsonb) || EXCLUDED.state, updated_at=now()", salon["id"], to,
        json.dumps({"owner_history": new_hist, "owner_history_at": history_at,
                    "owner_help_hint_sent": True, "owner_help_hint_at": history_at}))
    await chat.text(body)
    return True
