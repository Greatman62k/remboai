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
