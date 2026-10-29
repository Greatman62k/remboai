"""The conversation: understand (model, or plain rules if no model answers), then let code decide.
One customer at a time (a database lock per sender); what we remember about them lives in Postgres."""
from __future__ import annotations

import dataclasses
import difflib
import json
import logging
import re
from datetime import date, datetime, time, timedelta
from zoneinfo import ZoneInfo

from . import guard, mpesa, owner, payments, screen, smalltalk
from .availability import DAY_KEYS, Slot, choose, day_slots
from .bookings import busy_times, hold_slot, release
from .llm import Extraction, Result, rule_extract
from .messages import t
from .whatsapp import Message, mask

log = logging.getLogger("rembo.conv")
PAY_WORDS = re.compile(r"^\s*(pay|lipa|retry|resend|tena|try again)(\s+(now|sasa))?\W*$", re.I)
HORIZON_DAYS = 14


class Chat:
    """Sends to one customer on one salon's number."""

    def __init__(self, ctx, salon, to: str):
        self.ctx, self.pid, self.to = ctx, salon["wa_phone_number_id"], to

    async def text(self, body: str) -> None:
        await self.ctx.wa.send_text(self.pid, self.ctx.settings.wa_access_token, self.to, body)

    async def buttons(self, body: str, buttons: list[tuple[str, str]]) -> None:
        await self.ctx.wa.send_buttons(self.pid, self.ctx.settings.wa_access_token, self.to, body, buttons)


async def _reply(ctx, salon_id: str, wa_id: str, st: dict, chat: Chat, text: str) -> None:
    st.setdefault("history", []).append({"role": "assistant", "text": text, "ts": datetime.now(ZoneInfo("UTC")).isoformat()})
    if len(st["history"]) > guard.MAX_HISTORY * 2:
        st["history"] = st["history"][-guard.MAX_HISTORY * 2 :]
    await _save(ctx, salon_id, wa_id, st)
    await chat.text(text)


async def _reply_buttons(ctx, salon_id: str, wa_id: str, st: dict, chat: Chat, body: str, buttons) -> None:
    st.setdefault("history", []).append({"role": "assistant", "text": body, "ts": datetime.now(ZoneInfo("UTC")).isoformat()})
    if len(st["history"]) > guard.MAX_HISTORY * 2:
        st["history"] = st["history"][-guard.MAX_HISTORY * 2 :]
    await _save(ctx, salon_id, wa_id, st)
    await chat.buttons(body, buttons)


def when_label(dt: datetime, tz: ZoneInfo) -> str:
    d = dt.astimezone(tz)
    return d.strftime("%a %d %b, %H:%M")


def match_service(name: str | None, services: list[dict]) -> dict | None:
    if not name:
        return None
    keys: dict[str, dict] = {}
    for s in services:
        keys[s["name"].lower()] = s
        for a in s["aliases"]:
            keys.setdefault(a.lower(), s)
    q = name.lower().strip()
    if q in keys:
        return keys[q]
    close = difflib.get_close_matches(q, list(keys), n=1, cutoff=0.75)
    return keys[close[0]] if close else None


def menu(services: list[dict]) -> str:
    return "\n".join(f"- {s['name']} (KES {s['price_kes']})" for s in services)


async def handle(ctx, msg: Message) -> None:
    """Serialise one sender's messages with a session advisory lock, so rapid taps cannot interleave."""
    async with ctx.pool.acquire() as lock:
        key = f"conv:{msg.phone_number_id}:{msg.sender}"
        await lock.execute("SELECT pg_advisory_lock(hashtext($1))", key)
        try:
            await _handle(ctx, msg)
        finally:
            await lock.execute("SELECT pg_advisory_unlock(hashtext($1))", key)


async def _load(ctx, salon_id: str, wa_id: str, name: str | None) -> tuple[dict, dict]:
    client = await ctx.pool.fetchrow(
        "INSERT INTO clients (salon_id, wa_id, name) VALUES ($1::uuid,$2,$3) "
        "ON CONFLICT (salon_id, wa_id) DO UPDATE SET name = COALESCE(EXCLUDED.name, clients.name) "
        "RETURNING id::text, lang, name", salon_id, wa_id, name)
    raw = await ctx.pool.fetchval("SELECT state FROM conversations WHERE salon_id=$1::uuid AND wa_id=$2", salon_id, wa_id)
    state = raw if isinstance(raw, dict) else json.loads(raw or "{}")
    if not isinstance(state.get("history"), list):
        state["history"] = []
    return dict(client), state or {"history": []}


