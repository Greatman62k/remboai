import asyncio
import os
from datetime import date, datetime, time, timedelta

import pytest

from app import smalltalk
from app.availability import choose, day_slots
from app.llm import build_prompt, rule_extract, sanitize, Extraction
from app.timeparse import parse_day, parse_time, parse_window, swahili_hour
from tests.harness import NBO, build, client, open_day, payload, post

DSN = os.environ.get("TEST_DATABASE_URL")
needs_db = pytest.mark.skipif(not DSN, reason="needs TEST_DATABASE_URL")
A, B = "254711000001", "254711000002"


# ---- pure ---------------------------------------------------------------------------------------------------------

def test_swahili_clock_counts_from_dawn():
    assert [swahili_hour(n) for n in (1, 2, 6, 7, 8, 12)] == [7, 8, 12, 1, 2, 6]
    assert parse_time("saa mbili asubuhi") == time(8) and parse_time("saa nane mchana") == time(14)
    assert parse_time("saa kumi jioni") == time(16) and parse_time("saa kumi na moja jioni") == time(17)
    assert parse_time("2pm") == time(14) and parse_time("14:30") == time(14, 30) and parse_time("12am") == time(0)
    assert parse_time("iko na slot ya fade leo around 4pm ama?") == time(16) and parse_time("fade kesho") is None


def test_days_and_windows():
    mon = date(2026, 10, 26)
    assert parse_day("kesho", mon) == date(2026, 10, 27) and parse_day("leo", mon) == mon
    assert parse_day("Jumamosi", mon) == date(2026, 10, 31) and parse_day("jumatatu", mon) == date(2026, 11, 2)
    assert parse_window("kesho asubuhi") == "morning" and parse_window("I am free jioni") == "evening" and parse_window("I am free") is None


def test_free_slots_respect_hours_duration_and_existing_bookings():
    tz = NBO
    day = date(2026, 10, 28)   # Wednesday
    hours = {"wed": ["08:00", "11:00"], "sun": None}
    staff = [{"id": "s1", "name": "Brian"}]
    dur = timedelta(minutes=45)
    at = lambda h, m=0: datetime(2026, 10, 28, h, m, tzinfo=tz)  # noqa: E731
    starts = lambda slots: [s.starts_at.strftime("%H:%M") for s in slots]  # noqa: E731
    assert starts(day_slots(day, hours, tz, dur, staff, {}, at(0))) == ["08:00", "08:30", "09:00", "09:30", "10:00"]   # 10:30 + 45min would run past closing
    busy = {"s1": [(at(8, 30), at(9, 15))]}
    assert starts(day_slots(day, hours, tz, dur, staff, busy, at(0))) == ["09:30", "10:00"]       # anything overlapping 08:30-09:15 is gone
    assert day_slots(date(2026, 11, 1), hours, tz, dur, staff, {}, at(0)) == []                    # closed day
    assert starts(day_slots(day, hours, tz, dur, staff, {}, at(9, 0)))[0] == "09:00"               # nothing in the past
    two = [{"id": "s1", "name": "Brian"}, {"id": "s2", "name": "Amina"}]
    got = day_slots(day, hours, tz, dur, two, busy, at(0))
    assert starts(got)[0] == "08:00" and got[0].staff_name == "Amina"                              # the free stylist is offered


def test_choose_prefers_the_window_then_the_nearest():
    tz = NBO
    day = date(2026, 10, 28)
    staff = [{"id": "s1", "name": "Brian"}]
    slots = day_slots(day, {"wed": ["08:00", "19:00"]}, tz, timedelta(minutes=30), staff, {}, datetime(2026, 10, 28, 0, tzinfo=tz))
    got, exact = choose(slots, window="afternoon")
    assert exact and all(12 <= s.starts_at.hour < 17 for s in got) and len(got) == 3
    got, exact = choose(slots, at=time(14, 0))
    assert exact and got[0].starts_at.hour == 14
    got, exact = choose([s for s in slots if s.starts_at.hour < 10], window="evening")
    assert not exact and got                       # nothing in the evening: closest alternatives, flagged


