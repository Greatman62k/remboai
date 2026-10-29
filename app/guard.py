"""Input guardrails that run in code, before any model sees a message.
The model never writes a reply (every reply is a catalogue message filled by code) and its output is validated, so the worst an
attacker gets from the model is a wrong form. These guards also keep hostile text out of the model and out of the owner's chat."""
from __future__ import annotations

import re
import unicodedata

from . import lexicon

MAX_TEXT = 1000
MAX_NAME = 40

# Bidi controls and zero-width characters are used to hide text or to make a name look like something else.
_DROP = {"Cc", "Cf", "Cs", "Co", "Cn"}
_SPACE = re.compile(r"[^\S\n]+")

# Phrases that only appear when someone is trying to steer the model instead of booking a haircut.
_INJECTION = re.compile(
    r"(ignore|disregard|forget|override|bypass)\W+(all\W+|any\W+|the\W+|your\W+|previous\W+|prior\W+|above\W+|earlier\W+|these\W+|those\W+)*"
    r"(instructions?|rules?|prompts?|guidelines?|directions?|context)|"
    r"(system|developer|hidden|initial|original)\W+(prompt|message|instructions?)|"
    r"(reveal|show|print|repeat|leak|tell me)\W+(me\W+)?(your\W+|the\W+)*(prompt|instructions?|rules|secrets?|api\W?keys?|tokens?|passwords?|configuration|config)|"
    r"you\W+are\W+(now\W+(a|an|the|my|dan|in|free|unrestricted)\b|no\W+longer\W)|\bact\W+as\W+(a|an|the)?\W*\w+|pretend\W+(to\W+be|you\W+are)|jailbreak|\bDAN\b|developer\W+mode|"
    r"<\|?(system|im_start|im_end|assistant)\|?>|\[/?(INST|SYS)\]|^\W*(system|assistant|developer)\s*:", re.I | re.M)


def clean(text: str | None) -> str:
    """Normalise (so look-alike letters cannot dodge the filters), drop invisible and control characters, tidy spaces, cap the length."""
    if not text:
        return ""
    text = unicodedata.normalize("NFKC", text)
    text = "".join(ch for ch in text if ch in "\n\t" or unicodedata.category(ch) not in _DROP)
    text = _SPACE.sub(" ", text)
    text = re.sub(r"\n{3,}", "\n\n", text).strip()
    return text[:MAX_TEXT]


def clean_name(name: str | None) -> str | None:
    """A display name is attacker-controlled and ends up in the owner's chat: single line, short, nothing invisible."""
    n = " ".join(clean(name).split())[:MAX_NAME].strip()
    return n or None


_LEXICON_INJECTION = lexicon.load().pattern("injection")     # Swahili and Sheng phrases, from app/lexicon.json


def injection(text: str) -> bool:
    return bool(_INJECTION.search(text or "") or _LEXICON_INJECTION.search(text or ""))


async def strike(pool, sender: str, reason: str) -> None:
    await pool.execute("INSERT INTO abuse_strikes (sender, reason) VALUES ($1, $2)", sender, reason[:60])


async def blocked(pool, sender: str, limit: int) -> bool:
    """Enough strikes in the last day and the sender is ignored until they age out."""
    if limit <= 0:
        return False
    n = await pool.fetchval("SELECT count(*) FROM abuse_strikes WHERE sender=$1 AND at > now() - interval '24 hours'", sender)
    return n >= limit