async def _save(ctx, salon_id: str, wa_id: str, state: dict) -> None:
    hist = state.get("history", [])
    if len(hist) > guard.MAX_HISTORY * 2:
        state["history"] = hist[-guard.MAX_HISTORY * 2 :]
    await ctx.pool.execute(
        "INSERT INTO conversations (salon_id, wa_id, state) VALUES ($1::uuid,$2,$3::jsonb) "
        "ON CONFLICT (salon_id, wa_id) DO UPDATE SET state=EXCLUDED.state, updated_at=now()", salon_id, wa_id, json.dumps(state))


async def _catalog(ctx, salon_id: str) -> list[dict]:
    rows = await ctx.pool.fetch(
        "SELECT s.id::text, s.name, s.aliases, s.duration_min, s.price_kes, "
        "COALESCE(array_agg(ss.staff_id::text) FILTER (WHERE st.active), '{}') AS staff_ids "
        "FROM services s LEFT JOIN staff_services ss ON ss.service_id = s.id LEFT JOIN staff st ON st.id = ss.staff_id "
        "WHERE s.salon_id=$1::uuid AND s.active GROUP BY s.id ORDER BY s.name", salon_id)
    return [dict(r) for r in rows]


async def _handle(ctx, msg: Message) -> None:
    salon = await ctx.pool.fetchrow(
        "SELECT id::text, name, wa_phone_number_id, timezone, open_hours, deposit_kes, owner_wa_id FROM salons WHERE (wa_phone_number_id=$1 OR tg_bot_id=$1) AND active",
        msg.phone_number_id)
    if salon is None:
        log.warning("message for a number or bot we do not serve: %s", msg.phone_number_id)
        return
    salon = dict(salon)
    if isinstance(salon["open_hours"], str):
        salon["open_hours"] = json.loads(salon["open_hours"])
    chat = Chat(ctx, salon, msg.sender)
    if owner.is_owner(salon, msg.sender) and msg.kind == "text" and msg.text:
        tz0 = ZoneInfo(salon["timezone"])
        if await owner.handle(ctx, salon, msg.sender, msg.text, tz0, datetime.now(tz0)):
            return
    client, st = await _load(ctx, salon["id"], msg.sender, msg.name)
    lang = client["lang"]
    if await guard.owner_command(ctx, salon, msg.sender, msg.text or "", chat):
        return
    if st.get("state") == "handoff":
        created = await ctx.pool.fetchval(
            "SELECT created_at FROM handoffs WHERE salon_id=$1::uuid AND client_id=$2::uuid AND status='open' ORDER BY created_at DESC LIMIT 1",
            salon["id"], client["id"],
        )
        if created and (datetime.now(ZoneInfo(salon["timezone"])) - created).total_seconds() < guard.HANDOFF_HOURS * 3600:
            await _reply(ctx, salon["id"], msg.sender, st, chat, guard.reply_for("waiting", lang))
            return
        await guard.clear_handoff(ctx.pool, salon["id"], msg.sender)
        st = {"history": st.get("history", [])}
    hit = guard.classify(msg.text or "")
    if hit:
        cat, hlang = hit
        use_lang = hlang if hlang in ("en", "sw") else lang
        await guard.hand_off(ctx, salon, client, use_lang, cat, msg.text or "")
        await _reply(ctx, salon["id"], msg.sender, st, chat, guard.reply_for(cat, use_lang))
        return
    st.setdefault("history", []).append({"role": "user", "text": msg.text or "", "ts": datetime.now(ZoneInfo(salon["timezone"])).isoformat()})
    if len(st["history"]) > guard.MAX_HISTORY * 2:
        st["history"] = st["history"][-guard.MAX_HISTORY * 2 :]
    if msg.kind == "audio" and msg.media_id and ctx.voice is not None and ctx.voice.enabled:
        heard = await _hear(ctx, msg, lang)
        if heard is None:
            await _reply(ctx, salon["id"], msg.sender, st, chat, t(lang, "voice_failed"))
            return
        await _reply(ctx, salon["id"], msg.sender, st, chat, t(lang, "voice_heard", text=heard))  # say what was understood, so a misheard word can be corrected
        msg = dataclasses.replace(msg, kind="text", text=guard.clean(heard), reply_id=None)
    if msg.kind == "image" and msg.media_id and ctx.vision is not None and ctx.vision.enabled:
        services = await _catalog(ctx, salon["id"])
        service = await _see(ctx, msg, services)
        if service is None:
            await _reply(ctx, salon["id"], msg.sender, st, chat, t(lang, "photo_unclear", menu=menu(services)))
            return
        st = {"state": "asking", "service_name": service["name"], "date": None, "window": None, "time": None, "history": st.get("history", [])}
        await _save(ctx, salon["id"], msg.sender, st)
        await _reply(ctx, salon["id"], msg.sender, st, chat, t(lang, "photo_match", service=service["name"], price=service["price_kes"], mins=service["duration_min"]))
        if not msg.text:
            return
        msg = dataclasses.replace(msg, kind="text", reply_id=None)  # a caption such as "kesho mchana" carries on with the service chosen from the photo
    if msg.kind not in ("text", "interactive") or not msg.text:
        await _reply(ctx, salon["id"], msg.sender, st, chat, t(lang, "text_only"))
        return

    tz = ZoneInfo(salon["timezone"])
    now = datetime.now(tz)
    services = await _catalog(ctx, salon["id"])

    # While a deposit is due, a phone number or "pay" needs no model: it is an instruction for the payment.
    if st.get("state") == "held" and st.get("booking_id"):
        number = mpesa.normalize_msisdn(msg.text)
        if number or PAY_WORDS.match(msg.text):
            await _pay(ctx, salon, st, services, tz, chat, lang, number or st.get("phone") or mpesa.normalize_msisdn(msg.sender))
            return

    # Button taps and a bare number while slots are on offer need no model at all.
    if msg.reply_id and msg.reply_id.startswith("slot:") and msg.reply_id[5:].isdigit():
        res = Result(Extraction(intent="choose_slot", choice=int(msg.reply_id[5:]) + 1, language=lang), "button")
    elif msg.text.strip() in ("1", "2", "3") and st.get("state") == "offered":
        res = Result(Extraction(intent="choose_slot", choice=int(msg.text.strip()), language=lang), "button")
    elif guard.injection(msg.text):
        log.warning("prompt-injection attempt from %s: no model call", mask(msg.sender))
        await guard.strike(ctx.pool, msg.sender, "injection")
        await _reply(ctx, salon["id"], msg.sender, st, chat, t(lang, "off_topic", salon=salon["name"]))     # the same reply as any off-topic message: it does not say what was detected
        return
    else:
        where = screen.route(msg.text, services, st.get("state", "idle"), now.date())
        if where == "off":
            log.info("screened out: nothing to do with a salon (%d characters), no model call", len(msg.text))
            await _reply(ctx, salon["id"], msg.sender, st, chat, t(lang, "off_topic", salon=salon["name"]))
            return
        quick = rule_extract(msg.text, now.date(), services, st.get("state", "idle"))
        if where == "rules" or quick.intent == "greeting":
            res = Result(quick, "rules")
        else:
            res = await ctx.llm.extract(msg.text, today=now.date(), services=services, state=st.get("state", "idle"), history=st.get("history"))
    ex = res.ex
    log.info("understood intent=%s via %s %s %dms", ex.intent, res.source, res.model, res.ms)
    if ex.intent != "other" and ex.language != lang:
        lang = ex.language
        await ctx.pool.execute("UPDATE clients SET lang=$2 WHERE id=$1::uuid", client["id"], lang)

    hello = smalltalk.opener(msg.text)
    if hello and ex.intent == "other" and smalltalk.detect(msg.text)[1]:
        ex = Extraction(intent="greeting", language=lang)
    if hello and ex.intent != "greeting":
        await _reply(ctx, salon["id"], msg.sender, st, chat, hello)  # say hello back, then carry on with the request

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
        await _reply(ctx, salon["id"], msg.sender, st, chat, f"{hello or smalltalk.opener('hi')} " + t(lang, "welcome", salon=salon["name"]))
    elif ex.intent in ("book", "choose_slot"):
        await _book(*env, ex=ex)
    else:
        await _reply(ctx, salon["id"], msg.sender, st, chat, t(lang, "fallback"))


