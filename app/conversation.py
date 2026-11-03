"""The conversation router: understand (model, or plain rules if no model answers), then dispatch.

One customer at a time (a database lock per sender); what we remember about them lives in Postgres.
Booking work itself lives in booking_flow.py; this file keeps routing, language and dispatch."""
from __future__ import annotations

import dataclasses
import json
import logging
import re
from datetime import date, datetime
from zoneinfo import ZoneInfo

from . import agent, binding, guard, locks, mpesa, onboarding_chat, owner, payments, platform, screen, smalltalk, waitlist
from .availability import DAY_KEYS
from .booking_flow import (
    _book,
    _cancel,
    _catalog,
    _late_tap,
    _pay,
    _reply,
    _save,
    _till_claim,
    _visit_tap,
    cancel_owner_availability_request,
    expire_owner_availability_checks,
    example_for,
    handle_manual_payment_tap,
    match_service,
    menu,
    owner_availability_response,
    peek_state,
    resume_late_owner_booking,
    save_stranger,
    stranger_state,
)
from .channels import outbound
from .ai.llm import Extraction, Result, rule_extract
from .reminders import handle_still_tap
from .messages import t
from .channels.whatsapp import Message, mask

log = logging.getLogger("rembo.conv")
PAY_WORDS = re.compile(r"^\s*(pay|paid|lipa|nimelipa|nimeshalipa|nimetuma|nimeshatuma|done|sent|tuma|retry|resend|tena|try again)(\s+(now|sasa|hivi|tayari))?\W*$", re.I)
WAITLIST_WORDS = re.compile(r"^\s*(waitlist|waiting list|notify me|let me know|nijulishe|niweke|weka jina)\W*$", re.I)
DIRECT_CUSTOMER_BOOKING = re.compile(
    r"\b(?:i\s+(?:want|need|would like|'d like)\s+to\s+book|book\s+me(?:\s+for)?|can\s+i\s+book|"
    r"naomba\s+kuweka\s+miadi|nataka\s+kuweka\s+miadi)\b", re.I)


def _explicit_customer_booking(text: str, services: list[dict]) -> bool:
    if onboarding_chat.wants_customer_booking(text) or DIRECT_CUSTOMER_BOOKING.search(text or ""):
        return True
    for service in services:
        for name in (service.get("name"), *service.get("aliases", [])):
            if not name:
                continue
            term = re.escape(str(name).strip())
            if re.search(rf"\b(?:nataka|naomba)\s+(?:a\s+)?{term}\b", text or "", re.I):
                return True
            if re.search(rf"\b(?:book|schedule)\s+(?:me\s+)?(?:a\s+)?{term}\b", text or "", re.I):
                return True
    return False


class Chat:
    """Sends to one customer on one salon's number."""

    def __init__(self, ctx, salon, to: str):
        self.ctx, self.salon, self.pid, self.to = ctx, salon, salon["wa_phone_number_id"], to

    async def text(self, body: str) -> None:
        await outbound.send_text(self.ctx, self.salon, self.to, body)

    async def buttons(self, body: str, buttons: list[tuple[str, str]]) -> None:
        await outbound.send_buttons(self.ctx, self.salon, self.to, body, buttons)


def _tap_is_duplicate(st: dict, reply_id: str | None, now: datetime) -> bool:
    last = st.get("last_tap")
    if not last or not reply_id:
        return False
    try:
        at = datetime.fromisoformat(last.get("at")) if isinstance(last.get("at"), str) else last.get("at")
        if at and (now - at).total_seconds() < 60 and last.get("id") == reply_id:
            return True
    except Exception:
        return False
    return False


async def handle(ctx, msg: Message) -> None:
    """Serialise one sender's messages, so rapid taps cannot interleave. The lock is a lease row, not a connection: nothing is pinned
    in the pool while the model thinks and the replies go out."""
    async with locks.hold(ctx.pool, f"conv:{msg.phone_number_id}:{msg.sender}"):
        await _handle(ctx, msg)


async def _load(ctx, salon_id: str, wa_id: str, name: str | None) -> tuple[dict, dict]:
    client = await ctx.pool.fetchrow(
        "INSERT INTO clients (salon_id, wa_id, name) VALUES ($1::uuid,$2,$3) "
        "ON CONFLICT (salon_id, wa_id) DO UPDATE SET name = COALESCE(EXCLUDED.name, clients.name) "
        "RETURNING id::text, wa_id, lang, name", salon_id, wa_id, name)
    raw = await ctx.pool.fetchval("SELECT state FROM conversations WHERE salon_id=$1::uuid AND wa_id=$2", salon_id, wa_id)
    state = raw if isinstance(raw, dict) else json.loads(raw or "{}")
    if not isinstance(state.get("history"), list):
        state["history"] = []
    return dict(client), state or {"history": []}


async def _handoff_before_onboarding(ctx, salon: dict, sender: str, chat: Chat) -> bool:
    """Keep onboarding from intercepting a conversation that is paused for a human."""
    row = await ctx.pool.fetchrow(
        "SELECT h.created_at, c.lang FROM handoffs h JOIN clients c ON c.id=h.client_id "
        "WHERE h.salon_id=$1::uuid AND c.wa_id=$2 AND h.status='open' "
        "ORDER BY h.created_at DESC LIMIT 1", salon["id"], sender,
    )
    if row is not None:
        age = (datetime.now(ZoneInfo(salon["timezone"])) - row["created_at"]).total_seconds()
        if age < float(getattr(ctx.settings, "handoff_hours", guard.HANDOFF_HOURS)) * 3600:
            await chat.text(guard.reply_for("waiting", row["lang"] or "en"))
            return True
        await guard.clear_handoff(ctx.pool, salon["id"], sender)

    # A resolved or expired handoff must not leave a stale state flag behind.
    await ctx.pool.execute(
        "UPDATE conversations SET state=COALESCE(state,'{}'::jsonb)-'state'-'category', updated_at=now() "
        "WHERE salon_id=$1::uuid AND wa_id=$2 AND state->>'state'='handoff'",
        salon["id"], sender,
    )
    return False


async def _handle_sensitive_text(ctx, salon: dict, msg: Message, chat: Chat, text: str,
                                client: dict | None = None, state: dict | None = None) -> bool:
    """Escalate safety-sensitive customer text before a workflow or model can consume it."""
    hit = guard.classify(text)
    if not hit:
        return False
    if client is None:
        client, state = await _load(ctx, salon["id"], msg.sender, msg.name)
    category, detected_lang = hit
    lang = detected_lang if detected_lang in ("en", "sw") else client["lang"]
    await guard.hand_off(ctx, salon, client, lang, category, text)
    handoff_state = {**(state or {"history": []}), "state": "handoff", "category": category}
    await _reply(ctx, salon["id"], msg.sender, handoff_state, chat, guard.reply_for(category, lang))
    return True


async def _customer_has_selected_salon(ctx, msg: Message, salon: dict, linked: bool) -> bool:
    """Only attach an early customer handoff to a salon the sender actually selected."""
    if linked:
        return True
    bound_id = await binding.bound_salon(ctx.pool, msg.sender)
    if bound_id:
        return bound_id == salon["id"]
    count = await ctx.pool.fetchval(
        "SELECT count(*) FROM salons WHERE active AND (wa_phone_number_id=$1 OR tg_bot_id=$1)",
        msg.phone_number_id,
    )
    return int(count or 0) == 1


