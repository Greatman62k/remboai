"""Greetings and 'how are you', answered by plain rules so they work even when no model is reachable."""
import re

_HELLO = re.compile(r"^\W*(?P<w>hi+|hello+|hallo|hey+|howdy|yo|niaje|mambo|habari|sasa|hujambo|jambo|vipi|shikamoo|salaam|hii|"
                    r"good\s+(?:morning|afternoon|evening|day))\b", re.I)
_HOW = re.compile(r"\b(how\s*(?:are|r)\s*(?:you|u)|how'?s\s+it\s+going|what'?s\s+up|habari\s+yako|hali\s+yako|u\s*poa|uko\s+(?:aje|vipi|poa)|u\s*sawa|mambo\s+vipi)\b", re.I)
_SW = {"niaje", "mambo", "habari", "sasa", "hujambo", "jambo", "vipi", "shikamoo", "salaam", "hii"}


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