async def _hear(ctx, msg: Message, lang: str) -> str | None:
    """Voice note -> text. Swahili speakers are known by their earlier messages; otherwise the model works out the language itself."""
    got = await ctx.wa.download_media(ctx.settings.wa_access_token, msg.media_id)
    if got is None:
        return None
    heard = await ctx.voice.transcribe(got[0], got[1], "swh_Latn" if lang == "sw" else None)
    return heard.text if heard else None


async def _see(ctx, msg: Message, services: list[dict]) -> dict | None:
    """Photo -> one of this salon's services, or None. The model suggests; the menu decides."""
    got = await ctx.wa.download_media(ctx.settings.wa_access_token, msg.media_id, max_bytes=4_000_000)
    if got is None:
        return None
    guess = await ctx.vision.match(got[0], got[1], [s["name"] for s in services], msg.text)
    if guess is None or guess.confidence < 0.5:
        return None
    log.info("photo matched to a service via %s in %d ms (confidence %.2f)", guess.model, guess.ms, guess.confidence)
    return match_service(guess.service, services)


def _hours(salon: dict, lang: str) -> str:
    out = []
    for k in DAY_KEYS:
        v = salon["open_hours"].get(k)
        out.append(f"{k.capitalize()}: {v[0]}-{v[1]}" if v else f"{k.capitalize()}: {t(lang, 'closed')}")
    return "\n".join(out)