async def _owned_channel_salons(ctx, sender: str, phone_number_id: str, selector: str | None = None) -> list[dict]:
    rows = await ctx.pool.fetch(
        "SELECT id::text, name, slug, wa_phone_number_id, wa_access_token, timezone, open_hours, deposit_kes, owner_wa_id, tg_bot_id "
        "FROM salons WHERE active AND owner_wa_id=$1 AND (wa_phone_number_id=$2 OR tg_bot_id=$2) "
        "AND ($3::text IS NULL OR lower(slug)=lower($3)) ORDER BY created_at, id",
        sender, phone_number_id, selector,
    )
    return [dict(row) for row in rows]


_REGISTRATION_HYPOTHETICAL = re.compile(
    r"\b(?:if\s+i|what\s+if|what\s+happens|suppose\s+i|will\s+i|would\s+i|can\s+i|could\s+i|should\s+i|"
    r"how\s+(?:do|can|should|would|to)|can\s+you|could\s+you|are\s+you\s+able|"
    r"je|naweza|nitaweza|nitabaki)\b", re.I)
def _registration_start(msg: Message) -> bool:
    if msg.kind != "text":
        return False
    text = msg.text or ""
    explicit = onboarding_chat.registration_start_only(text)
    if explicit:
        return True
    request = onboarding_chat.wants_register(text)
    inquiry = text.strip().endswith("?") or _REGISTRATION_HYPOTHETICAL.search(text)
    return request and not inquiry


async def _platform_registration(ctx, msg: Message, session: dict | None) -> None:
    """Allow the platform identity to register its own salon, with ownership confirmation in the wizard."""
    text = msg.text or ""
    if session is None:
        if _registration_start(msg):
            await onboarding_chat.begin(ctx, msg.sender, msg.phone_number_id, text)
            return
        await outbound.send_plain(ctx, msg.sender, platform.help_text(smalltalk.language(text, "en")))
        return

    lang = session.get("lang") or onboarding_chat.detect_lang(text)
    if msg.kind == "audio":
        if not msg.media_id or ctx.voice is None or not ctx.voice.enabled:
            await outbound.send_plain(ctx, msg.sender, t(lang, "voice_failed"))
            return
        await _voice_typing(ctx, msg.sender)
        heard = await _hear(ctx, msg, lang, outbound.salon_token(ctx, None))
        if heard is None or not guard.clean(heard):
            await outbound.send_plain(ctx, msg.sender, t(lang, "voice_failed"))
            return
        text = guard.clean(heard)
        await outbound.update_inbound_text(ctx, text)
    media = {"kind": "image"} if msg.kind == "image" else None
    await onboarding_chat.advance(ctx, msg.sender, msg.phone_number_id, text, media)


async def _platform_voice_language(ctx, sender: str) -> str:
    """Use the platform owner's recent typed language as an ASR hint when available."""
    row = await ctx.pool.fetchrow(
        "SELECT history FROM platform_agent_sessions WHERE sender=$1 AND updated_at > now() - interval '30 minutes'", sender)
    history = row["history"] if row is not None else []
    if isinstance(history, str):
        try:
            history = json.loads(history)
        except ValueError:
            history = []
    if isinstance(history, list):
        for item in reversed(history):
            if isinstance(item, dict) and item.get("role") == "user":
                lang = smalltalk.language(str(item.get("content") or ""), "")
                if lang:
                    return lang
    return "en"


async def _platform_voice(ctx, msg: Message, lang: str) -> Message | None:
    """Transcribe a platform-admin voice note before routing it to the read-only assistant."""
    if not msg.media_id or ctx.voice is None or not ctx.voice.enabled:
        await outbound.send_plain(ctx, msg.sender, t(lang, "voice_failed"))
        return None
    await _voice_typing(ctx, msg.sender)
    heard = await _hear(ctx, msg, lang, outbound.salon_token(ctx, None))
    text = guard.clean(heard)
    if not text:
        await outbound.send_plain(ctx, msg.sender, t(lang, "voice_failed"))
        return None
    await outbound.update_inbound_text(ctx, text)
    return dataclasses.replace(msg, kind="text", text=text, media_id=None)


async def _replay_aware_state(ctx, salon_id: str, msg: Message, st: dict) -> dict:
    """The first attempt at a message records the conversation as it found it. A retry (the first attempt died part way, having maybe
    saved a later state) starts again from that record, so it takes the same path and its replies, bookings and prompts match the
    first attempt's, which is what lets them be recognised and not repeated."""
    snap = await ctx.pool.fetchval("SELECT state_before FROM inbound_messages WHERE wa_message_id=$1", msg.id)
    if snap is None:
        await ctx.pool.execute("UPDATE inbound_messages SET state_before=$2::jsonb WHERE wa_message_id=$1 AND state_before IS NULL", msg.id, json.dumps(st))
        return st
    restored = snap if isinstance(snap, dict) else json.loads(snap)
    log.info("handling %s again: starting from the conversation as it was before the first attempt", msg.id)
    await _save(ctx, salon_id, msg.sender, restored)
    return restored


async def _salon_row(ctx, salon_id: str) -> dict | None:
    row = await ctx.pool.fetchrow(
        "SELECT id::text, name, wa_phone_number_id, wa_access_token, timezone, open_hours, deposit_kes, owner_wa_id FROM salons WHERE id=$1::uuid AND active",
        salon_id)
    if row is None:
        return None
    out = dict(row)
    if isinstance(out["open_hours"], str):
        out["open_hours"] = json.loads(out["open_hours"])
    return out


async def _resolve_salon(ctx, msg: Message, salon: dict) -> tuple[dict, bool]:
    """One bot number can serve many salons. A /start deep link names the salon and is remembered;
    otherwise a remembered link wins over the default salon on this number. Returns the salon and
    whether this message linked to it (an invite tap, which then lands as a greeting)."""
    text = (msg.text or "")
    arg = text[len("/start"):].strip() if text.startswith("/start") else ""
    linked = False
    if arg and " " not in arg and arg.lower() not in ("register", "sajili", "anzisha"):
        target_id = await binding.bind_sender(ctx.pool, msg.sender, arg)
        if target_id is not None:
            target = await _salon_row(ctx, target_id)
            if target is not None:
                return target, True
    bound_id = await binding.bound_salon(ctx.pool, msg.sender)
    if bound_id is not None and bound_id != salon["id"]:
        target = await _salon_row(ctx, bound_id)
        if target is not None:
            return target, linked
    return salon, linked


