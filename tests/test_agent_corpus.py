"""The eval corpora are data, and wrong gold labels make an eval meaningless, so they are checked mechanically."""
import json
import os
import re

import pytest

HERE = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
V1 = json.load(open(os.path.join(HERE, "scripts", "agent_corpus.json")))
V2 = json.load(open(os.path.join(HERE, "scripts", "agent_corpus_v2.json")))
TOOLS = {"find_slots", "reserve_slot", "my_bookings", "cancel_booking", "join_waitlist", "hand_to_owner"}
ARG_KEYS = {"service", "day_offset", "window", "time", "stylist", "slot_id", "reason"}
SERVICES = {"Fade", "Beard trim", "Braids", "Kids cut", "Silk press"}
REASONS = {"complaint", "payment_dispute", "medical", "human_request", "other"}


def _tools(item):
    t = item["expect"]["tool"]
    return t if isinstance(t, list) else [t]


@pytest.mark.parametrize("corpus", [V1, V2], ids=["v1", "v2"])
def test_every_item_is_well_formed(corpus):
    for it in corpus:
        assert isinstance(it["text"], str) and it["text"].strip(), it
        assert isinstance(it["offered"], bool), it
        e = it["expect"]
        assert set(_tools(it)) <= TOOLS | {None}, it
        args = e.get("args", {})
        assert set(args) <= ARG_KEYS, it
        if "day_offset" in args:
            assert isinstance(args["day_offset"], int) and 0 <= args["day_offset"] <= 14, it
        if "window" in args:
            assert args["window"] in {"morning", "afternoon", "evening"}, it
        if "time" in args:
            assert re.fullmatch(r"([01]\d|2[0-3]):[0-5]\d", args["time"]), it
        if "service" in args:
            assert args["service"] in SERVICES, it
        if "reason" in args:
            assert args["reason"] in REASONS, it
        if "slot_id" in args:
            assert args["slot_id"] in {"s1", "s2", "s3"} and it["offered"], it
        if "reserve_slot" in _tools(it):
            assert it["offered"], f"a pick only makes sense after slots were offered: {it['text']}"
        if "reply_has_any" in e or "reply_lacks" in e:
            assert None in _tools(it), f"reply checks only run when the model answers in words: {it['text']}"
        for key in ("reply_has_any", "reply_lacks"):
            assert all(isinstance(w, str) and w for w in e.get(key, [])), it


def test_v2_is_large_and_broad():
    assert len(V2) >= 100
    assert len({it["cat"] for it in V2}) >= 10
    assert len({it["id"] for it in V2}) == len(V2)
    assert len({it["text"].lower() for it in V2}) == len(V2), "duplicate messages"


def test_v2_shares_no_message_with_v1():
    v1 = {it["text"].strip().lower() for it in V1}
    assert not [it["text"] for it in V2 if it["text"].strip().lower() in v1]


def test_v2_covers_the_failure_modes_that_matter():
    cats = {it["cat"] for it in V2}
    assert {"safety", "context", "handoff", "missing", "voice", "dates", "pricing", "compound"} <= cats
    tools_expected = {t for it in V2 for t in _tools(it)}
    assert TOOLS <= tools_expected | {"find_slots"}, "every tool should be the expected first move somewhere"
    assert sum(1 for it in V2 if it["offered"]) >= 10
    assert all(it.get("history") for it in V2 if it["offered"]), "offered items should carry the conversation that offered the slots"
    assert sum(1 for it in V2 if _tools(it) == [None]) >= 20, "answering in words must be tested as often as acting"


# -- gold labels that can be checked by arithmetic ------------------------------------------------------------------------------------
_DAYS = {"tuesday": 1, "tues": 1, "wednesday": 2, "thursday": 3, "thurs": 3, "friday": 4, "saturday": 5, "sat": 5,
         "jumanne": 1, "jumatano": 2, "alhamisi": 3, "ijumaa": 4, "jumamosi": 5}
_SKIP = re.compile(r"kesho|tomorrow|tmrw|leo|today|tarehe|november|\b\d{1,2}(st|nd|rd|th)\b|day after|kutwa|the \d")
_NEXT = re.compile(r"next week|wiki ijayo|ijayo")


def test_weekday_words_agree_with_day_offset():
    """Today is Monday 2 Nov 2026 in the eval fixture, so Tuesday is +1 ... Saturday +5, and 'next week' adds 7."""
    checked = 0
    for it in V2:
        want = it["expect"].get("args", {}).get("day_offset")
        low = it["text"].lower()
        if want is None or _SKIP.search(low):
            continue
        found = [d for w, d in _DAYS.items() if re.search(rf"\b{w}\b", low)]
        if len(found) != 1:
            continue
        expected = found[0] + (7 if _NEXT.search(low) else 0)
        assert want == expected, f"{it['id']} {it['text']!r}: day_offset {want}, weekday says {expected}"
        checked += 1
    assert checked >= 10


_SW = {"kumi na mbili": "18:00", "kumi na moja": "17:00", "moja": "07:00", "mbili": "08:00", "tatu": "09:00", "nne": "10:00", "tano": "11:00",
       "sita": "12:00", "saba": "13:00", "nane": "14:00", "tisa": "15:00", "kumi": "16:00"}


def test_swahili_clock_times_match_the_policy_guide():
    policy = json.load(open(os.path.join(HERE, "config", "remboai_agent_policy.json")))
    guide = policy["legacy_booking_extractor"]["swahili_time_guide"]
    for k, v in _SW.items():
        assert guide[f"saa {k}"] == v, k
    checked = 0
    for it in V2:
        want = it["expect"].get("args", {}).get("time")
        m = re.search(r"saa (kumi na mbili|kumi na moja|moja|mbili|tatu|nne|tano|sita|saba|nane|tisa|kumi)\b", it["text"].lower())
        if want and m:
            assert _SW[m.group(1)] == want, f"{it['id']} {it['text']!r}: {want} vs saa {m.group(1)}"
            checked += 1
    assert checked >= 6


def test_english_clock_times_match():
    checked = 0
    for it in V2:
        want = it["expect"].get("args", {}).get("time")
        m = re.search(r"\b(\d{1,2})(?::(\d{2}))?\s?(am|pm)\b", it["text"].lower())
        if want and m:
            h, mi, ap = int(m.group(1)), m.group(2) or "00", m.group(3)
            h = h % 12 + (12 if ap == "pm" else 0)
            assert want == f"{h:02d}:{mi}", f"{it['id']} {it['text']!r}: {want}"
            checked += 1
    assert checked >= 6


def test_offered_slots_match_the_fixture():
    """Context items pick among three slots: s1 Tue 10:00 Brian, s2 Tue 14:00 Brian, s3 Tue 15:30 Amina."""
    pick = {"I'll take 10am": "s1", "the one with Amina": "s3", "3:30 works for me": "s3", "second one": "s2", "yes the first": "s1",
            "Brian at 2": "s2", "nataka ya saa nane": "s2", "the afternoon one with Brian": "s2", "ya saa nne": "s1"}
    by_text = {it["text"]: it for it in V2}
    for text, slot in pick.items():
        assert by_text[text]["expect"]["args"]["slot_id"] == slot, text
