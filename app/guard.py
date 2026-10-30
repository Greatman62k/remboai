from __future__ import annotations

import re
import unicodedata

from . import lexicon
from .channels import outbound

MAX_TEXT = 1000
MAX_NAME = 40
MAX_HISTORY = 6
HANDOFF_HOURS = 4.0
ACK_EVERY_S = 3600

_DROP = {"Cc", "Cf", "Cs", "Co", "Cn"}
_SPACE = re.compile(r"[^\S\n]+")

_INJECTION = re.compile(
    r"(ignore|disregard|forget|override|bypass)\W+(all\W+|any\W+|the\W+|your\W+|previous\W+|prior\W+|above\W+|earlier\W+|these\W+|those\W+)*"
    r"(instructions?|rules?|prompts?|guidelines?|directions?|context)|"
    r"(system|developer|hidden|initial|original)\W+(prompt|message|instructions?)|"
    r"(reveal|show|print|repeat|leak|tell me)\W+(me\W+)?(your\W+|the\W+)*(prompt|instructions?|rules|secrets?|api\W?keys?|tokens?|passwords?|configuration|config)|"
    r"you\W+are\W+(now\W+(a|an|the|my|dan|in|free|unrestricted)\b|no\W+longer\W)|\bact\W+as\W+(a|an|the)?\W*\w+|pretend\W+(to\W+be|you\W+are)|jailbreak|"
    r"\bdo\W+anything\W+now\b|\bDAN\W+(mode|prompt|jailbreak|version)\b|\byou\W+are\W+(?-i:DAN)\b|developer\W+mode|"
    r"<\|?(system|im_start|im_end|assistant)\|?>|\[/?(INST|SYS)\]|^\W*(system|assistant|developer)\s*:", re.I | re.M)

_REPLIES = {
    "en": {
        "default": "Thank you for telling us. I've passed your message to the salon owner who will reply shortly. Your booking assistant will pause until they respond. We appreciate your patience.",
        "payment_dispute": "I'm sorry about the payment concern. For your security, refunds and payment disputes are handled directly by the salon owner. I've forwarded your request and they'll contact you shortly. Please keep your M-Pesa message handy.",
        "deposit": "Deposits are set by the salon to secure your slot. I've forwarded your request to the owner who can advise you personally.",
        "complaint": "Thank you for your patience and for letting us know. I've shared your feedback with the salon owner who will follow up with you directly and help resolve this.",
        "medical": "I'm sorry to hear about that. For health matters, please seek medical advice promptly if needed. I've alerted the salon owner who will contact you shortly to assist.",
        "emergency": "If this is an emergency, please contact emergency services (999 or 112) immediately. I've also alerted the salon owner.",
        "legal": "Thank you for sharing this. I've passed your message to the salon owner who will respond directly.",
        "harassment": "We keep conversations respectful and focused on bookings. I've passed your message to the salon owner for review.",
        "privacy": "For privacy, I can't share other clients' or staff details. I've forwarded your request to the owner to assist you directly.",
        "human_request": "Of course, I have notified the salon owner and they'll reply to you shortly. Thank you for your patience.",
        "waiting": "The salon owner has your message and will respond as soon as possible. Thank you for your patience. We will pause here until they're back.",
    },
    "sw": {
        "default": "Asante kwa kutujulisha. Nimepeleka ujumbe wako kwa mmiliki wa saluni ambaye atajibu hivi karibuni. Msaidizi atapumzika hadi ajibu. Tunashukuru kwa uvumilivu wako.",
        "payment_dispute": "Pole kwa usumbufu wa malipo. Kwa usalama wako, marejesho hushughulikiwa na mmiliki wa saluni. Nimepeleka ombi lako na watakuwasiliana hivi karibuni. Tafadhali weka ujumbe wako wa M-Pesa tayari.",
        "deposit": "Amana huwekwa na saluni kuhakikisha nafasi yako. Nimepeleka ombi lako kwa mmiliki atakayekushauri.",
        "complaint": "Asante kwa kutujulisha na kwa uvumilivu wako. Nimepeleka maoni yako kwa mmiliki wa saluni ambaye atafuatilia moja kwa moja.",
        "medical": "Pole sana. Kwa masuala ya afya, tafadhali tafuta ushauri wa kitaalam haraka ikiwa ni lazima. Nimemjulisha mmiliki wa saluni.",
        "emergency": "Kama ni dharura, tafadhali piga 999 au 112 mara moja. Nimemjulisha mmiliki wa saluni pia.",
        "legal": "Asante kwa kutujulisha. Nimepeleka ujumbe wako kwa mmiliki wa saluni ambaye atajibu moja kwa moja.",
        "harassment": "Tunaheshimu mazungumzo ya heshima na tunajikita kwenye miadi. Nimepeleka ujumbe wako kwa mmiliki.",
        "privacy": "Kwa usiri, siwezi kushiriki maelezo ya wateja wengine. Nimepeleka ombi lako kwa mmiliki.",
        "human_request": "Sawa, nimemjulisha mmiliki wa saluni na atajibu hivi karibuni. Asante kwa uvumilivu wako.",
        "waiting": "Mmiliki wa saluni amepokea ujumbe wako na atajibu haraka iwezekanavyo. Asante kwa uvumilivu. Tutapumzika hapa hadi arudi.",
    },
}