async def _identity(ctx, msg: Message, salon: dict, linked: bool, chat: Chat) -> None:
    """Answer 'who are you' and 'how do I use this' as the bot the sender reached.

    A sender linked to a business meets that business's assistant; anyone else meets
    RemboAI itself, with plain steps for owners and customers. Booking state is never
    touched, so asking mid-booking loses nothing."""
    text = msg.text or ""
    lang = smalltalk.language(text, "en")
    helping = smalltalk.asks_help(text)
    target = salon if linked else None
    if target is None:
        bound_id = await binding.bound_salon(ctx.pool, msg.sender)
        if bound_id is not None:
            target = await _salon_row(ctx, bound_id)
    if target is None:
        n = await ctx.pool.fetchval(
            "SELECT count(*) FROM salons WHERE (wa_phone_number_id=$1 OR tg_bot_id=$1) AND active", msg.phone_number_id)
        if n == 1:
            target = salon
    if target is None:
        await chat.text(t(lang, "main_help" if helping else "main_identity"))
        return
    services = await _catalog(ctx, target["id"])
    await Chat(ctx, target, msg.sender).text(
        t(lang, "assistant_help" if helping else "assistant_identity",
          salon=target["name"], example=example_for(services, lang)))


async def _neutral_route(ctx, msg: Message, salon: dict, linked: bool, chat: Chat) -> tuple[dict | None, bool]:
    """The shared bot belongs to no business: a customer link or a remembered binding
    routes to exactly one salon, and nothing else ever sees another salon's data.
    One salon on this number serves directly; an owner with one salon on this number
    lands there; anyone else names or picks their business first."""
    if linked:
        return salon, True
    bound_id = await binding.bound_salon(ctx.pool, msg.sender)
    if bound_id is not None:
        return salon, False
    rows = await ctx.pool.fetch(
        "SELECT id::text, name, slug FROM salons WHERE (wa_phone_number_id=$1 OR tg_bot_id=$1) AND active ORDER BY created_at, id",
        msg.phone_number_id)
    if len(rows) <= 1:
        return salon, False
    owned = [r for r in await ctx.pool.fetch(
        "SELECT id::text FROM salons WHERE owner_wa_id=$1 AND (wa_phone_number_id=$2 OR tg_bot_id=$2) AND active",
        msg.sender, msg.phone_number_id)]
    if len(owned) == 1:
        target = await _salon_row(ctx, owned[0]["id"])
        return (target or salon), False
    text = (msg.text or "").strip()
    lang = smalltalk.language(text, "en")
    seen_st = await peek_state(ctx, salon["id"], msg.sender)
    offer = seen_st.get("picker_offer")
    offer = offer if isinstance(offer, dict) else {}
    if offer.get("id") is not None and text.lower() in _PICKER_YES:
        target = await _salon_row(ctx, offer["id"])
        if target is not None:
            slug = offer.get("slug") or await binding.ensure_slug(ctx.pool, target["id"], target["name"])
            await binding.bind_sender(ctx.pool, msg.sender, slug)
            await _save(ctx, salon["id"], msg.sender,
                        {k: v for k, v in seen_st.items() if k != "picker_offer"})
            return target, True
    match = _match_business(rows, text)
    if match is not None:
        slug = match["slug"] or await binding.ensure_slug(ctx.pool, match["id"], match["name"])
        await binding.bind_sender(ctx.pool, msg.sender, slug)
        target = await _salon_row(ctx, match["id"])
        return (target or salon), True
    fuzzy = _fuzzy_business(rows, text)
    if fuzzy is not None:
        await _save(ctx, salon["id"], msg.sender,
                    {**seen_st, "picker_offer": {"id": fuzzy["id"], "slug": fuzzy.get("slug")}})
        await chat.text(t(lang, "ob_pick_confirm", name=fuzzy["name"]))
        return None, False
    if msg.kind in ("audio", "image") or smalltalk.asks_identity(text) or smalltalk.asks_help(text):
        # Generic identity and help questions belong to RemboAI itself. Do not
        # expose a list of salons just because they share this channel number.
        await _unallocated(ctx, msg)
        return None, False
    from .channels import invites
    channel = invites.channel_of(msg.sender)
    from .channels.telegram import bot_username as _bot_username
    username = await _bot_username(getattr(ctx, "wa", None)) if channel == "tg" else None
    number = getattr(ctx.settings, "central_whatsapp_number", "") if channel == "wa" else ""
    seen = int(seen_st.get("picker_sends") or 0)
    if seen >= 2:
        await guard.strike(ctx.pool, msg.sender, "picker_spam")
        await chat.text(t(lang, "ob_pick_again"))
        return None, False
    await _save(ctx, salon["id"], msg.sender,
                {k: v for k, v in seen_st.items() if k != "picker_offer" and k != "picker_sends"} | {"picker_sends": seen + 1})
    lines = []
    for i, r in enumerate(rows, 1):
        slug = r["slug"] or await binding.ensure_slug(ctx.pool, r["id"], r["name"])
        lines.append(t(lang, "ob_pick_item", n=i, name=r["name"],
                       link=invites.invite_for(channel, slug, username=username, number=number)))
    await chat.text(t(lang, "ob_pick_salon", lines="\n".join(lines)))
    return None, False


def _match_business(rows: list, text: str) -> dict | None:
    """A typed business choice: its number, slug, or exact name. Close is not enough to bind on."""
    clean = (text or "").strip().lower()
    if not clean:
        return None
    if clean.isdigit() and 1 <= int(clean) <= len(rows):
        return dict(rows[int(clean) - 1])
    for r in rows:
        if (r["slug"] or "").lower() == clean:
            return dict(r)
    for r in rows:
        if r["name"].lower() == clean:
            return dict(r)
    return None


def _fuzzy_business(rows: list, text: str) -> dict | None:
    """A near-miss on a business name: offered for confirmation, never bound on directly."""
    import difflib as _difflib
    clean = (text or "").strip().lower()
    if not clean:
        return None
    lowered = {r["name"].lower(): r for r in rows}
    close = _difflib.get_close_matches(clean, list(lowered), n=1, cutoff=0.8)
    return dict(lowered[close[0]]) if close else None


_PICKER_YES = {"yes", "yeah", "yep", "ndiyo", "ndio", "sawa", "ok"}


