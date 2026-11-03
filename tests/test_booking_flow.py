import asyncio
import os
from datetime import date, datetime, time, timedelta

import pytest

from app import smalltalk
from app.availability import choose, day_slots
from app.ai.llm import build_prompt, rule_extract, sanitize, Extraction
from app.timeparse import parse_day, parse_time, parse_window, swahili_hour
from tests.harness import NBO, build, client, open_day, payload, post

DSN = os.environ.get("TEST_DATABASE_URL")
needs_db = pytest.mark.skipif(not DSN, reason="needs TEST_DATABASE_URL")
A, B = "254711000001", "254711000002"



def test_swahili_clock_counts_from_dawn():
    assert [swahili_hour(n) for n in (1, 2, 6, 7, 8, 12)] == [7, 8, 12, 1, 2, 6]
    assert parse_time("saa mbili asubuhi") == time(8) and parse_time("saa nane mchana") == time(14)
    assert parse_time("saa kumi jioni") == time(16) and parse_time("saa kumi na moja jioni") == time(17)
    assert parse_time("2pm") == time(14) and parse_time("14:30") == time(14, 30) and parse_time("12am") == time(0)
    assert parse_time("iko na slot ya fade leo around 4pm ama?") == time(16) and parse_time("fade kesho") is None


def test_pure_thanks_gets_warmth_not_a_lecture():
    assert smalltalk.thanks("Thank you!") == ("thanks", "en")
    assert smalltalk.thanks("perfect") == ("thanks", "en")
    assert smalltalk.thanks("Asante sana") == ("thanks", "sw")
    assert smalltalk.thanks("shukran") == ("thanks", "sw")
    assert smalltalk.thanks("sawa") == ("thanks", "sw")
    assert smalltalk.thanks("karibu") == ("thanks", "sw")
    assert smalltalk.thanks("nataka fade") is None
    assert smalltalk.thanks("thank you, cancel my booking") is None
    assert smalltalk.thanks("") is None
    assert smalltalk.thanks("ok") == ("thanks", "en")
    assert smalltalk.thanks("thank") == ("thanks", "en")


def test_thanks_and_onboarding_fillers_cover_each_other():
    from app import onboarding_chat
    for w in smalltalk.THANKS_FIRST_WORDS:
        assert smalltalk.thanks(w) is not None
        assert w in onboarding_chat._FILLERS
    for w in ("hi", "habari", "ndiyo", "hapana"):
        assert w in onboarding_chat._FILLERS
    assert smalltalk.language("Niaje", "en") == "sw"
    assert smalltalk.language("Start register", "sw") == "en"
    assert smalltalk.language("Who are you", "sw") == "en"
    assert smalltalk.language("ok", "sw") == "sw"
    assert smalltalk.language("ok", "en") == "en"


def test_days_and_windows():
    mon = date(2026, 10, 26)
    assert parse_day("kesho", mon) == date(2026, 10, 27) and parse_day("leo", mon) == mon
    assert parse_day("Jumamosi", mon) == date(2026, 10, 31) and parse_day("jumatatu", mon) == date(2026, 11, 2)
    assert parse_window("kesho asubuhi") == "morning" and parse_window("I am free jioni") == "evening" and parse_window("I am free") is None


def test_free_slots_respect_hours_duration_and_existing_bookings():
    tz = NBO
    day = date(2026, 10, 28)
    hours = {"wed": ["08:00", "11:00"], "sun": None}
    staff = [{"id": "s1", "name": "Brian"}]
    dur = timedelta(minutes=45)
    at = lambda h, m=0: datetime(2026, 10, 28, h, m, tzinfo=tz)  # noqa: E731
    starts = lambda slots: [s.starts_at.strftime("%H:%M") for s in slots]  # noqa: E731
    assert starts(day_slots(day, hours, tz, dur, staff, {}, at(0))) == ["08:00", "08:30", "09:00", "09:30", "10:00"]
    busy = {"s1": [(at(8, 30), at(9, 15))]}
    assert starts(day_slots(day, hours, tz, dur, staff, busy, at(0))) == ["09:30", "10:00"]
    assert day_slots(date(2026, 11, 1), hours, tz, dur, staff, {}, at(0)) == []
    assert starts(day_slots(day, hours, tz, dur, staff, {}, at(9, 0)))[0] == "09:00"
    two = [{"id": "s1", "name": "Brian"}, {"id": "s2", "name": "Amina"}]
    got = day_slots(day, hours, tz, dur, two, busy, at(0))
    assert starts(got)[0] == "08:00" and got[0].staff_name == "Amina"


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
    assert not exact and got


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