async def _cancel(ctx, salon, client, st, services, tz, now, chat, lang) -> None:
    row = await ctx.pool.fetchrow(
        "UPDATE bookings SET status='cancelled' WHERE id = (SELECT id FROM bookings WHERE client_id=$1::uuid AND status IN ('held','confirmed') "
        "AND starts_at > now() ORDER BY starts_at LIMIT 1) RETURNING id::text, starts_at", client["id"])
    if row is None:
        await _reply(ctx, salon["id"], chat.to, st, chat, t(lang, "nothing_to_cancel"))
        return
    await _save(ctx, salon["id"], chat.to, {})
    paid = await ctx.pool.fetchval("SELECT amount_kes FROM payments WHERE booking_id=$1::uuid AND status='paid'", row["id"])
    key = "cancelled_paid" if paid else "cancelled"
    await _reply(ctx, salon["id"], chat.to, st, chat, t(lang, key, when=when_label(row["starts_at"], tz), salon=salon["name"]))
    if paid and salon["owner_wa_id"]:
        await ctx.wa.send_text(salon["wa_phone_number_id"], ctx.settings.wa_access_token, salon["owner_wa_id"],
                               t("en", "owner_refund", amount=paid,
                                 phone=mpesa.normalize_msisdn(chat.to) or chat.to, ref="", when=when_label(row["starts_at"], tz), why="the customer cancelled"))


async def find_slots(ctx, salon, service: dict, day: date, window: str | None, at: time | None, tz, now) -> tuple[list[Slot], bool]:
    """Up to three slots for the request. exact=False means nothing matched and these are the closest alternatives."""
    staff = [{"id": sid, "name": n} for sid, n in
             [(r["id"], r["name"]) for r in await ctx.pool.fetch(
                 "SELECT id::text, name FROM staff WHERE id = ANY($1::uuid[]) AND active ORDER BY name", service["staff_ids"])]]
    if not staff:
        return [], True
    start = datetime.combine(day, time(0), tz)
    end = start + timedelta(days=HORIZON_DAYS + 1)
    async with ctx.pool.acquire() as con:
        busy = await busy_times(con, [s["id"] for s in staff], start, end)
    dur, earliest = timedelta(minutes=service["duration_min"]), now + timedelta(minutes=30)
    first_fallback: list[Slot] = []
    for offset in range(HORIZON_DAYS):
        slots = day_slots(day + timedelta(days=offset), salon["open_hours"], tz, dur, staff, busy, earliest)
        if not slots:
            continue
        picked, exact = choose(slots, window=window, at=at)
        if picked and exact:
            return picked, offset == 0
        if offset == 0:
            first_fallback = picked
    if first_fallback:
        return first_fallback, False
    return [], True