async def _unallocated(ctx, msg: Message) -> None:
    """The shared bot with no business selected. It can answer general questions,
    explain setup, tell a sender their own id, and start registration.

    It has no access to salon records. Each exchange is retained in stranger_state."""
    text = msg.text or ""
    if msg.kind == "audio":
        lang = smalltalk.language(text, "en")
        if not msg.media_id or ctx.voice is None or not ctx.voice.enabled:
            await outbound.send_plain(ctx, msg.sender, t(lang, "voice_failed"))
            return
        await _voice_typing(ctx, msg.sender)
        heard = await _hear(ctx, msg, lang, outbound.salon_token(ctx, None))
        text = guard.clean(heard)
        if not text:
            await outbound.send_plain(ctx, msg.sender, t(lang, "voice_failed"))
            return
        await outbound.update_inbound_text(ctx, text)
        msg = dataclasses.replace(msg, kind="text", text=text, media_id=None)
    st = await stranger_state(ctx, msg.sender)
    hist = list(st.get("history") or []) + [{"role": "user", "text": text[:500], "ts": datetime.now(ZoneInfo("UTC")).isoformat()}]

    async def answer(body: str) -> None:
        hist.append({"role": "assistant", "text": body[:500], "ts": datetime.now(ZoneInfo("UTC")).isoformat()})
        await save_stranger(ctx, msg.sender, {**st, "history": hist})
        await outbound.send_plain(ctx, msg.sender, body)

    sess = await onboarding_chat.load_session(ctx.pool, msg.sender)
    if sess is not None:
        if msg.kind == "text":
            await onboarding_chat.advance(ctx, msg.sender, msg.phone_number_id, text, None)
        elif msg.kind == "image" and msg.media_id:
            await onboarding_chat.advance(ctx, msg.sender, msg.phone_number_id, text, {"kind": "image"})
        else:
            lang = sess.get("lang") or smalltalk.language(text, "en")
            await answer(t(lang, "voice_failed" if msg.kind == "audio" else "main_help"))
            return
        await save_stranger(ctx, msg.sender, {**st, "history": hist})
        return
    lowered = text.strip().lower()
    if msg.kind == "text" and lowered in ("/id", "my id", "myid", "id"):
        await answer(t(smalltalk.language(text, "en"), "my_id", id=msg.sender))
        return
    if _registration_start(msg):
        await onboarding_chat.begin(ctx, msg.sender, msg.phone_number_id, text)
        await save_stranger(ctx, msg.sender, {**st, "history": hist})
        return
    lang = smalltalk.language(text, "en")
    if msg.kind == "audio":
        await answer(t(lang, "main_voice"))
        return
    if msg.kind == "image":
        await answer(t(lang, "main_photo"))
        return
    if agent.enabled(ctx):
        from .agent import bare_assistant
        if await bare_assistant.turn(ctx, msg, st, hist):
            return
    if smalltalk.asks_user_identity(text):
        await answer(t(lang, "main_unallocated_identity"))
        return
    if smalltalk.asks_help(text):
        await answer(t(lang, "main_help"))
        return
    if smalltalk.asks_identity(text):
        await answer(t(lang, "main_identity"))
        return
    if smalltalk.thanks(text) is not None:
        await answer(t(lang, "main_thanks"))
        return
    if smalltalk.opener(text) is not None:
        await answer(t(lang, "main_hello"))
        return
    if int(st.get("main_sends") or 0) >= 2:
        await answer(t(lang, "main_nudge"))
        return
    body = t(lang, "main_identity")
    hist.append({"role": "assistant", "text": body[:500], "ts": datetime.now(ZoneInfo("UTC")).isoformat()})
    await save_stranger(ctx, msg.sender, {**st, "history": hist, "main_sends": int(st.get("main_sends") or 0) + 1})
    await outbound.send_plain(ctx, msg.sender, body)


