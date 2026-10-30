"""The vocabulary the receptionist understands without a model, kept as data (app/lexicon.json) instead of code: English, Swahili and Sheng
word lists that the screen, the keyword rules, date parsing, greetings and the injection guard all read. Adding words never means editing Python.
The loader is defensive: an entry that is malformed, too short to be meaningful or a common function word is dropped and reported, never fatal."""
from __future__ import annotations

import json
import logging
import os
import re
import unicodedata
from dataclasses import dataclass, field
from functools import lru_cache
from pathlib import Path

log = logging.getLogger("rembo.lexicon")
PATH = Path(os.environ.get("LEXICON_PATH") or Path(__file__).with_name("lexicon.json"))
LANGS = ("en", "sw", "sheng")
STRONG = ("services_styles", "booking_words")
WEAK = ("request_words", "price_payment_words", "hours_cancel_words")
SPECIFIC = ("price_payment_words", "hours_cancel_words")
TOPIC = STRONG + WEAK
_ENTRY = re.compile(r"^[^\W_][\w '.\-]{0,38}[*+]?$")
STOP = {"a", "an", "the", "and", "or", "is", "are", "am", "i", "me", "my", "you", "your", "it", "to", "of", "in", "on", "at", "for", "with", "yes", "no", "ok", "okay",
        "na", "ya", "wa", "kwa", "ni", "la", "za", "kwa", "hii", "hiyo", "huu", "huo", "hapa", "pale", "sana", "tu", "pia", "ama", "au", "kama", "ndio", "hapana", "sawa", "mimi", "wewe",
        "we", "yeye", "sisi", "ana", "ina", "iko", "yako", "yangu", "wako", "wangu", "kuna", "bado", "ila", "lakini", "pamoja", "sasa", "bro", "boss", "mze", "msee", "bruh", "man"}


_STOP_STRONG = {"top", "sides", "side", "lines", "line", "linez", "dem", "gal", "toto", "masa", "mathee", "mokoro", "mamii", "mamaa", "msupa", "empress", "mpoa", "ngeus", "baba",
                "dawa", "rangi", "kutengeneza", "fundi", "weka", "panga", "hifadhi", "mbao", "kibao", "blow", "stim", "trit", "kutembelea", "nikam", "nikuje", "nakuja", "nitafika",
                "sai", "kach", "chapaa", "ganji", "moni", "bokette", "mukuchu", "kichele", "maziwa", "cash", "rate", "fee", "charge", "change", "open", "push", "pisha", "futa", "muda", "masaa"}
_STOP_WEAK = {"muda", "masaa", "push", "pisha", "futa", "change", "cash", "rate", "fee", "charge", "sai", "kach", "chapaa", "ganji", "moni", "bokette", "mukuchu",
              "kichele", "maziwa"}
_STOP_DATE = {"am", "a m", "pm", "p m", "eve"}
_STOP_BY_CATEGORY = {c: _STOP_STRONG for c in STRONG} | {c: _STOP_WEAK for c in ("price_payment_words", "hours_cancel_words")} | {c: _STOP_DATE for c in ("today", "tomorrow", "day_after_tomorrow", "morning", "afternoon", "evening",
                      "monday", "tuesday", "wednesday", "thursday", "friday", "saturday", "sunday")} | {
    "sw_markers": {"fresh", "freshi", "jo", "form", "fom", "bana"},
    "greetings": {"morning", "afternoon", "evening", "wazi", "sema"}}


