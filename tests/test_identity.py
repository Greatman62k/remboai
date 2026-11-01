"""Bot identity and help: the shared bot introduces itself as RemboAI, a linked sender
meets their business's assistant, and pumping the business list gets a nudge, not the list."""
import asyncio
import os

import pytest

from app import smalltalk
from tests.harness import PID, build, client, open_day, payload, post, post_tg, tg_update

DSN = os.environ.get("TEST_DATABASE_URL")
needs_db = pytest.mark.skipif(not DSN, reason="needs TEST_DATABASE_URL")
A, C, D = "254711000001", "254711000003", "254711000004"


def test_identity_detector_covers_both_languages_but_not_prices():
    for q in ("who are you", "Who are you?", "what are you", "who is remboai",
              "what is rembo", "introduce yourself", "what is your name",
              "wewe ni nani", "nani wewe?", "jina lako nani", "rembo ni nini"):
        assert smalltalk.asks_identity(q), q
    for q in ("fade kesho", "what are you charging for fade", "asante", "ok",
              "i want to book", "prices", ""):
        assert not smalltalk.asks_identity(q), q


def test_help_detector_answers_how_to_use_it():
    for q in ("help", "Help!", "/help", "msaada", "nisaidie", "what can you do",
              "how do i use this", "jinsi ya kutumia"):
        assert smalltalk.asks_help(q), q
    for q in ("help me cancel my booking", "fade kesho", "who are you", ""):
        assert not smalltalk.asks_help(q), q


@needs_db
def test_single_salon_sender_meets_their_business_assistant():
    async def run():
        pool, out, _salon = await build(DSN)
        async with client() as c:
            await post(c, payload(A, "who are you", mid="i1"))
            assert "Zuri Cuts" in out.bodies(A)[-1] and "REGISTER" not in out.bodies(A)[-1]
            await post(c, payload(A, "help", mid="i2"))
            assert "Zuri Cuts" in out.bodies(A)[-1] and "prices" in out.bodies(A)[-1]
        await pool.close()
    asyncio.run(run())


@needs_db
def test_unallocated_sender_meets_remboai_itself():
    async def run():
        pool, out, _salon = await build(DSN)
        await pool.execute("INSERT INTO salons (name, wa_phone_number_id, slug) VALUES ('Malaika', $1, 'malaika')", PID)
        async with client() as c:
            await post(c, payload(C, "who are you", mid="u1"))
            body = out.bodies(C)[-1]
            assert "RemboAI" in body and "REGISTER" in body and "Zuri Cuts" not in body and "Malaika" not in body
            await post(c, payload(C, "how do i use this", mid="u2"))
            assert "REGISTER" in out.bodies(C)[-1]
        await pool.close()
    asyncio.run(run())


@needs_db
def test_linked_sender_meets_their_business_assistant():
    async def run():
        pool, out, _salon = await build(DSN)
        await pool.execute("INSERT INTO salons (name, wa_phone_number_id, slug) VALUES ('Malaika', $1, 'malaika')", PID)
        async with client() as c:
            await post(c, payload(C, "/start malaika", mid="l0"))
            await post(c, payload(C, "who are you", mid="l1"))
            assert "Malaika" in out.bodies(C)[-1] and "RemboAI" not in out.bodies(C)[-1]
        await pool.close()
    asyncio.run(run())


@needs_db
def test_exact_business_name_binds_without_asking():
    async def run():
        pool, out, _salon = await build(DSN)
        await pool.execute("INSERT INTO salons (name, wa_phone_number_id, slug) VALUES ('Malaika', $1, 'malaika')", PID)
        E = "254711000005"
        async with client() as c:
            await post(c, payload(E, "malaika", mid="f0"))
            assert await pool.fetchval("SELECT count(*) FROM sender_salon WHERE sender=$1", E) == 1
        await pool.close()
    asyncio.run(run())


@needs_db
def test_fuzzy_business_name_asks_before_binding():
    async def run():
        pool, out, _salon = await build(DSN)
        await pool.execute("INSERT INTO salons (name, wa_phone_number_id, slug) VALUES ('Malaika', $1, 'malaika')", PID)
        E = "254711000006"
        async with client() as c:
            await post(c, payload(E, "malaik", mid="f1"))
            assert "Did you mean Malaika?" in out.bodies(E)[-1]
            assert await pool.fetchval("SELECT count(*) FROM sender_salon WHERE sender=$1", E) == 0
            await post(c, payload(E, "YES", mid="f2"))
            assert await pool.fetchval("SELECT count(*) FROM sender_salon WHERE sender=$1", E) == 1
            assert "Did you mean" not in out.bodies(E)[-1]
        await pool.close()
    asyncio.run(run())


@needs_db
def test_repeated_picker_pumping_gets_a_nudge_and_a_strike():
    async def run():
        pool, out, _salon = await build(DSN)
        await pool.execute("INSERT INTO salons (name, wa_phone_number_id, slug) VALUES ('Malaika', $1, 'malaika')", PID)
        async with client() as c:
            await post(c, payload(D, "blah blah", mid="p1"))
            await post(c, payload(D, "still thinking", mid="p2"))
            await post(c, payload(D, "more chatter", mid="p3"))
            bodies = out.bodies(D)
            assert "Which business" in bodies[0] and "Which business" in bodies[1]
            assert "Which business" not in bodies[2] and "number or the name" in bodies[2]
            n = await pool.fetchval("SELECT count(*) FROM abuse_strikes WHERE sender=$1 AND reason='picker_spam'", D)
            assert n == 1
        await pool.close()
    asyncio.run(run())


