"""Read-only conversational assistant for authenticated RemboAI platform administrators."""
from __future__ import annotations

import json
import logging
import re
from datetime import datetime, timezone

from app import platform, smalltalk
from app.channels import outbound
from app.agent.grounding import Facts
from app.messages import ensure_help_hint, t
from app.agent.policy import PolicyError, load_policy, render_assistant_prompt, with_tool_descriptions
from app.agent.tools import _fn

log = logging.getLogger("rembo.agent.platform")

PLATFORM_TOOLS = [
    _fn("list_businesses", {}),
    _fn("platform_totals", {}),
    _fn("business_status", {"business": {"type": "string"}}, ["business"]),
]
PLATFORM_TOOL_NAMES = {item["function"]["name"] for item in PLATFORM_TOOLS}


def _clean_reply(text: str) -> str:
    """Normalize model output for plain-text chat channels such as Telegram and WhatsApp."""
    body = re.sub(r"[ \t]*—[ \t]*", ", ", text or "")
    body = re.sub(r"[ \t]*–[ \t]*", "-", body)
    body = re.sub(r"(?m)^[ \t]{0,3}[-*][ \t]+", "• ", body)
    body = re.sub(r"(?m)^[ \t]{0,3}#{1,6}[ \t]+", "", body)
    body = re.sub(r"(?<!\w)(\*\*|__)(.+?)\1(?!\w)", r"\2", body, flags=re.S)
    body = re.sub(r"(?<!\w)(\*|_)([^*_\n]+?)\1(?!\w)", r"\2", body)
    body = body.replace("`", "")
    body = re.sub(r"\n[ \t]*\n(?:[ \t]*\n)+", "\n\n", body)
    return body.strip()


def _fallback_reply(run, lang: str = "en") -> str:
    if run.unavailable:
        status = "Sina jibu la msaidizi kwa sasa." if lang == "sw" else "I couldn't get an assistant response just now."
    elif run.problems:
        status = "Singeweza kuthibitisha jibu salama kwa sasa." if lang == "sw" else "I couldn't verify a safe answer just now."
    else:
        status = "Singeweza kukamilisha jibu hilo kwa sasa." if lang == "sw" else "I couldn't complete that answer just now."
    return f"{status}\n\n{platform.help_text(lang)}"


def _system_prompt(policy: dict, lang: str, first_reply: bool) -> str:
    base = render_assistant_prompt(policy, "platform_admin")
    first = (f'\nFirst reply: end with "{t(lang, "help_hint")}" and keep the invitation brief.'
             if first_reply else "")
    return (
        f"{base}\n\nPLATFORM HELP REFERENCE, ENGLISH\n{platform.help_text('en')}"
        f"\n\nPLATFORM HELP REFERENCE, KISWAHILI\n{platform.help_text('sw')}"
        f"\nExplain commands in everyday language, with a short example, when asked what they do. "
        f"A question about a command does not authorize running it.{first}"
    )


