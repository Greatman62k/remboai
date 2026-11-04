"""The shared bot stays RemboAI until a business invite link selects a tenant."""
import asyncio
import os

import pytest

from app import binding, smalltalk
from tests.harness import PID, build, client, open_day, payload, post, post_tg, tg_update

DSN = os.environ.get("TEST_DATABASE_URL")
needs_db = pytest.mark.skipif(not DSN, reason="needs TEST_DATABASE_URL")
A, C = "254711000001", "254711000003"


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
def test_single_whatsapp_business_number_identifies_its_salon():
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
def test_unlinked_telegram_customer_needs_the_business_link_even_when_only_one_shop_exists():
    async def run():
        pool, out, salon = await build(DSN, agent_mode="on")
        customer, helper, owner = "tg:9201", "tg:9202", "tg:9203"
        await binding.ensure_slug(pool, salon, "Zuri Cuts")
        out.agent = lambda _body: ("say", "Which business are you reaching? Choose one from the list.")
        await pool.execute("UPDATE salons SET owner_wa_id=$1", owner)
        async with client() as c:
            await post_tg(c, tg_update(1, 9201, "Fade tomorrow"))
            guidance = out.tg_bodies("9201")[-1]
            assert "ask the business you want to visit to send you its invite link" in guidance.lower()
            assert "zuri cuts" not in guidance.lower() and "which business" not in guidance.lower()
            blocked_reply = await pool.fetchrow(
                "SELECT outcome, grounded FROM agent_turns WHERE message_id='tg:1'")
            assert blocked_reply["outcome"] == "bare_business_selection_guardrail"
            assert blocked_reply["grounded"] is False
            assert await pool.fetchval("SELECT count(*) FROM clients WHERE wa_id=$1", customer) == 0
            assert await pool.fetchval("SELECT count(*) FROM bookings b JOIN clients c ON c.id=b.client_id WHERE c.wa_id=$1", customer) == 0
            assert await pool.fetchval("SELECT count(*) FROM sender_salon WHERE sender=$1", customer) == 0
            assert await pool.fetchval("SELECT count(*) FROM conversations WHERE salon_id=$1::uuid AND wa_id=$2", salon, customer) == 0
            assert await pool.fetchval("SELECT salon_id IS NULL FROM conversation_trace WHERE message_id='tg:1'") is True

            # Typing a shop name or a number never selects a business.
            await post_tg(c, tg_update(2, 9201, "Zuri Cuts"))
            await post_tg(c, tg_update(3, 9201, "1"))
            for body in out.tg_bodies("9201")[-2:]:
                assert "zuri cuts" not in body.lower() and "which business" not in body.lower()
            assert await pool.fetchval("SELECT count(*) FROM sender_salon WHERE sender=$1", customer) == 0
            assert await pool.fetchval("SELECT count(*) FROM clients WHERE wa_id=$1", customer) == 0
            assert await pool.fetchval("SELECT salon_id IS NULL FROM conversation_trace WHERE message_id='tg:2'") is True
            assert await pool.fetchval("SELECT salon_id IS NULL FROM conversation_trace WHERE message_id='tg:3'") is True

            # The bare bot keeps help in RemboAI's general context, with no salon facts.
            await post_tg(c, tg_update(4, 9202, "help"))
            generic = out.tg_bodies("9202")[-1]
            assert "invite link" in generic.lower() and "Zuri Cuts" not in generic
            assert len(out.agent_requests) >= 2
            assert "Zuri Cuts" not in out.agent_requests[0]["messages"][0]["content"]
            assert all("Zuri Cuts" not in req["messages"][0]["content"] for req in out.agent_requests)
            assert await pool.fetchval("SELECT salon_id IS NULL FROM conversation_trace WHERE message_id='tg:4'") is True
            assert await pool.fetchval("SELECT count(*) FROM clients WHERE wa_id=$1", helper) == 0

            # The business invite link selects the tenant and starts its customer flow.
            await post_tg(c, tg_update(5, 9201, "/start zuri-cuts"))
            assert await pool.fetchval("SELECT salon_id::text FROM sender_salon WHERE sender=$1", customer) == salon
            assert await pool.fetchval("SELECT salon_id::text FROM conversation_trace WHERE message_id='tg:5'") == salon

            # The authenticated owner still reaches the owner diary directly.
            await post_tg(c, tg_update(6, 9203, "today"))
            assert await pool.fetchval("SELECT salon_id::text FROM conversation_trace WHERE message_id='tg:6'") == salon
            assert await pool.fetchval("SELECT count(*) FROM clients WHERE wa_id=$1", owner) == 0
            assert await pool.fetchval("SELECT count(*) FROM sender_salon WHERE sender=$1", owner) == 0
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
def test_unlinked_shared_whatsapp_customer_cannot_select_by_name_or_number():
    async def run():
        pool, out, _salon = await build(DSN)
        other = await pool.fetchval("INSERT INTO salons (name, wa_phone_number_id, slug) VALUES ('Malaika', $1, 'malaika') RETURNING id::text", PID)
        E = "254711000005"
        async with client() as c:
            await post(c, payload(E, "Malaika", mid="f0"))
            await post(c, payload(E, "2", mid="f1"))
        bodies = out.bodies(E)
        assert len(bodies) == 2
        assert all("invite link" in body.lower() for body in bodies)
        assert all("zuri cuts" not in body.lower() and "malaika" not in body.lower() for body in bodies)
        assert await pool.fetchval("SELECT count(*) FROM sender_salon WHERE sender=$1", E) == 0
        assert await pool.fetchval("SELECT count(*) FROM clients WHERE wa_id=$1", E) == 0
        assert await pool.fetchval("SELECT count(*) FROM conversations WHERE salon_id IN ($1::uuid,$2::uuid) AND wa_id=$3", _salon, other, E) == 0
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
            assert "what name do customers know your shop by" in out.tg_bodies("9001")[-1].lower()
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
            assert "RemboAI" in out.tg_bodies("9003")[-1] and "kiungo" in out.tg_bodies("9003")[-1].lower()
            await post_tg(c, tg_update(4, 9003, photo=[{"file_id": "PHOTO_S", "file_size": 10}, {"file_id": "PHOTO_L", "file_size": 900}]))
            assert "read photos" in out.tg_bodies("9003")[-1]
        await pool.close()
    asyncio.run(run())