@needs_db
def test_owner_identity_and_id_help_do_not_enter_customer_flow():
    async def run():
        pool, out, _salon = await build(DSN)
        await pool.execute("UPDATE salons SET owner_wa_id=$1", A)
        async with client() as c:
            await post(c, payload(A, "who are you", mid="w1"))
            body = out.bodies(A)[-1]
            assert "Zuri Cuts" in body and "Thank you for reaching out" not in body
            await post(c, payload(A, "Id", mid="w2"))
            assert A in out.bodies(A)[-1]
        await pool.close()
    asyncio.run(run())


@needs_db
def test_returning_customer_gets_one_welcome_not_two():
    async def run():
        pool, out, _salon = await build(DSN)
        async with client() as c:
            await post(c, payload(A, "hi", mid="g0"))
            assert "Welcome to Zuri Cuts" in out.bodies(A)[-1]
            cid = await pool.fetchval("SELECT id::text FROM clients WHERE wa_id=$1", A)
            sid = await pool.fetchval("SELECT id::text FROM services WHERE name='Fade'")
            stid = await pool.fetchval("SELECT id::text FROM staff LIMIT 1")
            await pool.execute(
                "INSERT INTO bookings (salon_id, client_id, staff_id, service_id, starts_at, ends_at, status) "
                "VALUES ($1::uuid, $2::uuid, $3::uuid, $4::uuid, now() - interval '2 days', now() - interval '2 days' + interval '45 minutes', 'confirmed')",
                _salon, cid, stid, sid)
            await post(c, payload(A, "hi", mid="g1"))
            body = out.bodies(A)[-1]
            assert "Welcome back! What would you like" in body and "We are delighted" not in body
        await pool.close()
    asyncio.run(run())


@needs_db
def test_bot_with_no_businesses_introduces_itself_and_registers():
    async def run():
        pool, out, _salon = await build(DSN)
        await pool.execute("UPDATE salons SET active=false")
        async with client() as c:
            await post_tg(c, tg_update(1, 9001, "hello there"))
            assert "RemboAI" in out.tg_bodies("9001")[-1]
            await post_tg(c, tg_update(2, 9001, "who are you"))
            assert "RemboAI" in out.tg_bodies("9001")[-1]
            await post_tg(c, tg_update(3, 9001, "/id"))
            assert "tg:9001" in out.tg_bodies("9001")[-1]
            await post_tg(c, tg_update(4, 9001, "register"))
            assert "salon name" in out.tg_bodies("9001")[-1].lower()
        await pool.close()
    asyncio.run(run())


@needs_db
def test_stranger_gets_specific_answers_not_one_fallback():
    async def run():
        pool, out, _salon = await build(DSN)
        await pool.execute("UPDATE salons SET active=false")
        async with client() as c:
            await post_tg(c, tg_update(1, 9003, "hello"))
            assert "Hello! I am RemboAI" in out.tg_bodies("9003")[-1]
            await post_tg(c, tg_update(2, 9003, "asante"))
            assert "Karibu" in out.tg_bodies("9003")[-1]
            await post_tg(c, tg_update(3, 9003, voice={"file_id": "VOICE1", "duration": 4}))
            assert "listen to voice notes" in out.tg_bodies("9003")[-1]
            await post_tg(c, tg_update(4, 9003, photo=[{"file_id": "PHOTO_S", "file_size": 10}, {"file_id": "PHOTO_L", "file_size": 900}]))
            assert "read photos" in out.tg_bodies("9003")[-1]
        await pool.close()
    asyncio.run(run())


@needs_db
def test_unrecognized_chatter_gets_a_nudge_the_third_time():
    async def run():
        pool, out, _salon = await build(DSN)
        await pool.execute("UPDATE salons SET active=false")
        async with client() as c:
            await post_tg(c, tg_update(1, 9002, "anybody here?"))
            await post_tg(c, tg_update(2, 9002, "still there?"))
            await post_tg(c, tg_update(3, 9002, "hmm okay?"))
            bodies = out.tg_bodies("9002")
            assert "RemboAI" in bodies[0] and "RemboAI" in bodies[1]
            assert "RemboAI" not in bodies[2] and "REGISTER" in bodies[2]
            row = await pool.fetchval("SELECT state FROM stranger_state WHERE sender='tg:9002'")
            assert row["main_sends"] == 2 and len(row["history"]) == 6
        await pool.close()
    asyncio.run(run())


@needs_db
def test_asking_who_mid_booking_loses_nothing():
    async def run():
        pool, out, _salon = await build(DSN)
        out.script["fade z"] = {"intent": "book", "service": "Fade", "date": open_day().isoformat(), "window": "afternoon"}
        async with client() as c:
            await post(c, payload(A, "fade z", mid="z1"))
            assert await pool.fetchval("SELECT state->>'state' FROM conversations WHERE wa_id=$1", A) == "offered"
            await post(c, payload(A, "who are you", mid="z2"))
            assert "Zuri Cuts" in out.bodies(A)[-1]
            assert await pool.fetchval("SELECT state->>'state' FROM conversations WHERE wa_id=$1", A) == "offered"
            await post(c, payload(A, reply_id="slot:0", mid="z3"))
            assert await pool.fetchval("SELECT state->>'state' FROM conversations WHERE wa_id=$1", A) == "held"
        await pool.close()
    asyncio.run(run())
