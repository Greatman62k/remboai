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

from . import binding, guard, locks, mpesa, onboarding_chat, owner, payments, platform, screen, smalltalk, waitlist
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
    example_for,
    match_service,
    menu,
    peek_state,
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
        linked = True
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
    """The shared bot with no businesses on it yet. It can only introduce itself,
    tell a sender their own id for setup, and register the first business.

    Every exchange lands in stranger_state, so these visits stay checkable, and the
    third unrecognized message gets a one-line nudge instead of the full intro."""
    text = msg.text or ""
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
    if msg.kind == "text" and (lowered in ("/start register", "/start sajili", "/start anzisha", "start register", "start sajili",
                                           "start anzisha", "register", "sajili", "anzisha") or onboarding_chat.wants_register(text)):
        if lowered not in ("register", "sajili", "anzisha") and onboarding_chat.wants_register(text):
            ack = t(onboarding_chat.detect_lang(text), "ob_register_ack")
            hist.append({"role": "assistant", "text": ack[:500], "ts": datetime.now(ZoneInfo("UTC")).isoformat()})
            await outbound.send_plain(ctx, msg.sender, ack)
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
    if platform.is_platform(ctx.settings, msg.sender):
        if await platform.handle(ctx, msg.sender, msg.text or ""):
            return
    salon = await ctx.pool.fetchrow(
        "SELECT id::text, name, wa_phone_number_id, wa_access_token, timezone, open_hours, deposit_kes, owner_wa_id FROM salons WHERE (wa_phone_number_id=$1 OR tg_bot_id=$1) AND active ORDER BY created_at, id LIMIT 1",
        msg.phone_number_id)
    if salon is None:
        if msg.sender.startswith("tg:"):
            await _unallocated(ctx, msg)
        else:
            log.warning("message for a number or bot we do not serve: %s", msg.phone_number_id)
        return
    salon = dict(salon)
    if isinstance(salon["open_hours"], str):
        salon["open_hours"] = json.loads(salon["open_hours"])
    salon, linked = await _resolve_salon(ctx, msg, salon)
    chat = Chat(ctx, salon, msg.sender)
    sess = await onboarding_chat.load_session(ctx.pool, msg.sender)
    if sess is not None:
        text, media = msg.text or "", ({"kind": "image"} if msg.kind == "image" else None)
        if msg.kind == "audio" and msg.media_id and sess.get("step") == "ask_prices" and ctx.voice is not None and ctx.voice.enabled:
            heard = await _hear(ctx, msg, sess.get("lang") or "en", outbound.salon_token(ctx, salon), salon["id"])
            if heard is None:
                await chat.text(t(sess.get("lang") or "en", "voice_failed"))
                return
            text = guard.clean(heard)
            await chat.text(t(sess.get("lang") or "en", "voice_heard", text=text))
        await onboarding_chat.advance(ctx, msg.sender, msg.phone_number_id, text, media)
        return
    if msg.kind == "text" and (msg.text or "").strip().lower() in ("/start register", "/start sajili", "/start anzisha", "start register", "start sajili", "start anzisha", "register", "sajili", "anzisha"):
        await onboarding_chat.begin(ctx, msg.sender, msg.phone_number_id, msg.text or "")
        return
    if msg.kind == "text" and onboarding_chat.wants_register(msg.text or ""):
        lang = onboarding_chat.detect_lang(msg.text or "")
        await chat.text(t(lang, "ob_register_ack"))
        await onboarding_chat.begin(ctx, msg.sender, msg.phone_number_id, msg.text or "")
        return
    if msg.kind == "text" and (smalltalk.asks_identity(msg.text or "") or (smalltalk.asks_help(msg.text or "") and not owner.is_owner(salon, msg.sender))):
        await _identity(ctx, msg, salon, linked, chat)
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
    if owner.is_owner(salon, msg.sender) and msg.kind == "text" and msg.text:
        tz0 = ZoneInfo(salon["timezone"])
        if await owner.handle(ctx, salon, msg.sender, msg.text, tz0, datetime.now(tz0)):
            return
    client, st = await _load(ctx, salon["id"], msg.sender, msg.name)
    st = await _replay_aware_state(ctx, salon["id"], msg, st)
    lang = client["lang"]
    if await guard.owner_command(ctx, salon, msg.sender, msg.text or "", chat):
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
    hit = guard.classify(msg.text or "")
    if hit:
        cat, hlang = hit
        use_lang = hlang if hlang in ("en", "sw") else lang
        await guard.hand_off(ctx, salon, client, use_lang, cat, msg.text or "")
        st = {**st, "state": "handoff", "category": cat}
        await _reply(ctx, salon["id"], msg.sender, st, chat, guard.reply_for(cat, use_lang))
        return
    st.setdefault("history", []).append({"role": "user", "text": msg.text or "", "ts": datetime.now(ZoneInfo(salon["timezone"])).isoformat()})
    if len(st["history"]) > guard.MAX_HISTORY * 2:
        st["history"] = st["history"][-guard.MAX_HISTORY * 2 :]
    if msg.kind == "audio" and msg.media_id and ctx.voice is not None and ctx.voice.enabled:
        heard = await _hear(ctx, msg, lang, outbound.salon_token(ctx, salon), salon["id"])
        if heard is None:
            await _reply(ctx, salon["id"], msg.sender, st, chat, t(lang, "voice_failed"))
            return
        await _reply(ctx, salon["id"], msg.sender, st, chat, t(lang, "voice_heard", text=heard))
        msg = dataclasses.replace(msg, kind="text", text=guard.clean(heard), reply_id=None)
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
    if WAITLIST_WORDS.match(msg.text or "") and st.get("state") == "no_slots" and st.get("service_id") and st.get("date"):
        service = next((s for s in services if s["id"] == st["service_id"]), None)
        if service is not None:
            outcome = await waitlist.join(ctx.pool, salon["id"], client["id"], service["id"], date.fromisoformat(st["date"]))
            key = "waitlist_joined" if outcome == "joined" else "waitlist_already"
            await _reply(ctx, salon["id"], chat.to, st, chat, t(lang, key, service=service["name"], day=st["date"]))
            return
    if msg.reply_id and msg.reply_id.startswith("till:"):
        if owner.is_owner(salon, msg.sender) and await owner.handle_tap(ctx, salon, msg.sender, msg.reply_id):
            return
    if st.get("state") == "awaiting_feedback" and (msg.text or "").strip() and not msg.reply_id and not msg.media_id:
        quick = rule_extract(msg.text, now.date(), services)
        if quick.intent == "other":
            await ctx.pool.execute("UPDATE visit_checks SET feedback=$2, answered_at=now() WHERE booking_id=$1::uuid",
                                   st["visit_booking"], msg.text.strip()[:500])
            cleared_feedback = {"history": st.get("history", [])}
            await _save(ctx, salon["id"], chat.to, cleared_feedback)
            await _reply(ctx, salon["id"], chat.to, cleared_feedback, chat, t(lang, "visit_feedback_thanks"))
            return
    if st.get("state") == "held" and tap_id:
        svc_name = next((s["name"] for s in services if s["id"] == st.get("service_id")), st.get("service_name") or "your booking")
        when = st.get("held_when") or "your booked time"
        stylist = st.get("held_stylist") or "your stylist"
        await _reply(ctx, salon["id"], msg.sender, st, chat, t(lang, "already_held", service=svc_name, when=when, stylist=stylist))
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
            quick_held = rule_extract(msg.text or "", now.date(), services, st.get("state", "held"))
            if quick_held.intent == "cancel":
                pass
            elif quick_held.intent in ("price_list", "hours", "greeting"):
                if quick_held.intent == "price_list":
                    note = ""
                    if int(salon["deposit_kes"]) > 0 and await payments.account(ctx.pool, salon["id"]):
                        note = "\n\n" + t(lang, "deposit_note", amount=int(salon["deposit_kes"]))
                    await _reply(ctx, salon["id"], msg.sender, st, chat, t(lang, "prices", lines=menu(services)) + note + "\n\n" + t(lang, "remind_number", service=st.get("service_name") or "your booking", when=st.get("held_when") or "your booked time", stylist=st.get("held_stylist") or "your stylist", amount=st.get("amount") or 0))
                    return
                if quick_held.intent == "hours":
                    await _reply(ctx, salon["id"], msg.sender, st, chat, t(lang, "hours", lines=_hours(salon, lang)) + "\n\n" + t(lang, "remind_number", service=st.get("service_name") or "your booking", when=st.get("held_when") or "your booked time", stylist=st.get("held_stylist") or "your stylist", amount=st.get("amount") or 0))
                    return
                if quick_held.intent == "greeting":
                    hello = smalltalk.opener(msg.text) or "Hi"
                    await _reply(ctx, salon["id"], msg.sender, st, chat, f"{hello} " + t(lang, "welcome", salon=salon["name"], example=example_for(services, lang)) + "\n\n" + t(lang, "remind_number", service=st.get("service_name") or "your booking", when=st.get("held_when") or "your booked time", stylist=st.get("held_stylist") or "your stylist", amount=st.get("amount") or 0))
                    return
            elif st.get("phone"):
                svc_name = next((s["name"] for s in services if s["id"] == st.get("service_id")), st.get("service_name") or "your booking")
                when = st.get("held_when") or "your booked time"
                stylist = st.get("held_stylist") or "your stylist"
                await _reply(ctx, salon["id"], msg.sender, st, chat, t(lang, "already_held", service=svc_name, when=when, stylist=stylist))
                return
            elif quick_held.intent != "cancel":
                svc_name = st.get("service_name") or "your booking"
                when = st.get("held_when") or "your booked time"
                stylist = st.get("held_stylist") or "your stylist"
                await _reply(ctx, salon["id"], msg.sender, st, chat, t(lang, "remind_number", service=svc_name, when=when, stylist=stylist, amount=st.get("amount") or 0))
                return

    if msg.reply_id and msg.reply_id.startswith("slot:") and msg.reply_id[5:].isdigit():
        res = Result(Extraction(intent="choose_slot", choice=int(msg.reply_id[5:]) + 1, language=lang), "button")
    elif msg.text.strip() in ("1", "2", "3") and st.get("state") == "offered":
        res = Result(Extraction(intent="choose_slot", choice=int(msg.text.strip()), language=lang), "button")
    elif guard.injection(msg.text):
        log.warning("prompt-injection attempt from %s: no model call", mask(msg.sender))
        await guard.strike(ctx.pool, msg.sender, "injection")
        await _reply(ctx, salon["id"], msg.sender, st, chat, t(lang, "off_topic", salon=salon["name"], example=example_for(services, lang)))
        return
    else:
        pleasant = smalltalk.thanks(msg.text or "")
        if pleasant and st.get("state", "idle") == "idle" and rule_extract(msg.text or "", now.date(), services, "idle").intent in ("other", "greeting"):
            key, speaking = pleasant
            if speaking != lang:
                lang = speaking
                await ctx.pool.execute("UPDATE clients SET lang=$2 WHERE id=$1::uuid", client["id"], lang)
            await _reply(ctx, salon["id"], msg.sender, st, chat, t(lang, key))
            return
        where = screen.route(msg.text, services, st.get("state", "idle"), now.date())
        if where == "off":
            log.info("screened out: nothing to do with a salon (%d chars) text=%r", len(msg.text), (msg.text or "")[:120])
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
        if int(salon["deposit_kes"]) > 0 and await payments.account(ctx.pool, salon["id"]):
            note = "\n\n" + t(lang, "deposit_note", amount=int(salon["deposit_kes"]))
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
        await _reply(ctx, salon["id"], msg.sender, st, chat, t(lang, key, example=example_for(services, lang)))


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




