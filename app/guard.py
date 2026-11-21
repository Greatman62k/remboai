from __future__ import annotations

import re
import unicodedata

from . import lexicon
from .channels import outbound
from .contact import display_contact

MAX_TEXT = 1000
MAX_NAME = 40
MAX_HISTORY = 6
ACK_EVERY_S = 3600

_DROP = {"Cc", "Cf", "Cs", "Co", "Cn"}
_SPACE = re.compile(r"[^\S\n]+")

_INJECTION = re.compile(
    r"(ignore|disregard|forget|override|bypass)\W+(all\W+|any\W+|the\W+|your\W+|previous\W+|prior\W+|above\W+|earlier\W+|these\W+|those\W+)*"
    r"(instructions?|rules?|prompts?|guidelines?|directions?|context)|"
    r"(system|developer|hidden|initial|original)\W+(prompt|message|instructions?)|"
    r"(reveal|show|print|repeat|leak|tell me)\W+(me\W+)?(?:(your\W+|the\W+)*(prompt|secrets?|api\W?keys?|tokens?|passwords?|configuration|config)|your\W+(instructions?|rules))|"
    r"you\W+are\W+(now\W+(a|an|the|my|dan|in|free|unrestricted)\b|no\W+longer\W)|\bact\W+as\W+(?!(?:soon|early|late|quickly|fast|well)\b)(a|an|the)?\W*\w+|pretend\W+(to\W+be|you\W+are)|jailbreak|"
    r"\bdo\W+anything\W+now\b|\bDAN\W+(mode|prompt|jailbreak|version)\b|\byou\W+are\W+(?-i:DAN)\b|developer\W+mode|"
    r"<\|?(system|im_start|im_end|assistant)\|?>|\[/?(INST|SYS)\]|^\W*(system|assistant|developer)\s*:", re.I | re.M)

_REPLIES = {
    "en": {
        "default": "I've saved your message for the business to review. I'll pause booking help here for now.",
        "payment_dispute": "I'm sorry about the payment issue. I've saved your concern for the business to review. Keep your receipt, and please don't pay again until the payment is checked.",
        "deposit": "The business sets its deposit policy. I've saved your question for review; the booking assistant will pause here for now.",
        "complaint": "Thanks for telling us. I've saved your feedback for the business to review. I'll pause booking help here for now.",
        "medical": "I'm sorry to hear that. Please seek medical advice promptly if needed. I've saved your message for the business to review.",
        "emergency": "I'm sorry you're feeling this way. If you may be in immediate danger, call Kenya emergency services on 999 or 112 now, and stay with someone you trust. I've saved your message for the business as well.",
        "legal": "I've saved your message for the business to review. The booking assistant will pause here for now.",
        "harassment": "I'll keep this conversation focused on bookings. I've saved your message for the business to review.",
        "privacy": "I can't share other customers' or staff members' details. I've saved your request for the business to review.",
        "human_request": "I've saved your request for the business owner to review. I'll pause booking help here for now.",
        "waiting": "Your message is saved for the business to review. Booking help is paused here for now.",
    },
    "sw": {
        "default": "Nimehifadhi ujumbe wako ili biashara iweze kuupitia. Nitasitisha msaada wa kuweka miadi kwa sasa.",
        "payment_dispute": "Pole kwa tatizo la malipo. Nimehifadhi suala lako ili biashara ilikague. Weka risiti yako, na usilipe tena kabla malipo hayajakaguliwa.",
        "deposit": "Biashara ndiyo huamua kuhusu amana. Nimehifadhi swali lako ili likaguliwe; msaidizi atasitisha hapa kwa sasa.",
        "complaint": "Asante kwa kutueleza. Nimehifadhi maoni yako ili biashara iyapitie. Nitasitisha msaada wa kuweka miadi kwa sasa.",
        "medical": "Pole kwa hilo. Tafuta ushauri wa afya haraka ikihitajika. Nimehifadhi ujumbe wako ili biashara iweze kuupitia.",
        "emergency": "Pole kwa unavyohisi. Ikiwa uko kwenye hatari ya haraka, piga huduma za dharura Kenya kwa 999 au 112 sasa, na ukae na mtu unayemwamini. Nimehifadhi ujumbe wako kwa biashara pia.",
        "legal": "Nimehifadhi ujumbe wako ili biashara iweze kuupitia. Msaidizi wa miadi atasitisha hapa kwa sasa.",
        "harassment": "Mazungumzo haya yataendelea kuhusu miadi pekee. Nimehifadhi ujumbe wako ili biashara iweze kuupitia.",
        "privacy": "Siwezi kushiriki maelezo ya wateja au wafanyakazi wengine. Nimehifadhi ombi lako ili biashara ilipitie.",
        "human_request": "Nimehifadhi ombi lako ili mmiliki wa biashara alipitie. Nitasitisha msaada wa kuweka miadi kwa sasa.",
        "waiting": "Ujumbe wako umehifadhiwa ili biashara iweze kuupitia. Msaada wa kuweka miadi umesitishwa kwa sasa.",
    },
}