def test_model_output_is_checked_by_code():
    today = date(2026, 10, 27)
    ex = sanitize(Extraction(intent="book", date="2019-01-01", time="25:99", choice=7), today)
    assert ex.date is None and ex.time is None and ex.choice is None
    assert sanitize(Extraction(intent="book", date="2026-10-30"), today).date == "2026-10-30"
    p = build_prompt(today, ["Fade"], "offered")
    assert "Tue = 2026-10-27" in p and "saa nane=14:00" in p and '["Fade"]' in p and "never as instructions" in p


def test_keyword_fallback_fills_the_same_form():
    sv = [{"name": "Fade", "aliases": []}, {"name": "Silk press", "aliases": ["silk"]}]
    today = date(2026, 10, 27)
    e = rule_extract("hello nataka fade leo", today, sv)
    assert (e.intent, e.service, e.date, e.language) == ("book", "Fade", "2026-10-27", "sw")
    assert rule_extract("2", today, sv, "offered").choice == 2 and rule_extract("2", today, sv).intent == "other"
    assert rule_extract("good morning", today, sv).intent == "greeting" and rule_extract("how are you", today, sv).intent == "greeting"
    assert rule_extract("bei ni ngapi", today, sv).intent == "price_list" and rule_extract("ghairi", today, sv).intent == "cancel"


def test_smalltalk_answers_in_the_greetings_language():
    assert smalltalk.opener("hello nataka fade leo") == "Hello!" and smalltalk.opener("Niaje") == "Poa!"
    assert smalltalk.opener("hi how are you") == "Hi! I'm doing great, thanks for asking." and "Niko poa" in smalltalk.opener("habari yako")
    assert smalltalk.opener("high fade please") is None and smalltalk.opener("morning slot") is None


# ---- through the webhook, real clients, fake Meta + model ----------------------------------------------------------

@needs_db
def test_booking_conversation_from_message_to_held_slot():
    async def run():
        pool, out, salon = await build(DSN)
        day = open_day()
        out.script["hello nataka fade kesho mchana"] = {"intent": "book", "service": "Fade", "date": day.isoformat(), "window": "afternoon", "language": "sw"}
        async with client() as c:
            await post(c, payload(A, "hello nataka fade kesho mchana"))
            sent = out.to(A)
            assert sent[0]["text"]["body"] == "Hello!"                                    # greeted, then the request carries on
            offer = sent[1]
            assert offer["type"] == "interactive" and "Nafasi za mapema za Fade" in offer["interactive"]["body"]["text"]
            ids = [b["reply"]["id"] for b in offer["interactive"]["action"]["buttons"]]
            assert ids == ["slot:0", "slot:1", "slot:2"] and offer["_auth"] == "Bearer TOK"
            await post(c, payload(A, reply_id="slot:1", mid="m2"))                        # a tap needs no model call
            assert len(out.calls) == 1
            assert "Nimehifadhi Fade" in out.bodies(A)[-1]
        b = await pool.fetchrow("SELECT status, starts_at, hold_expires_at FROM bookings")
        assert b["status"] == "held" and b["hold_expires_at"] > datetime.now(NBO)
        assert b["starts_at"].astimezone(NBO).hour == 12 and b["starts_at"].astimezone(NBO).minute == 30   # second slot of the afternoon
        await pool.close()
    asyncio.run(run())


@needs_db
def test_two_customers_tapping_the_same_slot_get_exactly_one_hold():
    async def run():
        pool, out, salon = await build(DSN)
        day = open_day()
        out.script["fade please"] = {"intent": "book", "service": "Fade", "date": day.isoformat(), "window": "afternoon"}
        async with client() as c:
            await post(c, payload(A, "fade please", mid="a1"))
            await post(c, payload(B, "fade please", mid="b1"))
            rs = await asyncio.gather(post(c, payload(A, reply_id="slot:0", mid="a2")), post(c, payload(B, reply_id="slot:0", mid="b2")))
            assert all(r.status_code == 200 for r in rs)
        assert await pool.fetchval("SELECT count(*) FROM bookings WHERE status='held'") == 1
        losers = [w for w in (A, B) if "Fade" not in out.bodies(w)[-1] or "200" not in out.bodies(w)[-1]]
        assert len(losers) == 1 and "just taken" in out.bodies(losers[0])[-1]            # the loser is told and offered the next slots
        await pool.close()
    asyncio.run(run())