@needs_db
def test_booking_conversation_from_message_to_held_slot():
    async def run():
        pool, out, salon = await build(DSN)
        day = open_day()
        out.script["hello nataka fade kesho mchana"] = {"intent": "book", "service": "Fade", "date": day.isoformat(), "window": "afternoon", "language": "sw"}
        async with client() as c:
            await post(c, payload(A, "hello nataka fade kesho mchana"))
            sent = out.to(A)
            assert sent[0]["text"]["body"] == "Hello!"
            offer = sent[1]
            assert offer["type"] == "interactive" and "Nafasi za mapema za Fade" in offer["interactive"]["body"]["text"]
            ids = [b["reply"]["id"] for b in offer["interactive"]["action"]["buttons"]]
            assert ids == ["slot:0", "slot:1", "slot:2"] and offer["_auth"] == "Bearer TOK"
            await post(c, payload(A, reply_id="slot:1", mid="m2"))
            assert len(out.calls) == 1
            assert "Nimehifadhi Fade" in out.bodies(A)[-1]
        b = await pool.fetchrow("SELECT status, starts_at, hold_expires_at FROM bookings")
        assert b["status"] == "held" and b["hold_expires_at"] > datetime.now(NBO)
        assert b["starts_at"].astimezone(NBO).hour == 12 and b["starts_at"].astimezone(NBO).minute == 30
        await pool.close()
    asyncio.run(run())


@needs_db
def test_thanks_alone_is_answered_warmly_with_no_model_call():
    async def run():
        pool, out, salon = await build(DSN)
        async with client() as c:
            await post(c, payload(A, "Asante", mid="t1"))
            assert "Karibu" in out.bodies(A)[-1]
            await post(c, payload(B, "perfect", mid="t2"))
            assert "welcome" in out.bodies(B)[-1].lower()
        assert out.calls == []
        await pool.close()
    asyncio.run(run())


@needs_db
def test_language_follows_each_message_and_start_register_needs_no_slash():
    async def run():
        pool, out, salon = await build(DSN)
        async with client() as c:
            await post(c, payload(A, "Niaje", mid="l1"))
            assert "Karibu" in out.bodies(A)[-1]
            assert await pool.fetchval("SELECT lang FROM clients WHERE wa_id=$1", A) == "sw"
            await post(c, payload(A, "Start", mid="l2"))
            assert "booking assistant" in out.bodies(A)[-1]
            assert await pool.fetchval("SELECT lang FROM clients WHERE wa_id=$1", A) == "en"
            await post(c, payload(B, "Start register", mid="l3"))
            assert "salon name" in out.bodies(B)[-1].lower()
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
        assert len(losers) == 1 and "just taken" in out.bodies(losers[0])[-1]
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
        assert out.calls == ["primary.test", "primary.test", "spark-open.test"]
        assert out.to(A)[-1]["type"] == "interactive"
        await pool.close()
    asyncio.run(run())


@needs_db
def test_with_no_model_at_all_the_rules_still_complete_a_booking_across_messages():
    async def run():
        pool, out, salon = await build(DSN)
        out.down.add("primary.test")
        async with client() as c:
            await post(c, payload(A, "fade", mid="1"))
            assert "which day" in out.bodies(A)[-1].lower()
            await post(c, payload(A, "kesho", mid="2"))
            assert out.to(A)[-1]["type"] == "interactive"
            await post(c, payload(A, "2", mid="3"))
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
            assert "Fade" in out.bodies(A)[-1] and "KES 500" in out.bodies(A)[-1] and "Silk press" in out.bodies(A)[-1] and "KES 2500" in out.bodies(A)[-1]
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
            await post(c, payload(B, "cancel it", mid="8"))
            assert "couldn't find an upcoming booking" in out.bodies(B)[-1]
        await pool.close()
    asyncio.run(run())


@needs_db
def test_unknown_number_and_non_text_messages_are_handled_calmly():
    async def run():
        pool, out, salon = await build(DSN, asr_url="")
        async with client() as c:
            stray = payload(A, "hello", mid="s1")
            stray["entry"][0]["changes"][0]["value"]["metadata"]["phone_number_id"] = "NOT-OURS"
            assert (await post(c, stray)).status_code == 200 and out.sent == []
            voice = payload(A, mid="v1")
            voice["entry"][0]["changes"][0]["value"]["messages"][0] = {"from": A, "id": "v1", "type": "audio", "audio": {"id": "X"}}
            await post(c, voice)
            assert "text messages" in out.bodies(A)[-1].lower()
        await pool.close()
    asyncio.run(run())


@needs_db
def test_a_free_booking_confirms_and_schedules_reminders():
    async def run():
        from tests.test_payments import to_pay
        pool, out, salon = await build(DSN)
        await pool.execute("UPDATE salons SET deposit_kes=0")
        await pool.execute("INSERT INTO reminder_templates (salon_id, name, body, approved) VALUES ($1::uuid,'reminder_24h','Hi {salon} {when}',true), ($1::uuid,'reminder_2h','Soon {when}',true)", salon)
        async with client() as c:
            await to_pay(c, out, pool)
        assert await pool.fetchval("SELECT status FROM bookings") == "confirmed"
        starts = await pool.fetchval("SELECT starts_at FROM bookings")
        if starts.tzinfo is None:
            starts = starts.replace(tzinfo=NBO)
        expected = 2 if starts > datetime.now(NBO) + timedelta(hours=24) else 1
        assert await pool.fetchval("SELECT count(*) FROM reminder_jobs WHERE status='pending'") == expected
        await pool.close()
    asyncio.run(run())