_SENSITIVE = [
    ("emergency", "en", r"\b(emergency|ambulance|suicide|suicidal|kill myself|want to die|hurt myself|self[- ]harm)\b"),
    ("emergency", "sw", r"\b(dharura|nataka kufa|nataka kujiumiza|najiua|nitajiua|kujiua|nitajiumiza|kujiumiza)\b"),
    ("legal", "en", r"\b(lawyer|lawsuit|sue you|consumer protection)\b|\breport (you|the (?:salon|shop|business))\b|\bpolice\b(?!\W+(?:station|post|barracks|lines?)\b)|"
                    r"\b(?:report(?:ing)?|call(?:ing)?|tak(?:e|ing))\b(?:\W+\w+){0,3}?\W+(?:to\W+)?(?:the\W+)?police\b"),
    ("legal", "sw", r"\b(wakili|mahakama|nitakushtaki)\b|(?<!kituo cha )\bpolisi\b"),
    ("medical", "en", r"\b(allergic|infection|bleeding|blister|chemical burn|fainted|can'?t breathe)\b|\brash(?![- ]free)\b|"
                      r"\bhair\W+(?:\w+\W+){0,2}?(?:falling|coming)\W+out\W+after\b|\b(?:losing|lost)\W+(?:my\W+)?hair\W+after\b"),
    ("medical", "sw", r"\b(mzio|nimeungua|nimevimba|uvimbe)\b"),
    ("payment_dispute", "en", r"\b(refunds?(?!\s+polic)|reversal|chargeback|charged twice|deducted|overcharged|scam|scammed)\b|paid but|money (was )?taken"),
    ("payment_dispute", "sw", r"\b(nirudishie|rudisha pesa|nimekatwa|imekatwa|tapeli|wezi)\b|nimelipa lakini"),
    ("deposit", "en", r"\b(skip|waive|without)\b.{0,20}\bdeposit\b|\bdeposit\b.{0,15}\b(waive)\b"),
    ("harassment", "en", r"\b(fuck|bitch|idiot|stupid|nude|date me|harass|touched me|inappropriate)\b"),
    ("harassment", "sw", r"\b(mjinga|malaya|mpumbavu)\b"),
    ("complaint", "en", r"\b(complain|unhappy|terrible|horrible|bad service|rude|worst service)\b|\b(you|your (stylist|team))\b.{0,20}\b(ruined|damaged)\b"),
    ("complaint", "sw", r"\b(lalamiko|nimekasirika|umeharibu|huduma mbaya)\b"),
    ("privacy", "en", r"(give|send).{0,30}(number|contact).{0,40}\b(stylist|staff|client)\b|\bother clients.*phone\b"),
    ("human_request", "en", r"\b(talk|speak|chat).{0,25}\b(human|person|agent|owner|manager)\b|\breal person\b"),
    ("human_request", "sw", r"\b(kuongea|ongea)\b.{0,25}\b(mtu|mmiliki|meneja)\b"),
]

_SENSITIVE_COMPILED = [(c, lang, re.compile(rx, re.I)) for c, lang, rx in _SENSITIVE]
_LEXICON_INJECTION = lexicon.load().pattern("injection")

def clean(text: str | None) -> str:
    if not text:
        return ""
    text = unicodedata.normalize("NFKC", text)
    text = "".join(ch for ch in text if ch in "\n\t" or unicodedata.category(ch) not in _DROP)
    text = _SPACE.sub(" ", text)
    text = re.sub(r"\n{3,}", "\n\n", text).strip()
    return text[:MAX_TEXT]

def clean_name(name: str | None) -> str | None:
    n = " ".join(clean(name).split())[:MAX_NAME].strip()
    return n or None

def classify(text: str) -> tuple[str, str] | None:
    if not text:
        return None
    for cat, lang, rx in _SENSITIVE_COMPILED:
        if rx.search(text):
            return cat, lang
    return None

def reply_for(category: str, lang: str) -> str:
    table = _REPLIES.get(lang, _REPLIES["en"])
    return table.get(category, table["default"])

def injection(text: str) -> bool:
    return bool(_INJECTION.search(text or "") or _LEXICON_INJECTION.search(text or ""))