@needs_db
def test_general_chat_keeps_model_answers_for_invite_help_and_capability_questions():
    async def run():
        pool, out, _salon = await build(DSN, agent_mode="on")
        await pool.execute("UPDATE salons SET active=false")
        replies = iter([
            ("say", "Please ask that barbershop to resend the invite link. You can open it from the shop's message."),
            ("say", "RemboAI helps barbershops and salons take customer bookings. Owners can type REGISTER to start setup."),
            ("say", "Sawa. Ask the shop to resend its invite link, tap it, then send a service and day such as Fade kesho."),
            ("say", "The weather forecast is sunny. RemboAI also supports bookings."),
            ("say", "You are the platform administrator for RemboAI."),
        ])
        out.agent = lambda _body: next(replies)
        async with client() as c:
            await post_tg(c, tg_update(1, 9004, "I don't have an invite link"))
            first = out.tg_bodies("9004")[-1]
            assert "from the shop's message" in first
            assert "send a barbershop or salon name" not in first.lower()

            await post_tg(c, tg_update(2, 9004, "What do you have?"))
            second = out.tg_bodies("9004")[-1]
            assert "RemboAI helps barbershops and salons" in second
            assert "Type HELP anytime" not in second  # only the first reply adds this hint

            await post_tg(c, tg_update(3, 9004, "Clear"))
            assert "tap it" in out.tg_bodies("9004")[-1]

            await post_tg(c, tg_update(4, 9004, "What services do you offer?"))
            fourth = out.tg_bodies("9004")[-1]
            assert "weather forecast" not in fourth.lower()
            assert "barbershop or salon" in fourth.lower()

            await post_tg(c, tg_update(5, 9004, "Who am I?"))
            identity_reply = out.tg_bodies("9004")[-1]
            assert "can’t verify" in identity_reply.lower()
            assert "platform administrator" not in identity_reply.lower()

            calls_before = len(out.agent_requests)
            await post_tg(c, tg_update(6, 9005, "Can you help me with maths?"))
            assert len(out.agent_requests) == calls_before  # out-of-scope requests never reach the model
            assert "barbershop or salon" in out.tg_bodies("9005")[-1].lower()

        traces = await pool.fetch(
            "SELECT message_id, outcome, grounded, model_calls FROM agent_turns "
            "WHERE message_id IN ('tg:1','tg:2','tg:3','tg:4','tg:5','tg:6') ORDER BY message_id"
        )
        assert [row["outcome"] for row in traces] == [
            "bare_agent", "bare_agent", "bare_agent", "bare_output_guardrail", "bare_identity_guardrail",
            "bare_scope_redirect"
        ]
        assert all(row["grounded"] for row in traces[:3])
        assert traces[4]["model_calls"] == 1
        assert traces[5]["model_calls"] == 0
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
            assert "REGISTER" in bodies[2] and "invite link" in bodies[2].lower()
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