async def _handle(ctx, msg: Message) -> None:
    platform_sender = platform.is_platform(ctx.settings, msg.sender)
    explicit_owner_route = False
    platform_owner_route = False

    if platform_sender:
        platform_audio = msg.kind == "audio"
        if platform_audio:
            session = await onboarding_chat.load_session(ctx.pool, msg.sender)
            if session is not None:
                await _platform_registration(ctx, msg, session)
                return
            lang = await _platform_voice_language(ctx, msg.sender)
            transcribed = await _platform_voice(ctx, msg, lang)
            if transcribed is None:
                return
            msg = transcribed
            if _registration_start(msg):
                await _platform_registration(ctx, msg, None)
                return
        if msg.kind == "text" and guard.injection(msg.text or ""):
            await guard.strike(ctx.pool, msg.sender, "platform_agent_injection")
            await outbound.send_plain(ctx, msg.sender, platform.help_text(smalltalk.language(msg.text or "", "en")))
            return
        if (msg.kind == "text" and not platform_audio and platform.is_mutation_command(msg.text or "")
                and await platform.handle(ctx, msg.sender, msg.text or "")):
            return
        owner_request = platform.owner_request(msg.text or "") if msg.kind == "text" and not platform_audio else None
        if owner_request is None:
            session = await onboarding_chat.load_session(ctx.pool, msg.sender)
            if session is not None or _registration_start(msg):
                await _platform_registration(ctx, msg, session)
                return
            if msg.kind == "text" and agent.enabled(ctx):
                from .agent import platform_assistant
                if await platform_assistant.turn(ctx, msg):
                    return
            if (msg.kind == "text" and not (platform_audio and platform.is_mutation_command(msg.text or ""))
                    and await platform.handle(ctx, msg.sender, msg.text or "")):
                return
            await outbound.send_plain(ctx, msg.sender, platform.help_text(smalltalk.language(msg.text or "", "en")))
            return
        selector, owner_text = owner_request
        if not owner_text:
            if msg.kind == "text" and agent.enabled(ctx):
                from .agent import platform_assistant
                if await platform_assistant.turn(ctx, msg):
                    return
            await outbound.send_plain(ctx, msg.sender, platform.help_text(smalltalk.language(msg.text or "", "en")))
            return
        owned = await _owned_channel_salons(ctx, msg.sender, msg.phone_number_id, selector)
        if len(owned) != 1:
            message = ("No business owned by this account matches that route." if not owned else
                       "Several businesses match. Use /owner <business-slug> | <command>.")
            lang = smalltalk.language(msg.text or "", "en")
            message = ("Hakuna biashara inayolingana na njia hiyo." if lang == "sw" and not owned else
                       "Biashara kadhaa zinafanana. Tumia /owner <slug-ya-biashara> | <amri>." if lang == "sw" else message)
            await outbound.send_plain(ctx, msg.sender, message + "\n\n" + platform.help_text(lang))
            return
        salon, linked = owned[0], False
        msg = dataclasses.replace(msg, text=owner_text)
        explicit_owner_route = platform_owner_route = True
    else:
        owner_request = platform.owner_request(msg.text or "") if msg.kind == "text" else None
        if owner_request is not None:
            selector, owner_text = owner_request
            route_lang = smalltalk.language(msg.text or "", "en")
            if not owner_text:
                await outbound.send_plain(ctx, msg.sender, t(route_lang, "owner_route_denied"))
                return
            owned = await _owned_channel_salons(ctx, msg.sender, msg.phone_number_id, selector)
            if len(owned) != 1:
                key = "owner_route_denied" if not owned else "owner_route_choose"
                await outbound.send_plain(ctx, msg.sender, t(route_lang, key))
                return
            salon, linked = owned[0], False
            msg = dataclasses.replace(msg, text=owner_text)
            explicit_owner_route = True
        else:
            salon = await ctx.pool.fetchrow(
                "SELECT id::text, name, wa_phone_number_id, wa_access_token, timezone, open_hours, deposit_kes, owner_wa_id, tg_bot_id "
                "FROM salons WHERE (wa_phone_number_id=$1 OR tg_bot_id=$1) AND active ORDER BY created_at, id LIMIT 1",
                msg.phone_number_id)
            if salon is None:
                if msg.sender.startswith("tg:"):
                    await _unallocated(ctx, msg)
                else:
                    log.warning("message for a number or bot we do not serve: %s", msg.phone_number_id)
                return
            salon = dict(salon)
            salon, linked = await _resolve_salon(ctx, msg, salon)
            if not linked:
                owned = await _owned_channel_salons(ctx, msg.sender, msg.phone_number_id)
                if len(owned) == 1:
                    salon = owned[0]

    if isinstance(salon["open_hours"], str):
        salon["open_hours"] = json.loads(salon["open_hours"])
    await outbound.set_conversation_salon(ctx, salon["id"])
    chat = Chat(ctx, salon, msg.sender)
    owner_mode = owner.is_owner(salon, msg.sender)
    if owner_mode and msg.kind == "audio":
        lang = "en"
        if not msg.media_id or ctx.voice is None or not ctx.voice.enabled:
            await chat.text(t(lang, "voice_failed"))
            return
        await _voice_typing(ctx, msg.sender)
        heard = await _hear(ctx, msg, lang, outbound.salon_token(ctx, salon), salon["id"])
        text = guard.clean(heard)
        if not text:
            await chat.text(t(lang, "voice_failed"))
            return
        await outbound.update_inbound_text(ctx, text)
        msg = dataclasses.replace(msg, kind="text", text=text, media_id=None, reply_id=None)
    if owner_mode and msg.kind == "text" and guard.injection(msg.text or ""):
        await guard.strike(ctx.pool, msg.sender, "owner_agent_injection")
        await chat.text(t(smalltalk.language(msg.text or "", "en"), "owner_unrecognized"))
        return
    if owner_mode and msg.kind == "interactive" and msg.reply_id and await owner.handle_tap(ctx, salon, msg.sender, msg.reply_id):
        return
    if owner_mode and msg.kind == "text" and await guard.owner_command(ctx, salon, msg.sender, msg.text or "", chat):
        return

    sess = await onboarding_chat.load_session(ctx.pool, msg.sender)
    registration_start = _registration_start(msg)

    if owner_mode and msg.kind == "text" and msg.text and not registration_start:
        booking_request = _explicit_customer_booking(msg.text, [])
        if not booking_request and re.match(r"\s*(?:nataka|naomba|book|schedule)\b", msg.text, re.I):
            booking_request = _explicit_customer_booking(msg.text, await _catalog(ctx, salon["id"]))
        if booking_request:
            lang = smalltalk.language(msg.text, "en")
            await chat.text(t(lang, "owner_self_identity", salon=salon["name"]))
            return

    if owner_mode and (explicit_owner_route or sess is None) and not registration_start:
        if msg.kind == "text" and msg.text:
            tz0 = ZoneInfo(salon["timezone"])
            now0 = datetime.now(tz0)
            if (agent.enabled(ctx) and not owner.is_command(msg.text)
                    and await agent.owner_turn(ctx, salon, msg.sender, msg.text, tz0, now0, chat)):
                return
            command = msg.text.strip().lower().strip(" ?.!")
            lang = smalltalk.language(msg.text, "en")
            if command in ("/id", "my id", "myid", "id"):
                await chat.text(t(lang, "my_id", id=msg.sender))
                return
            if smalltalk.asks_help(msg.text):
                if await owner.handle(ctx, salon, msg.sender, msg.text, tz0, now0):
                    return
                await chat.text(t(lang, "owner_help"))
                return
            if smalltalk.asks_identity(msg.text):
                await _identity(ctx, msg, salon, linked, chat)
                return
            if re.fullmatch(r"(?:prices?|price list|menu|bei|orodha ya bei)", command):
                await chat.text(t(lang, "prices", lines=menu(await _catalog(ctx, salon["id"]))))
                return
            if re.fullmatch(r"(?:hours?|opening hours|open hours|muda|saa za kufungua)", command):
                await chat.text(t(lang, "hours", lines=_hours(salon, lang)))
                return
            if smalltalk.opener(msg.text) is not None:
                await chat.text(t(lang, "welcome", salon=salon["name"], example=example_for(await _catalog(ctx, salon["id"]), lang)))
                return
            tz0 = ZoneInfo(salon["timezone"])
            if await owner.owner_followup_is_pending(ctx, salon, msg.sender, msg.text):
                await chat.text(t(smalltalk.language(msg.text, "en"), "owner_unrecognized"))
                return
            if owner.DELETE_REQUEST.search(msg.text):
                await chat.text(t(lang, "owner_business_delete"))
                return
            if await owner.handle(ctx, salon, msg.sender, msg.text, tz0, datetime.now(tz0)):
                return
        if explicit_owner_route or sess is None:
            lang = smalltalk.language(msg.text or "", "en")
            await chat.text(t(lang, "owner_unrecognized"))
            return

    if platform_owner_route and registration_start:
        await outbound.send_plain(ctx, msg.sender, platform.help_text(smalltalk.language(msg.text or "", "en")))
        return
    salon_context_selected = owner_mode or await _customer_has_selected_salon(ctx, msg, salon, linked)
    if salon_context_selected and await _handoff_before_onboarding(ctx, salon, msg.sender, chat):
        return
    if (not owner_mode and salon_context_selected and msg.kind == "text" and
            await _handle_sensitive_text(ctx, salon, msg, chat, msg.text or "")):
        return

    if sess is not None:
        text, media = msg.text or "", ({"kind": "image"} if msg.kind == "image" else None)
        if msg.kind == "audio":
            lang = sess.get("lang") or "en"
            if not msg.media_id or ctx.voice is None or not ctx.voice.enabled:
                await chat.text(t(lang, "voice_failed"))
                return
            await _voice_typing(ctx, msg.sender)
            heard = await _hear(ctx, msg, lang, outbound.salon_token(ctx, salon), salon["id"])
            if heard is None:
                await chat.text(t(lang, "voice_failed"))
                return
            text = guard.clean(heard)
            if not text:
                await chat.text(t(lang, "voice_failed"))
                return
            await outbound.update_inbound_text(ctx, text)
            if not owner_mode and salon_context_selected and await _handle_sensitive_text(ctx, salon, msg, chat, text):
                return
            # If the owner cancels registration or asks to book as a customer,
            # let the normal text router handle the recognised speech.
            msg = dataclasses.replace(msg, kind="text", text=text, media_id=None, reply_id=None)
        if msg.kind == "text" and onboarding_chat.wants_cancel_registration(text):
            await onboarding_chat.advance(ctx, msg.sender, msg.phone_number_id, text, media)
            return
        if msg.kind == "text" and onboarding_chat.wants_customer_booking(text):
            await onboarding_chat.clear_session(ctx.pool, msg.sender)
        else:
            await onboarding_chat.advance(ctx, msg.sender, msg.phone_number_id, text, media)
            return
    if owner_mode and not registration_start:
        lang = smalltalk.language(msg.text or "", "en")
        await chat.text(t(lang, "owner_unrecognized"))
        return
    if _registration_start(msg):
        await onboarding_chat.begin(ctx, msg.sender, msg.phone_number_id, msg.text or "")
        return
    salon, arrival = await _neutral_route(ctx, msg, salon, linked, chat)
    if salon is None:
        return
    if arrival:
        msg = dataclasses.replace(msg, kind="text", text="hi", reply_id=None)
    chat = Chat(ctx, salon, msg.sender)
    if msg.kind == "text" and (msg.text or "").strip().lower() in ("/id", "my id", "myid", "id"):
        lang = smalltalk.language(msg.text or "", "en")
        if owner.is_owner(salon, msg.sender):
            await chat.text(t(lang, "my_id", id=msg.sender))
        else:
            services = await _catalog(ctx, salon["id"])
            await chat.text(t(lang, "assistant_help", salon=salon["name"], example=example_for(services, lang)))
        return
    if owner.is_owner(salon, msg.sender):
        lang = smalltalk.language(msg.text or "", "en")
        await chat.text(t(lang, "owner_unrecognized"))
        return
    client, st = await _load(ctx, salon["id"], msg.sender, msg.name)
    st = await _replay_aware_state(ctx, salon["id"], msg, st)
    lang = client["lang"]
    if msg.kind == "interactive" and msg.reply_id and msg.reply_id.startswith("manualpay:"):
        tz = ZoneInfo(salon["timezone"])
        services = await _catalog(ctx, salon["id"])
        await handle_manual_payment_tap(ctx, salon, client, st, services, tz, datetime.now(tz), chat, lang, msg.reply_id)
        return
    if st.get("state") == "handoff":
        created = await ctx.pool.fetchval(
            "SELECT created_at FROM handoffs WHERE salon_id=$1::uuid AND client_id=$2::uuid AND status='open' ORDER BY created_at DESC LIMIT 1",
            salon["id"], client["id"],
        )
        if created and (datetime.now(ZoneInfo(salon["timezone"])) - created).total_seconds() < float(getattr(ctx.settings, "handoff_hours", guard.HANDOFF_HOURS)) * 3600:
            await _reply(ctx, salon["id"], msg.sender, st, chat, guard.reply_for("waiting", lang))
            return
        await guard.clear_handoff(ctx.pool, salon["id"], msg.sender)
        st = {"history": st.get("history", [])}
    if await _handle_sensitive_text(ctx, salon, msg, chat, msg.text or "", client, st):
        return
    st.setdefault("history", []).append({"role": "user", "text": msg.text or "", "ts": datetime.now(ZoneInfo(salon["timezone"])).isoformat()})
    if len(st["history"]) > guard.MAX_HISTORY * 2:
        st["history"] = st["history"][-guard.MAX_HISTORY * 2 :]
    if msg.kind == "audio" and msg.media_id and ctx.voice is not None and ctx.voice.enabled:
        await _voice_typing(ctx, msg.sender)
        heard = await _hear(ctx, msg, lang, outbound.salon_token(ctx, salon), salon["id"])
        if heard is None:
            await _reply(ctx, salon["id"], msg.sender, st, chat, t(lang, "voice_failed"))
            return
        heard_text = guard.clean(heard)
        await outbound.update_inbound_text(ctx, heard_text)
        st["history"][-1]["text"] = heard_text
        msg = dataclasses.replace(msg, kind="text", text=heard_text, reply_id=None)
        if not owner.is_owner(salon, msg.sender) and await _handle_sensitive_text(ctx, salon, msg, chat, heard_text, client, st):
            return
    if guard.injection(msg.text or ""):
        log.warning("prompt-injection attempt from %s: no model call", mask(msg.sender))
        await guard.strike(ctx.pool, msg.sender, "injection")
        services = await _catalog(ctx, salon["id"])
        await _reply(ctx, salon["id"], msg.sender, st, chat,
                     t(lang, "off_topic", salon=salon["name"], example=example_for(services, lang)))
        return
    if msg.reply_id and msg.reply_id.startswith("ownerresume:"):
        match = re.fullmatch(r"ownerresume:(yes|no):([0-9a-fA-F-]{36})", msg.reply_id)
        if match:
            services = await _catalog(ctx, salon["id"])
            tz = ZoneInfo(salon["timezone"])
            await resume_late_owner_booking(ctx, salon, client, st, services, tz, datetime.now(tz), chat,
                                            lang, match[2], match[1])
            return
    if st.get("state") == "owner_check_pending":
        answer = (msg.text or "").strip().lower()
        if msg.kind == "text" and answer in {"cancel", "stop", "ghairi", "sitisha"}:
            if await cancel_owner_availability_request(ctx, salon, msg.sender, st, chat, lang):
                return
        pending = await ctx.pool.fetchrow(
            "SELECT owner_check_status, owner_check_expires_at FROM bookings WHERE id=$1::uuid AND salon_id=$2::uuid",
            st.get("booking_id"), salon["id"])
        if pending and pending["owner_check_status"] == "approved":
            await owner_availability_response(ctx, salon, st["booking_id"], "yes")
            return
        if (pending and pending["owner_check_status"] == "pending" and pending["owner_check_expires_at"]
                and pending["owner_check_expires_at"] <= datetime.now(ZoneInfo("UTC"))):
            await expire_owner_availability_checks(ctx)
            return
        await _reply(ctx, salon["id"], msg.sender, st, chat,
                     t(lang, "owner_manual_waiting", service=st.get("service_name") or "your booking",
                       when=st.get("held_when") or "your selected time", stylist=st.get("held_stylist") or "your stylist"))
        return
    if st.get("state") == "owner_late_offer":
        answer = (msg.text or "").strip().lower()
        yes = answer in {"yes", "yeah", "yep", "yup", "okay", "ok", "sawa", "ndio", "ndiyo", "ndioyo"}
        no = answer in {"no", "nope", "hapana", "sio"}
        if yes or no:
            services = await _catalog(ctx, salon["id"])
            tz = ZoneInfo(salon["timezone"])
            await resume_late_owner_booking(ctx, salon, client, st, services, tz, datetime.now(tz), chat,
                                            lang, st.get("owner_late_booking_id", ""), "yes" if yes else "no")
            return
        if st.get("owner_late_booking_id"):
            st = {**st, "state": "asking"}
            await _save(ctx, salon["id"], msg.sender, st)
    if msg.kind == "image" and msg.media_id and st.get("state") == "held" and st.get("booking_id"):
        acct = await payments.account(ctx.pool, salon["id"])
        if acct is not None and acct.get("manual"):
            guess = await _payment_screenshot(ctx, msg, outbound.salon_token(ctx, salon), payments.manual_provider(acct))
            if guess is None:
                await _reply(ctx, salon["id"], msg.sender, st, chat, t(lang, "till_photo_unreadable", salon=salon["name"]))
            else:
                await _till_claim(ctx, salon, client, st, chat, lang, "", extracted=guess)
            return
    if msg.kind == "image" and msg.media_id and ctx.vision is not None and ctx.vision.enabled:
        services = await _catalog(ctx, salon["id"])
        service = await _see(ctx, msg, services, outbound.salon_token(ctx, salon))
        if service is None:
            await _reply(ctx, salon["id"], msg.sender, st, chat, t(lang, "photo_unclear", menu=menu(services)))
            return
        st = {"state": "asking", "service_name": service["name"], "date": None, "window": None, "time": None, "history": st.get("history", [])}
        await _save(ctx, salon["id"], msg.sender, st)
        await _reply(ctx, salon["id"], msg.sender, st, chat, t(lang, "photo_match", service=service["name"], price=service["price_kes"], mins=service["duration_min"]))
        if not msg.text:
            return
        msg = dataclasses.replace(msg, kind="text", reply_id=None)
    if msg.kind not in ("text", "interactive") or not msg.text:
        await _reply(ctx, salon["id"], msg.sender, st, chat, t(lang, "text_only"))
        return

    tz = ZoneInfo(salon["timezone"])
    now = datetime.now(tz)
    services = await _catalog(ctx, salon["id"])

    tap_id: str | None = None
    if msg.reply_id and msg.reply_id.startswith("slot:"):
        tap_id = msg.reply_id
    elif msg.text.strip() in ("1", "2", "3") and st.get("state") in ("offered", "held"):
        tap_id = f"num:{msg.text.strip()}"
    if tap_id and _tap_is_duplicate(st, tap_id, now):
        log.info("duplicate tap ignored %s %s", mask(msg.sender), tap_id)
        return
    if msg.reply_id and msg.reply_id.startswith("late-"):
        await _late_tap(ctx, salon, client, st, services, tz, now, chat, lang, msg.reply_id)
        return
    if msg.reply_id and msg.reply_id.startswith("visit-"):
        await _visit_tap(ctx, salon, client, st, tz, chat, lang, msg.reply_id)
        return
    if msg.reply_id and msg.reply_id.startswith("wait-"):
        if await waitlist.handle_tap(ctx, salon, client, st, chat, lang, tz, msg.reply_id):
            return
    if msg.reply_id and msg.reply_id.startswith("still-"):
        if await handle_still_tap(ctx, salon, client, st, chat, lang, tz, msg.reply_id):
            return
    if st.get("state") == "awaiting_feedback" and (msg.text or "").strip() and not msg.reply_id and not msg.media_id:
        quick = rule_extract(msg.text, now.date(), services)
        if quick.intent == "other":
            await ctx.pool.execute("UPDATE visit_checks SET feedback=$2, answered_at=now() WHERE booking_id=$1::uuid",
                                   st["visit_booking"], msg.text.strip()[:500])
            cleared_feedback = {"history": st.get("history", [])}
            await _reply(ctx, salon["id"], msg.sender, cleared_feedback, chat, t(lang, "visit_feedback_thanks"))
            return
    if st.get("state") == "held" and tap_id:
        svc_name = next((s["name"] for s in services if s["id"] == st.get("service_id")), st.get("service_name") or "your booking")
        when = st.get("held_when") or "your booked time"
        stylist = st.get("held_stylist") or "your stylist"
        acct = await payments.account(ctx.pool, salon["id"])
        await _reply(ctx, salon["id"], msg.sender, st, chat,
                     t(lang, "already_held", service=svc_name, when=when, stylist=stylist,
                       destination=payments.destination_label(acct, lang)))
        st["already_held_sent"] = True
        st["last_tap"] = {"id": tap_id, "at": now.isoformat()}
        await _save(ctx, salon["id"], msg.sender, st)
        return

    if st.get("state") == "held" and st.get("booking_id"):
        crow = await ctx.pool.fetchrow("SELECT status FROM bookings WHERE id=$1::uuid", st["booking_id"])
        if not crow or crow["status"] != "held":
            await _save(ctx, salon["id"], msg.sender, {"history": st.get("history", [])})
            st = {"history": st.get("history", [])}
        else:
            quick_held_first = rule_extract(msg.text or "", now.date(), services, "held")
            if quick_held_first.intent != "cancel" and st.get("booking_id"):
                acct = await payments.account(ctx.pool, salon["id"])
                if acct is not None and acct.get("manual"):
                    await _till_claim(ctx, salon, client, st, chat, lang, msg.text or "")
                    return
            strict = mpesa.normalize_msisdn(msg.text or "")
            embedded = mpesa.extract_msisdn(msg.text or "") if not strict else None
            number = strict or embedded
            if number:
                await _pay(ctx, salon, st, services, tz, chat, lang, number, msg.text)
                return
            if PAY_WORDS.match(msg.text or ""):
                await _pay(ctx, salon, st, services, tz, chat, lang, st.get("phone") or mpesa.normalize_msisdn(msg.sender), msg.text)
                return
            if mpesa.looks_like_phone_attempt(msg.text or ""):
                await _reply(ctx, salon["id"], msg.sender, st, chat, t(lang, "invalid_phone"))
                return
            # Other text in the held state reaches the model below. The
            # payment instructions and phone-number validation remain here.

    if msg.reply_id and msg.reply_id.startswith("slot:") and msg.reply_id[5:].isdigit():
        res = Result(Extraction(intent="choose_slot", choice=int(msg.reply_id[5:]) + 1, language=lang), "button")
    elif msg.text.strip() in ("1", "2", "3") and st.get("state") == "offered":
        res = Result(Extraction(intent="choose_slot", choice=int(msg.text.strip()), language=lang), "button")
    else:
        where = screen.route(msg.text, services, st.get("state", "idle"), now.date())
        if where == "off" and agent.enabled(ctx) and any(h.get("role") == "assistant" for h in (st.get("history") or [])[-6:]):
            where = "model"      # short follow-ups need the model to read recent context
        if agent.enabled(ctx):
            lang = await _speak(ctx, client, lang, msg.text or "")
            if await agent.turn(ctx, salon, client, st, services, tz, now, chat, lang, msg):
                return
        if smalltalk.asks_help(msg.text or ""):
            lang = await _speak(ctx, client, lang, msg.text or "")
            await _reply(ctx, salon["id"], msg.sender, st, chat,
                         t(lang, "assistant_help", salon=salon["name"], example=example_for(services, lang)))
            return
        if WAITLIST_WORDS.match(msg.text or "") and st.get("state") == "no_slots" and st.get("service_id") and st.get("date"):
            service = next((s for s in services if s["id"] == st["service_id"]), None)
            if service is not None:
                outcome = await waitlist.join(ctx.pool, salon["id"], client["id"], service["id"], date.fromisoformat(st["date"]))
                key = "waitlist_joined" if outcome == "joined" else "waitlist_already"
                await _reply(ctx, salon["id"], chat.to, st, chat, t(lang, key, service=service["name"], day=st["date"]))
                return
        if st.get("state") == "held" and st.get("booking_id"):
            quick_held = rule_extract(msg.text or "", now.date(), services, "held")
            acct = await payments.account(ctx.pool, salon["id"])
            reminder = (payments.manual_instructions(lang, int(st.get("amount") or 0), acct, salon["name"])
                        if acct and acct.get("manual") else
                        t(lang, "remind_number", service=st.get("service_name") or "your booking",
                          when=st.get("held_when") or "your booked time",
                          stylist=st.get("held_stylist") or "your stylist", amount=st.get("amount") or 0))
            if quick_held.intent == "price_list":
                note = ""
                if int(salon["deposit_kes"]) > 0 and acct:
                    note = "\n\n" + payments.deposit_note(lang, int(salon["deposit_kes"]), acct)
                await _reply(ctx, salon["id"], msg.sender, st, chat, t(lang, "prices", lines=menu(services)) + note + "\n\n" + reminder)
                return
            if quick_held.intent == "hours":
                await _reply(ctx, salon["id"], msg.sender, st, chat, t(lang, "hours", lines=_hours(salon, lang)) + "\n\n" + reminder)
                return
            if quick_held.intent == "greeting":
                hello = smalltalk.opener(msg.text) or "Hi"
                await _reply(ctx, salon["id"], msg.sender, st, chat,
                             f"{hello} " + t(lang, "welcome", salon=salon["name"], example=example_for(services, lang)) + "\n\n" + reminder)
                return
            if quick_held.intent != "cancel":
                if st.get("phone"):
                    svc_name = next((s["name"] for s in services if s["id"] == st.get("service_id")), st.get("service_name") or "your booking")
                    await _reply(ctx, salon["id"], msg.sender, st, chat,
                                 t(lang, "already_held", service=svc_name,
                                   when=st.get("held_when") or "your booked time",
                                   stylist=st.get("held_stylist") or "your stylist",
                                   destination=payments.destination_label(acct, lang)))
                else:
                    await _reply(ctx, salon["id"], msg.sender, st, chat, reminder)
                return
        pleasant = smalltalk.thanks(msg.text or "")
        if pleasant and st.get("state", "idle") == "idle" and rule_extract(msg.text or "", now.date(), services, "idle").intent in ("other", "greeting"):
            key, speaking = pleasant
            if speaking != lang:
                lang = speaking
                await ctx.pool.execute("UPDATE clients SET lang=$2 WHERE id=$1::uuid", client["id"], lang)
            await _reply(ctx, salon["id"], msg.sender, st, chat, t(lang, key))
            return
        if where == "off":
            log.info("screened out after model fallback: nothing to do with a salon (%d chars) text=%r", len(msg.text), (msg.text or "")[:120])
            lang = await _speak(ctx, client, lang, msg.text or "")
            await _reply(ctx, salon["id"], msg.sender, st, chat, t(lang, "off_topic", salon=salon["name"], example=example_for(services, lang)))
            return
        quick = rule_extract(msg.text, now.date(), services, st.get("state", "idle"))
        if where == "rules" or quick.intent == "greeting":
            res = Result(quick, "rules")
        else:
            res = await ctx.llm.extract(msg.text, today=now.date(), services=services, state=st.get("state", "idle"), history=st.get("history"),
                                        salon_id=salon["id"])
            if res.ex.intent == "other" and quick.intent == "book" and (quick.service or quick.date or quick.window or quick.time):
                res = Result(quick, "keywords")
    ex = res.ex
    log.info("understood intent=%s via %s%s %s %dms", ex.intent, res.source, " degraded" if res.degraded else "", res.model, res.ms)
    if ex.intent != "other" and ex.language != lang:
        lang = ex.language
        await ctx.pool.execute("UPDATE clients SET lang=$2 WHERE id=$1::uuid", client["id"], lang)

    hello = smalltalk.opener(msg.text)
    if hello and ex.intent == "other" and smalltalk.detect(msg.text)[1]:
        ex = Extraction(intent="greeting", language=lang)
    if hello and ex.intent != "greeting":
        await _reply(ctx, salon["id"], msg.sender, st, chat, hello)

    env = (ctx, salon, client, st, services, tz, now, chat, lang)
    if ex.intent == "cancel":
        await _cancel(*env)
    elif ex.intent == "price_list":
        note = ""
        acct = await payments.account(ctx.pool, salon["id"])
        if int(salon["deposit_kes"]) > 0 and acct:
            note = "\n\n" + payments.deposit_note(lang, int(salon["deposit_kes"]), acct)
        await _reply(ctx, salon["id"], msg.sender, st, chat, t(lang, "prices", lines=menu(services)) + note)
    elif ex.intent == "hours":
        await _reply(ctx, salon["id"], msg.sender, st, chat, t(lang, "hours", lines=_hours(salon, lang)))
    elif ex.intent == "greeting":
        if await ctx.pool.fetchval("SELECT 1 FROM bookings WHERE client_id=$1::uuid AND (starts_at < now() OR status IN ('confirmed','no_show')) LIMIT 1", client["id"]):
            await _reply(ctx, salon["id"], msg.sender, st, chat, f"{hello or smalltalk.opener('hi')} " + t(lang, "welcome_back_ask", example=example_for(services, lang)))
        else:
            await _reply(ctx, salon["id"], msg.sender, st, chat, f"{hello or smalltalk.opener('hi')} " + t(lang, "welcome", salon=salon["name"], example=example_for(services, lang)))
    elif ex.intent in ("book", "choose_slot"):
        await _book(*env, ex=ex, tap_id=tap_id)
    else:
        lang = await _speak(ctx, client, lang, msg.text or "")
        key = "fallback_degraded" if res.degraded else "fallback"
        await _reply(ctx, salon["id"], msg.sender, st, chat, t(lang, key, salon=salon["name"], example=example_for(services, lang)))