async def hand_off(ctx, salon: dict, client: dict, lang: str, category: str, text: str) -> None:
    import logging
    from . import handoffs
    log = logging.getLogger("rembo.guard")
    owner = (salon.get("owner_wa_id") or "").strip().lstrip("+")
    owner_body = None
    if owner:
        wa_id = client.get("wa_id") or ""
        who = client.get("name") or display_contact(wa_id)
        if wa_id.startswith("tg:"):
            contact_line = f"Telegram contact: {display_contact(wa_id)}"
        else:
            digits = re.sub(r"\D", "", wa_id)
            contact_line = f"Chat: https://wa.me/{digits}" if digits else f"Contact: {display_contact(wa_id)}"
        owner_body = (f"Needs a human ({category.replace('_',' ')}): {who}\n{contact_line}\n"
                      f"They said: \"{(text or '')[:200]}\"\nReply \"resolve {wa_id}\" when done.")

    # A failure aborts this turn before either party is acknowledged. The handoff
    # row, initial customer event, notification intent and paused state commit together.
    result = await handoffs.create_handoff(
        ctx.pool, salon["id"], client["id"], client["wa_id"], category, text,
        source_message_id=outbound.current_message_id(), owner_notification=owner_body)
    if owner and owner_body:
        # Keep the owner alert out of the diagnostic row representing the customer's turn.
        source = result.get("source_message_id") or "unknown"
        async with outbound.message_scope(f"handoff-notice:{result['id']}:{source}"):
            try:
                await outbound.send_text(ctx, salon, owner, owner_body)
            except Exception:
                log.exception("owner notify failed for handoff %s", result["id"])

async def is_handoff(pool, salon_id: str, wa_id: str) -> bool:
    row = await pool.fetchval(
        "SELECT 1 FROM handoffs WHERE salon_id=$1::uuid AND client_id=(SELECT id FROM clients WHERE salon_id=$1::uuid AND wa_id=$2) AND status='open' LIMIT 1",
        salon_id, wa_id,
    )
    return bool(row)

async def owner_command(ctx, salon: dict, wa_id: str, text: str, send) -> bool:
    owner = (salon.get("owner_wa_id") or "").strip().lstrip("+")
    if not owner or wa_id != owner:
        return False

    command = (text or "").strip()
    if re.fullmatch(r"resolve", command, re.I):
        rows = await ctx.pool.fetch(
            "SELECT c.wa_id, c.name, h.category FROM handoffs h JOIN clients c ON c.id=h.client_id "
            "WHERE h.salon_id=$1::uuid AND h.status='open' ORDER BY h.created_at", salon["id"])
        if not rows:
            await send.text("There are no open requests to resolve.")
            return True
        if len(rows) > 1:
            lines = "\n".join(f"- {r['name'] or display_contact(r['wa_id'])}: {r['category']} (resolve {r['wa_id']})" for r in rows)
            await send.text("There are several open requests. Reply with the customer to resolve:\n" + lines)
            return True
        target = rows[0]["wa_id"]
    else:
        m = re.fullmatch(r"resolve\s+((?:tg:\d{5,15})|(?:\+?\d{9,15}))", command, re.I)
        if not m:
            if re.match(r"resolve\b", command, re.I):
                await send.text("Use RESOLVE for the only open request, or RESOLVE <customer ID> when there are several.")
                return True
            return False
        target = m.group(1)
        target = target.lower() if target.lower().startswith("tg:") else target.lstrip("+")

    from . import handoffs
    target_case = await ctx.pool.fetchrow(
        "SELECT h.id::text FROM handoffs h JOIN clients c ON c.id=h.client_id AND c.salon_id=h.salon_id "
        "WHERE h.salon_id=$1::uuid AND h.status='open' AND c.wa_id=$2 ORDER BY h.created_at LIMIT 1", salon["id"], target)
    if not target_case:
        await send.text(f"There is no open request for {display_contact(target)}.")
        return True
    try:
        resolved = await handoffs.resolve_handoff(ctx.pool, salon["id"], target_case["id"], wa_id, "owner_resolved_via_telegram")
    except handoffs.CaseHasSendingReply:
        await send.text("A reply is currently being submitted to that customer. Try RESOLVE again after it finishes.")
        return True
    if not resolved or resolved.get("already_resolved"):
        await send.text(f"There is no open request for {display_contact(target)}.")
        return True
    await send.text(f"Resolved for {display_contact(target)}. The bot will handle them again.")
    return True

async def strike(pool, sender: str, reason: str) -> None:
    """One strike per message and reason: handling a message again (a retry) must not count it twice."""
    await pool.execute("INSERT INTO abuse_strikes (sender, reason, message_id) VALUES ($1, $2, $3) ON CONFLICT DO NOTHING", sender, reason[:60], outbound.current_message_id())

async def blocked(pool, sender: str, limit: int) -> bool:
    if limit <= 0:
        return False
    n = await pool.fetchval("SELECT count(*) FROM abuse_strikes WHERE sender=$1 AND at > now() - interval '24 hours'", sender)
    return n >= limit