@dataclass
class Lexicon:
    data: dict
    problems: list[str] = field(default_factory=list)
    removed: list[str] = field(default_factory=list)

    def entries(self, category: str, langs=LANGS) -> list[str]:
        block = self.data.get(category) or {}
        return [e for lang in langs for e in block.get(lang, [])]

    def words(self, category: str, langs=LANGS) -> set[str]:
        """Entries without the * and + markers, for exact-word checks."""
        return {e.rstrip("*+") for e in self.entries(category, langs)}

    def alternation(self, *categories: str, langs=LANGS) -> str:
        """The entries as the inside of a regex group: longest first, so 'kesho kutwa' wins over 'kesho'. Empty string if there are none."""
        return "|".join(sorted({_alt(e) for c in categories for e in self.entries(c, langs)}, key=len, reverse=True))

    def pattern(self, *categories: str, langs=LANGS, anchored: bool = False) -> re.Pattern:
        """One case-insensitive regex that matches any entry of the categories as a whole word (or a word start with *).
        With no entries it matches nothing."""
        alts = self.alternation(*categories, langs=langs)
        if not alts:
            return re.compile(r"(?!)")
        return re.compile((r"^\W*(?:" if anchored else r"\b(?:") + alts + r")\b", re.I)

    def examples(self, kind: str) -> list[str]:
        return list((self.data.get("examples") or {}).get(kind, []))


def _alt(entry: str) -> str:
    if entry.endswith("*"):
        return _phrase(entry[:-1]) + r"\w*"
    if entry.endswith("+"):
        return _phrase(entry[:-1]) + "+"
    return _phrase(entry)


def _phrase(text: str) -> str:
    return re.escape(text).replace(r"\ ", r"\s+").replace("'", "'?")


def _clean_entry(raw, category: str, problems: list[str], removed: list[str]) -> str | None:
    if not isinstance(raw, str):
        problems.append(f"{category}: not text: {raw!r}")
        return None
    e = " ".join(unicodedata.normalize("NFKC", raw).lower().split())
    if not _ENTRY.match(e) or len(e.rstrip("*+")) < 2:
        problems.append(f"{category}: dropped malformed entry {raw!r}")
        return None
    if category in TOPIC and e.rstrip("*+") in STOP:
        removed.append(f"{category}: {raw!r} (too common: it would make every message look like salon talk)")
        return None
    if e.rstrip("*+") in _STOP_BY_CATEGORY.get(category, ()):
        removed.append(f"{category}: {raw!r} (too generic for this list)")
        return None
    return e


def build(raw: dict) -> Lexicon:
    problems: list[str] = []
    removed: list[str] = []
    data: dict = {}
    for category, block in raw.items():
        if category.startswith("_"):
            continue
        if category == "examples":
            data[category] = {k: [x.strip() for x in v if isinstance(x, str) and x.strip()] for k, v in (block or {}).items() if isinstance(v, list)}
            continue
        if not isinstance(block, dict):
            problems.append(f"{category}: expected en/sw/sheng lists")
            continue
        out, seen = {}, set()
        for lang in LANGS:
            keep = []
            for raw_entry in block.get(lang, []) or []:
                e = _clean_entry(raw_entry, category, problems, removed)
                if e and e not in seen:
                    seen.add(e)
                    keep.append(e)
            out[lang] = keep
        for lang in block:
            if lang not in LANGS:
                problems.append(f"{category}: unknown list {lang!r} (use en, sw, sheng)")
        data[category] = out
    return Lexicon(data, problems, removed)


@lru_cache(maxsize=1)
def load() -> Lexicon:
    try:
        lex = build(json.loads(PATH.read_text(encoding="utf-8")))
    except (OSError, ValueError) as e:
        log.error("could not read %s (%s): running with an empty vocabulary", PATH.name, e)
        return Lexicon({})
    for p in lex.problems:
        log.warning("lexicon: %s", p)
    if lex.removed:
        log.info("lexicon: %d too-generic entries ignored (run scripts/check_lexicon.py --write to clean the file)", len(lex.removed))
    return lex


def cleaned(raw: dict) -> dict:
    """The file's own structure with every refused entry removed and the explanatory `_` notes kept, ready to write back."""
    lx = build(raw)
    out = {k: v for k, v in raw.items() if k.startswith("_")}
    for category in raw:
        if category in lx.data:
            out[category] = lx.data[category]
    return dict(sorted(out.items(), key=lambda kv: list(raw).index(kv[0])))