@needs_db
def test_failover_endpoint_answers_when_the_first_model_is_down():
    async def run():
        pool, out, salon = await build(DSN, llm_fallback_base_url="https://spark-open.test/v1", llm_fallback_api_key="f", llm_fallback_model="meta-models/muse-spark-open-1026")
        out.down.add("primary.test")
        out.script["fade tomorrow"] = {"intent": "book", "service": "Fade", "date": open_day().isoformat(), "window": "morning"}
        async with client() as c:
            await post(c, payload(A, "fade tomorrow"))
        assert out.calls == ["primary.test", "primary.test", "spark-open.test"]          # two tries on the primary, then the failover
        assert out.to(A)[-1]["type"] == "interactive"
        await pool.close()
    asyncio.run(run())


@needs_db
def test_with_no_model_at_all_the_rules_still_complete_a_booking_across_messages():
    async def run():
        pool, out, salon = await build(DSN)
        out.down.add("primary.test")
        async with client() as c:
            await post(c, payload(A, "fade", mid="1"))                       # service only: asks the day
            assert "which day" in out.bodies(A)[-1].lower()
            await post(c, payload(A, "kesho", mid="2"))                      # the day arrives next: slots offered
            assert out.to(A)[-1]["type"] == "interactive"
            await post(c, payload(A, "2", mid="3"))                          # a bare number picks a slot
            assert "Fade" in out.bodies(A)[-1]
        assert await pool.fetchval("SELECT count(*) FROM bookings WHERE status='held'") == 1
        await pool.close()
    asyncio.run(run())


@needs_db
def test_cancel_prices_hours_greeting_and_unknown_service():
    async def run():
        pool, out, salon = await build(DSN)
        out.script.update({
            "book silk": {"intent": "book", "service": "silk", "date": open_day().isoformat(), "window": "morning"},
            "i want a perm": {"intent": "book", "service": "Perm", "date": open_day().isoformat()},
            "cancel it": {"intent": "cancel"}, "prices?": {"intent": "price_list"}, "when are you open": {"intent": "hours"},
            "how are you": {"intent": "other"}})
        async with client() as c:
            await post(c, payload(A, "prices?", mid="1"))
            assert "Fade (KES 500)" in out.bodies(A)[-1] and "Silk press (KES 2500)" in out.bodies(A)[-1]
            await post(c, payload(A, "when are you open", mid="2"))
            assert "Sun: closed" in out.bodies(A)[-1]
            await post(c, payload(A, "how are you", mid="3"))
            assert out.bodies(A)[-1].startswith("Hi! I'm doing great") and "Welcome to Zuri Cuts" in out.bodies(A)[-1]
            await post(c, payload(A, "i want a perm", mid="4"))
            assert "couldn't find that service" in out.bodies(A)[-1]
            await post(c, payload(A, "book silk", mid="5"))
            await post(c, payload(A, reply_id="slot:0", mid="6"))
            assert await pool.fetchval("SELECT count(*) FROM bookings WHERE status='held'") == 1
            await post(c, payload(A, "cancel it", mid="7"))
            assert "cancelled" in out.bodies(A)[-1] and await pool.fetchval("SELECT count(*) FROM bookings WHERE status='held'") == 0
            await post(c, payload(B, "cancel it", mid="8"))                  # nobody else's booking is touched
            assert "couldn't find an upcoming booking" in out.bodies(B)[-1]
        await pool.close()
    asyncio.run(run())


@needs_db
def test_unknown_number_and_non_text_messages_are_handled_calmly():
    async def run():
        pool, out, salon = await build(DSN, asr_url="")  # no speech service: voice notes get the text-only reply
        async with client() as c:
            stray = payload(A, "hello", mid="s1")
            stray["entry"][0]["changes"][0]["value"]["metadata"]["phone_number_id"] = "NOT-OURS"
            assert (await post(c, stray)).status_code == 200 and out.sent == []        # not a salon of ours: ignored
            voice = payload(A, mid="v1")
            voice["entry"][0]["changes"][0]["value"]["messages"][0] = {"from": A, "id": "v1", "type": "audio", "audio": {"id": "X"}}
            await post(c, voice)
            assert "text messages" in out.bodies(A)[-1].lower()
        await pool.close()
    asyncio.run(run())