async def _offer(ctx, salon, st, service, day, window, at, tz, now, chat, lang, note_key: str | None = None) -> None:
    slots, exact = await find_slots(ctx, salon, service, day, window, at, tz, now)
    if not slots:
        state = {"history": st.get("history", [])}
        await _save(ctx, salon["id"], chat.to, state)
        await _reply(ctx, salon["id"], chat.to, state, chat, t(lang, "no_slots", service=service["name"]))
        return
    lines = "\n".join(t(lang, "slot_line", n=i + 1, when=when_label(s.starts_at, tz), stylist=s.staff_name) for i, s in enumerate(slots))
    state = {"state": "offered", "service_id": service["id"], "date": day.isoformat(), "window": window, "time": at.strftime("%H:%M") if at else None,
             "slots": [{"staff_id": s.staff_id, "staff_name": s.staff_name, "starts_at": s.starts_at.isoformat(), "ends_at": s.ends_at.isoformat()} for s in slots],
             "history": st.get("history", [])}
    await _save(ctx, salon["id"], chat.to, state)
    intro = t(lang, note_key) + "\n" if note_key else ""
    body = intro + t(lang, "offer", service=service["name"], note="" if exact else t(lang, "nearest"), lines=lines)
    await _reply_buttons(ctx, salon["id"], chat.to, state, chat, body, [(f"slot:{i}", when_label(s.starts_at, tz).replace(",", "")[:20]) for i, s in enumerate(slots)])


async def _book(ctx, salon, client, st, services, tz, now, chat, lang, ex: Extraction) -> None:
    if ex.intent == "choose_slot":
        await _choose(ctx, salon, client, st, services, tz, now, chat, lang, ex.choice or 0)
        return
    if st.get("state") == "held" and st.get("booking_id"):  # starting over: do not keep blocking the old slot
        await release(ctx.pool, st["booking_id"])
    # Remember answers across messages: "fade" now, "tomorrow" next.
    mem = st if st.get("state") in ("asking",) else {}
    service = match_service(ex.service, services) if ex.service else (match_service(mem.get("service_name"), services))
    if ex.service and service is None:
        await _reply(ctx, salon["id"], chat.to, st, chat, t(lang, "unknown_service", menu=menu(services)))
        return
    day = date.fromisoformat(ex.date) if ex.date else (date.fromisoformat(mem["date"]) if mem.get("date") else None)
    window = ex.window or mem.get("window")
    at = time.fromisoformat(ex.time) if ex.time else (time.fromisoformat(mem["time"]) if mem.get("time") else None)
    pending = {"state": "asking", "service_name": service["name"] if service else None, "date": day.isoformat() if day else None,
               "window": window, "time": at.strftime("%H:%M") if at else None, "history": st.get("history", [])}
    if service is None:
        await _save(ctx, salon["id"], chat.to, pending)
        await _reply(ctx, salon["id"], chat.to, pending, chat, t(lang, "ask_service", menu=menu(services)))
        return
    if day is None:
        await _save(ctx, salon["id"], chat.to, pending)
        await _reply(ctx, salon["id"], chat.to, pending, chat, t(lang, "ask_date"))
        return
    await _offer(ctx, salon, st, service, day, window, at, tz, now, chat, lang)