class PlatformToolbox:
    def __init__(self, ctx):
        self.ctx = ctx
        self.facts = Facts()
        self.facts.reporting = True
        self.outcomes = []

    async def run(self, name: str, args: dict) -> dict:
        if name not in PLATFORM_TOOL_NAMES:
            return {"error": "unknown_tool"}
        try:
            result = await getattr(self, "t_" + name)(args if isinstance(args, dict) else {})
        except Exception:
            log.exception("platform assistant read-only tool failed: %s", name)
            result = {"error": "tool_failed"}
        self.facts.add(json.dumps(result, ensure_ascii=False, default=str))
        return result

    async def t_list_businesses(self, _args: dict) -> dict:
        rows = await self.ctx.pool.fetch(
            "SELECT s.name, s.slug, s.active, count(b.id)::int AS bookings, max(b.created_at) AS last_booking "
            "FROM salons s LEFT JOIN bookings b ON b.salon_id=s.id GROUP BY s.id ORDER BY s.created_at, s.id")
        now = datetime.now(timezone.utc)
        businesses = []
        for row in rows:
            last = row["last_booking"]
            age_days = None if last is None else max(0, int((now - last).total_seconds() // 86400))
            businesses.append({"name": row["name"], "slug": row["slug"], "active": row["active"],
                               "bookings": row["bookings"], "last_booking_age_days": age_days})
        return {"businesses": businesses}

    async def t_platform_totals(self, _args: dict) -> dict:
        row = await self.ctx.pool.fetchrow(
            "SELECT (SELECT count(*) FROM salons)::int AS businesses, "
            "(SELECT count(*) FROM salons WHERE active)::int AS active_businesses, "
            "(SELECT count(*) FROM bookings)::int AS bookings, "
            "(SELECT count(*) FROM clients)::int AS clients, "
            "(SELECT count(*) FROM payments WHERE status='paid')::int AS paid_deposits")
        return dict(row)

    async def t_business_status(self, args: dict) -> dict:
        rows = await platform._find(self.ctx.pool, str(args.get("business") or ""))
        if len(rows) != 1:
            return {"matches": [{"name": row["name"], "slug": row["slug"], "active": row["active"]} for row in rows],
                    "error": "no_match" if not rows else "ambiguous_match"}
        row = rows[0]
        activity = await self.ctx.pool.fetchrow(
            "SELECT count(*)::int AS bookings, max(created_at) AS last_booking FROM bookings WHERE salon_id=$1::uuid", row["id"])
        last = activity["last_booking"]
        age_days = None if last is None else max(0, int((datetime.now(timezone.utc) - last).total_seconds() // 86400))
        return {"name": row["name"], "slug": row["slug"], "active": row["active"],
                "bookings": activity["bookings"], "last_booking_age_days": age_days}


async def turn(ctx, msg) -> bool:
    """Answer one platform-admin message with grounded, read-only model assistance."""
    from app.agent import _trace, drive, enabled

    if not enabled(ctx) or msg.kind != "text" or not (msg.text or "").strip():
        return False
    mid = outbound.current_message_id()
    if mid:
        done = await ctx.pool.fetchrow(
            "SELECT final_text FROM agent_turns WHERE message_id=$1 AND final_text IS NOT NULL", mid)
        if done is not None:
            await outbound.send_plain(ctx, msg.sender, done["final_text"])
            return True
    try:
        await ctx.wa.typing(msg.sender)
    except Exception:
        pass

    state = await ctx.pool.fetchrow(
        "SELECT history FROM platform_agent_sessions WHERE sender=$1 AND updated_at > now() - interval '30 minutes'", msg.sender)
    history = state["history"] if state is not None else []
    if isinstance(history, str):
        try:
            history = json.loads(history)
        except ValueError:
            history = []
    if not isinstance(history, list):
        history = []
    messages = [item for item in history[-8:] if isinstance(item, dict) and item.get("role") in ("user", "assistant")]
    first_reply = not any(item.get("role") == "assistant" for item in messages)
    lang = smalltalk.language(msg.text or "", "en")
    messages.append({"role": "user", "content": (msg.text or "")[:1000]})

    toolbox = PlatformToolbox(ctx)
    try:
        policy = load_policy(getattr(ctx.settings, "agent_policy_path", None))
    except PolicyError:
        log.exception("platform assistant policy is unavailable")
        return False
    system = _system_prompt(policy, lang, first_reply)
    tools = with_tool_descriptions(PLATFORM_TOOLS, policy, "platform_admin")
    toolbox.facts.add(system)
    toolbox.facts.add_user(*[item.get("content", "") for item in messages if item.get("role") == "user"])
    run = await drive(ctx, system, messages.copy(), toolbox, tools,
                      policy["repair_prompts"]["platform_admin"])
    if not run.lead:
        if run.unavailable:
            await _trace(ctx, mid=mid, salon={"id": None}, sender="platform_admin", user_text=msg.text or "",
                         steps=run.steps, final_text=None, buttons=None, state_after=None,
                         outcome="unavailable", grounded=None, problems=run.problems, model=run.first_model,
                         calls=run.calls, ms=run.ms)
            return False
        body = _fallback_reply(run, lang)
        await _trace(ctx, mid=mid, salon={"id": None}, sender="platform_admin", user_text=msg.text or "",
                     steps=run.steps, final_text=body, buttons=None, state_after=None,
                     outcome="unavailable" if run.unavailable else "grounding_fallback",
                     grounded=False if run.problems else None, problems=run.problems, model=run.first_model,
                     calls=run.calls, ms=run.ms)
        await outbound.send_plain(ctx, msg.sender, body)
        return True

    body = _clean_reply(run.lead[:700])
    if first_reply:
        body = ensure_help_hint(body, lang)
    saved = [*messages, {"role": "assistant", "content": body}][-10:]
    await ctx.pool.execute(
        "INSERT INTO platform_agent_sessions (sender, history, updated_at) VALUES ($1,$2::jsonb,now()) "
        "ON CONFLICT (sender) DO UPDATE SET history=EXCLUDED.history, updated_at=now()",
        msg.sender, json.dumps(saved, ensure_ascii=False))
    await _trace(ctx, mid=mid, salon={"id": None}, sender="platform_admin", user_text=msg.text or "",
                 steps=run.steps, final_text=body, buttons=None, state_after=None,
                 outcome="platform_agent", grounded=not run.problems, problems=run.problems,
                 model=run.first_model, calls=run.calls, ms=run.ms)
    await outbound.send_plain(ctx, msg.sender, body)
    return True