_SENSITIVE = [
    ("emergency", "en", r"\b(emergency|ambulance|suicide|suicidal|kill myself|want to die|hurt myself|self[- ]harm)\b"),
    ("emergency", "sw", r"\b(dharura|nataka kufa|najiua|kujiua)\b"),
    ("legal", "en", r"\b(lawyer|lawsuit|sue you|consumer protection)\b|\breport (you|the salon)\b|\bpolice\b(?!\W+(?:station|post|barracks|lines?)\b)|"
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
    log = logging.getLogger("rembo.guard")
    try:
        await ctx.pool.execute(
            "INSERT INTO handoffs (salon_id, client_id, category, message) VALUES ($1::uuid,$2::uuid,$3,$4) ON CONFLICT (client_id) WHERE status = 'open' DO NOTHING",
            salon["id"], client["id"], category, text[:1000],
        )
    except Exception:
        log.exception("handoff insert failed")
    owner = (salon.get("owner_wa_id") or getattr(ctx.settings, "owner_alert_wa_id", "") or "").strip().lstrip("+")
    if owner:
        try:
            wa_id = client.get("wa_id") or ""
            who = client.get("name") or f"+{wa_id}"
            body = f"Needs a human ({category.replace('_',' ')}): {who}\nChat: https://wa.me/{wa_id}\nThey said: \"{(text or '')[:200]}\"\nReply \"resolve {wa_id}\" when done."
            await outbound.send_text(ctx, salon, owner, body)
        except Exception:
            log.exception("owner notify failed")
    try:
        await ctx.pool.execute(
            "INSERT INTO conversations (salon_id, wa_id, state) VALUES ($1::uuid,$2,$3::jsonb) ON CONFLICT (salon_id, wa_id) DO UPDATE SET state=EXCLUDED.state, updated_at=now()",
            salon["id"], client["wa_id"], __import__("json").dumps({"state": "handoff", "category": category}),
        )
    except Exception:
        log.exception("handoff state save failed")

async def is_handoff(pool, salon_id: str, wa_id: str) -> bool:
    row = await pool.fetchval(
        "SELECT 1 FROM handoffs WHERE salon_id=$1::uuid AND client_id=(SELECT id FROM clients WHERE salon_id=$1::uuid AND wa_id=$2) AND status='open' LIMIT 1",
        salon_id, wa_id,
    )
    return bool(row)

async def clear_handoff(pool, salon_id: str, wa_id: str) -> None:
    await pool.execute(
        "UPDATE handoffs SET status='resolved', resolved_at=now() WHERE status='open' AND salon_id=$1::uuid AND client_id IN (SELECT id FROM clients WHERE salon_id=$1::uuid AND wa_id=$2)",
        salon_id, wa_id,
    )

async def owner_command(ctx, salon: dict, wa_id: str, text: str, send) -> bool:
    import re as _re
    owner = (salon.get("owner_wa_id") or getattr(ctx.settings, "owner_alert_wa_id", "") or "").strip().lstrip("+")
    m = _re.match(r"^\s*resolve\s+\+?(\d{9,15})\s*$", text or "", _re.I)
    if not owner or wa_id != owner or not m:
        return False
    target = m.group(1)
    await ctx.pool.execute(
        "UPDATE handoffs SET status='resolved', resolved_at=now() WHERE status='open' AND salon_id=$1::uuid AND client_id IN (SELECT id FROM clients WHERE salon_id=$1::uuid AND wa_id=$2)",
        salon["id"], target,
    )
    await ctx.pool.execute(
        "INSERT INTO conversations (salon_id, wa_id, state) VALUES ($1::uuid,$2,$3::jsonb) ON CONFLICT (salon_id, wa_id) DO UPDATE SET state=EXCLUDED.state, updated_at=now()",
        salon["id"], target, __import__("json").dumps({}),
    )
    await send.text(f"Done. The bot will handle {target} again.")
    return True

async def strike(pool, sender: str, reason: str) -> None:
    """One strike per message and reason: handling a message again (a retry) must not count it twice."""
    await pool.execute("INSERT INTO abuse_strikes (sender, reason, message_id) VALUES ($1, $2, $3) ON CONFLICT DO NOTHING", sender, reason[:60], outbound.current_message_id())

async def blocked(pool, sender: str, limit: int) -> bool:
    if limit <= 0:
        return False
    n = await pool.fetchval("SELECT count(*) FROM abuse_strikes WHERE sender=$1 AND at > now() - interval '24 hours'", sender)
    return n >= limit
