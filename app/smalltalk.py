"""Greetings and 'how are you', answered by plain rules so they work even when no model is reachable."""
import re

from . import lexicon

_LX = lexicon.load()
_HELLO = re.compile(r"^\W*(?P<w>" + (_LX.alternation("greetings") or "(?!)") + r")\b", re.I)
_HOW = _LX.pattern("how_are_you")
_SW = _LX.words("greetings", ("sw", "sheng"))


def detect(text: str) -> tuple[str | None, bool]:
    m = _HELLO.match(text or "")
    return (m.group("w").lower() if m else None), bool(_HOW.search(text or ""))


def opener(text: str) -> str | None:
    """A short reply to the pleasantry in the message (None if there is none). Language follows the greeting itself."""
    word, how = detect(text)
    if not word and not how:
        return None
    sw = (word in _SW) if word else bool(re.search(r"\b(habari|hali|uko|u\s*poa|u\s*sawa)\b", text.lower()))
    if sw:
        head = {"habari": "Nzuri!", "hujambo": "Sijambo!", "jambo": "Jambo!", "shikamoo": "Marahaba!"}.get(word or "", "Poa!")
        return head + (" Niko poa, asante kwa kuuliza." if how else "")
    head = (word.capitalize() + "!") if word and word.startswith("good") else ("Hello!" if word and word.startswith("hello") else "Hi!")
    return head + (" I'm doing great, thanks for asking." if how else "")


_THANKS_ALTS = (r"thank\s*you", "thankyou", "thank", "thanks", "thx", r"asante(\s+sana)?", "shukran", "shukrani",
                "perfect", "safi", "great", "good", "nice", "super", "awesome", "poa", "sawa", "nzuri",
                r"karibu(ni)?", "ok", "okay")
_THANKS = re.compile(r"^\W*(?:" + "|".join(_THANKS_ALTS) + r")\W*$", re.I)
_THANKS_SW = {"asante", "shukran", "shukrani", "sawa", "poa", "safi", "nzuri", "karibu", "karibuni"}
THANKS_FIRST_WORDS = {"thank", "thankyou", "thanks", "thx", "asante", "shukran", "shukrani", "perfect",
                       "safi", "great", "good", "nice", "super", "awesome", "poa", "sawa", "nzuri",
                       "karibu", "karibuni", "ok", "okay"}
FILLER_EXTRA = {"hi", "hello", "hey", "habari", "hujambo", "jambo", "shikamoo",
                "ndiyo", "ndio", "hapana", "yes", "no"}


def thanks(text: str) -> tuple[str, str] | None:
    """A warm reply when the whole message is just thanks or approval, else None. Returns (key, lang);
    the caller renders the key so the wording stays in the message catalogue."""
    m = _THANKS.match(text or "")
    if not m:
        return None
    first = re.sub(r"\W+", "", m.group(0).split()[0]).lower()
    return ("thanks", "sw" if first in _THANKS_SW else "en")


_IDENTITY = re.compile(
    r"\b(who are you|what are you(?!\s+(charging|costing|price|prices|bei|gharama))|who is rembo\w*|what is rembo\w*"
    r"|introduce yourself|what is your name|wewe ni nani|nani wewe|jina lako(\s+nani)?|rembo\w*\s+ni\s+(nani|nini))\b", re.I)
_HELP = re.compile(r"^\W*(help|msaada|nisaidie|/help)\W*$", re.I)
_HELP_Q = re.compile(r"\b(what can you do|how do i use|how does this work|jinsi ya kutumia|how to use)\b", re.I)


def asks_identity(text: str) -> bool:
    """The sender wants to know who they are talking to, in English or Swahili."""
    return bool(_IDENTITY.search(text or ""))


def asks_help(text: str) -> bool:
    """The sender wants to know what this bot can do and how to use it."""
    t = text or ""
    return bool(_HELP.match(t) or _HELP_Q.search(t))


_SW_MARKERS = lexicon.load().pattern("sw_markers")
_EN_MARKERS = re.compile(r"\b(the|you|your|yours|who|what|when|where|how|start|register|please|morning|afternoon|evening|today|tomorrow|book|booking|price|prices|open|opening|hours|cancel|thanks|thank|perfect|bye|hello)\b", re.I)


def language(text: str, default: str = "en") -> str:
    """The language this message is written in. Swahili/Sheng markers win; plain English
    markers flip it back; anything neutral keeps the conversation's language so 'ok'
    never changes it on its own."""
    t = text or ""
    if _SW_MARKERS.search(t):
        return "sw"
    if _EN_MARKERS.search(t):
        return "en"
    return default