@needs_db
def test_tapping_again_while_holding_gets_an_answer_not_silence():
    async def run():
        from tests.test_payments import to_pay
        pool, out, salon = await build(DSN)
        async with client() as c:
            await to_pay(c, out, pool)
            await post(c, payload(A, "1", mid="n1"))
        assert "still held" in out.bodies(A)[-1]
        assert "till 5012345" in out.bodies(A)[-1]
        assert await pool.fetchval("SELECT status FROM bookings") == "held"
        await pool.close()
    asyncio.run(run())


@needs_db
def test_an_expired_hold_does_not_block_the_calendar():
    async def run():
        from app.bookings import busy_times
        pool, out, salon = await build(DSN)
        staff = await pool.fetchval("SELECT id::text FROM staff")
        svc = await pool.fetchval("SELECT id::text FROM services WHERE name='Fade'")
        client_id = await pool.fetchval("INSERT INTO clients (salon_id, wa_id) VALUES ($1::uuid,'254700000099') RETURNING id::text", salon)
        start = datetime.combine(open_day(), time(14), NBO)
        end = start + timedelta(minutes=45)
        await pool.execute(
            "INSERT INTO bookings (salon_id, staff_id, service_id, client_id, starts_at, ends_at, status, hold_expires_at) "
            "VALUES ($1::uuid,$2::uuid,$3::uuid,$4::uuid,$5,$6,'held', now() - interval '1 minute')",
            salon, staff, svc, client_id, start, end)
        async with pool.acquire() as con:
            assert await busy_times(con, [staff], start - timedelta(hours=1), end + timedelta(hours=1)) == {}
        await pool.close()
    asyncio.run(run())


@needs_db
def test_sheng_gets_a_swahili_answer_and_returning_customers_are_welcomed_back():
    async def run():
        pool, out, salon = await build(DSN)
        async with client() as c:
            await post(c, payload(B, "niaje maze", mid="s1"))
            assert "Karibu" in out.bodies(B)[-1]
            assert "Welcome back" not in out.bodies(B)[-1] and "Karibu tena" not in out.bodies(B)[-1]
            staff = await pool.fetchval("SELECT id::text FROM staff")
            svc = await pool.fetchval("SELECT id::text FROM services WHERE name='Fade'")
            client_id = await pool.fetchval("SELECT id::text FROM clients WHERE wa_id=$1", B)
            start = datetime.now(NBO) - timedelta(days=7)
            await pool.execute(
                "INSERT INTO bookings (salon_id, staff_id, service_id, client_id, starts_at, ends_at, status) "
                "VALUES ($1::uuid,$2::uuid,$3::uuid,$4::uuid,$5,$6,'no_show')",
                salon, staff, svc, client_id, start, start + timedelta(minutes=45))
            await post(c, payload(B, "habari", mid="s2"))
            assert "Karibu tena" in out.bodies(B)[-1]
        await pool.close()
    asyncio.run(run())


@needs_db
def test_an_expiry_leaves_a_hold_saved_by_a_late_payment_and_its_reminders_alone():
    async def run():
        from app.bookings import expire_holds
        pool, out, salon = await build(DSN)
        staff = await pool.fetchval("SELECT id::text FROM staff")
        svc = await pool.fetchval("SELECT id::text FROM services WHERE name='Fade'")
        client_id = await pool.fetchval("INSERT INTO clients (salon_id, wa_id) VALUES ($1::uuid,'254700000099') RETURNING id::text", salon)
        start = datetime.now(NBO) + timedelta(days=2)
        booking = await pool.fetchval(
            "INSERT INTO bookings (salon_id, staff_id, service_id, client_id, starts_at, ends_at, status, hold_expires_at) "
            "VALUES ($1::uuid,$2::uuid,$3::uuid,$4::uuid,$5,$6,'held', now() - interval '1 minute') RETURNING id::text",
            salon, staff, svc, client_id, start, start + timedelta(minutes=45))
        await pool.execute("INSERT INTO payments (booking_id, salon_id, phone, amount_kes) VALUES ($1::uuid,$2::uuid,'254700000099',200)", booking, salon)
        await pool.execute("INSERT INTO reminder_jobs (booking_id, salon_id, client_id, wa_id, run_at, template_name) VALUES ($1::uuid,$2::uuid,$3::uuid,'254700000099',now(),'reminder_2h')", booking, salon, client_id)
        assert await expire_holds(pool) == 0
        assert await pool.fetchval("SELECT status FROM bookings") == "held"
        assert await pool.fetchval("SELECT status FROM reminder_jobs") == "pending"
        await pool.close()
    asyncio.run(run())