async def _voice_typing(ctx, to: str) -> None:
    """Show progress during slower speech-model starts where the channel supports it."""
    try:
        await ctx.wa.typing(to)
    except Exception:
        pass


async def _hear(ctx, msg: Message, lang: str, token: str, salon_id: str | None = None) -> str | None:
    """Voice note -> text. Swahili speakers are known by their earlier messages; otherwise the model works out the language itself."""
    got = await ctx.wa.download_media(token, msg.media_id)
    if got is None:
        return None
    heard = await ctx.voice.transcribe(got[0], got[1], "swh_Latn" if lang == "sw" else None, salon_id=salon_id)
    return heard.text if heard else None


async def _see(ctx, msg: Message, services: list[dict], token: str) -> dict | None:
    """Photo -> one of this salon's services, or None. The model suggests; the menu decides."""
    got = await ctx.wa.download_media(token, msg.media_id, max_bytes=4_000_000)
    if got is None:
        return None
    guess = await ctx.vision.match(got[0], got[1], [s["name"] for s in services], msg.text)
    if guess is None or guess.confidence < 0.5:
        return None
    log.info("photo matched to a service via %s in %d ms (confidence %.2f)", guess.model, guess.ms, guess.confidence)
    return match_service(guess.service, services)


async def _payment_screenshot(ctx, msg: Message, token: str, expected_provider: str):
    """Extract an unverified claim from an image; never treat it as payment confirmation."""
    if ctx.vision is None or not ctx.vision.enabled:
        return None
    got = await ctx.wa.download_media(token, msg.media_id, max_bytes=4_000_000)
    if got is None:
        return None
    return await ctx.vision.extract_payment(got[0], got[1], expected_provider)


async def _speak(ctx, client: dict, lang: str, text: str) -> str:
    """The language this message is written in, persisted when it changed. Stops one
    Swahili greeting from pinning every later English reply to Swahili."""
    speaking = smalltalk.language(text, lang)
    if speaking != lang:
        await ctx.pool.execute("UPDATE clients SET lang=$2 WHERE id=$1::uuid", client["id"], speaking)
    return speaking


def _hours(salon: dict, lang: str) -> str:
    out = []
    for k in DAY_KEYS:
        v = salon["open_hours"].get(k)
        out.append(f"{k.capitalize()}: {v[0]}-{v[1]}" if v else f"{k.capitalize()}: {t(lang, 'closed')}")
    return "\n".join(out)