async def _choose(ctx, salon, client, st, services, tz, now, chat, lang, choice: int) -> None:
    slots = st.get("slots") or []
    if st.get("state") != "offered" or not 1 <= choice <= len(slots):
        await _reply(ctx, salon["id"], chat.to, st, chat, t(lang, "fallback"))
        return
    service = next((s for s in services if s["id"] == st["service_id"]), None)
    pick = slots[choice - 1]
    if service is None:
        await _reply(ctx, salon["id"], chat.to, st, chat, t(lang, "fallback"))
        return
    old = st.get("booking_id")
    if old:
        await release(ctx.pool, old)
    active = await ctx.pool.fetchval(
        "SELECT count(*) FROM bookings WHERE client_id=$1::uuid AND status IN ('held','confirmed') AND starts_at > now()", client["id"])
    if ctx.settings.max_active_bookings and active >= ctx.settings.max_active_bookings:
        state = {"history": st.get("history", [])}
        await _save(ctx, salon["id"], chat.to, state)
        await _reply(ctx, salon["id"], chat.to, state, chat, t(lang, "too_many_bookings", n=active, salon=salon["name"]))
        return
    booking_id = await hold_slot(
        ctx.pool, salon_id=salon["id"], staff_id=pick["staff_id"], service_id=service["id"], client_id=client["id"],
        starts_at=datetime.fromisoformat(pick["starts_at"]), ends_at=datetime.fromisoformat(pick["ends_at"]), minutes=ctx.settings.hold_minutes)
    if booking_id is None:  # somebody else got there first: show what is free now
        await _offer(ctx, salon, st, service, date.fromisoformat(st["date"]), st.get("window"),
                     time.fromisoformat(st["time"]) if st.get("time") else None, tz, now, chat, lang, note_key="slot_gone")
        return
    when = when_label(datetime.fromisoformat(pick["starts_at"]), tz)
    acct = await payments.account(ctx.pool, salon["id"])
    amount = payments.deposit_amount(salon, service) if acct else 0
    if amount == 0:
        await ctx.pool.execute("UPDATE bookings SET status='confirmed', hold_expires_at=NULL WHERE id=$1::uuid AND status='held'", booking_id)
        state = {"history": st.get("history", [])}
        await _save(ctx, salon["id"], chat.to, state)
        await _reply(ctx, salon["id"], chat.to, state, chat, t(lang, "booked_free", service=service["name"], when=when, stylist=pick["staff_name"], salon=salon["name"]))
        if salon["owner_wa_id"]:
            await ctx.wa.send_text(salon["wa_phone_number_id"], ctx.settings.wa_access_token, salon["owner_wa_id"],
                                   t("en", "owner_booked_free", client=client["name"] or mpesa.normalize_msisdn(chat.to) or "customer",
                                     service=service["name"], when=when, stylist=pick["staff_name"]))
        return
    held = t(lang, "held", service=service["name"], when=when, stylist=pick["staff_name"], mins=ctx.settings.hold_minutes)
    state = {"state": "held", "booking_id": booking_id, "amount": amount, "history": st.get("history", [])}
    phone = mpesa.normalize_msisdn(chat.to)
    if phone is None:
        await _save(ctx, salon["id"], chat.to, state)
        await _reply(ctx, salon["id"], chat.to, state, chat, held + "\n" + t(lang, "ask_number", amount=amount))
        return
    state_with_phone = {**state, "phone": phone}
    await _save(ctx, salon["id"], chat.to, state_with_phone)
    await _reply(ctx, salon["id"], chat.to, state_with_phone, chat, held + "\n" + await _prompt(ctx, salon, booking_id, phone, amount, lang))


async def _prompt(ctx, salon, booking_id: str, phone: str, amount: int, lang: str) -> str:
    """Send the M-Pesa prompt and say what happened."""
    result = await payments.request_deposit(ctx, salon, booking_id, phone, amount)
    if result == "failed":
        return t(lang, "stk_failed")
    return t(lang, "stk_wait" if result == "wait" else "stk_sent", amount=amount, phone=phone)


async def _pay(ctx, salon, st, services, tz, chat, lang, phone: str | None) -> None:
    """The customer sent a number, or said "pay": (re)send the deposit prompt for the booking being held."""
    row = await ctx.pool.fetchrow(
        "SELECT b.status, sv.price_kes FROM bookings b JOIN services sv ON sv.id=b.service_id WHERE b.id=$1::uuid", st["booking_id"])
    if row is None or row["status"] != "held":
        await _save(ctx, salon["id"], chat.to, {})
        await _reply(ctx, salon["id"], chat.to, st, chat, t(lang, "hold_gone"))
        return
    amount = st.get("amount") or min(int(salon["deposit_kes"]), row["price_kes"])
    if phone is None:
        await _reply(ctx, salon["id"], chat.to, st, chat, t(lang, "ask_number", amount=amount))
        return
    await _save(ctx, salon["id"], chat.to, {**st, "phone": phone})
    await _reply(ctx, salon["id"], chat.to, st, chat, await _prompt(ctx, salon, st["booking_id"], phone, amount, lang))
