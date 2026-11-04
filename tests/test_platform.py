"""Control hub and lifecycle: the platform owner runs shops, the quiet get warned twice, then deleted."""
import asyncio
import json
import os

import pytest

from app import lifecycle, platform
from tests.harness import PID, build, client, payload, post, post_tg, tg_update

DSN = os.environ.get("TEST_DATABASE_URL")
needs_db = pytest.mark.skipif(not DSN, reason="needs TEST_DATABASE_URL")
A, P = "254711000001", "tg:9001"


def test_owner_route_parser_requires_the_explicit_prefix_and_parses_salon_scope():
    assert platform.owner_request("/owner today") == (None, "today")
    assert platform.owner_request("/owner@remboai_bot zuri-cuts | resolve") == ("zuri-cuts", "resolve")
    assert platform.owner_request("/owner") == (None, "")
    assert platform.owner_request("owner today") is None


def test_platform_commands_understand_role_and_natural_business_lists():
    assert platform.ROLE.match("What is my role")
    assert platform.SHOPS.match("Return list")
    assert platform.SHOPS.match("list businesses")


def test_platform_admin_help_and_role_are_scannable_plain_text_lists():
    from app.messages import t

    for text in (platform.HELP_TEXT, platform.ROLE_TEXT):
        assert "1. " in text
        assert "del Brian's Kinyozi YES" in text
        assert "\n- " not in text
    assert platform.HELP_TEXT.startswith("Platform administrator help\n")
    assert platform.ROLE_TEXT.startswith("You are authenticated as a RemboAI platform administrator.")
    for lang in ("en", "sw"):
        owner_help = t(lang, "owner_help")
        assert owner_help.count("\n") == 7
        assert "\n1. " in owner_help and "\n7. " in owner_help
        assert "\n- " not in owner_help
    assert "payment pochi 0712 345 678" in t("en", "owner_help")
    assert "payment send 0712 345 678" in t("sw", "owner_help")


def test_platform_model_reply_is_cleaned_for_plain_text_chat():
    from app.agent.platform_assistant import _clean_reply

    body = _clean_reply("Great — here's help:\n\n- **Businesses**: `shops`\n- **Totals**: `stats`")
    assert body == "Great, here's help:\n\n• Businesses: shops\n• Totals: stats"
    assert "—" not in body and "–" not in body
    assert "**" not in body and "`" not in body


def test_platform_model_failure_is_explained_before_command_fallback():
    from types import SimpleNamespace
    from app.agent.platform_assistant import _fallback_reply

    body = _fallback_reply(SimpleNamespace(unavailable=True, problems=[]))
    assert body.startswith("I couldn't get an assistant response just now.")
    assert platform.HELP_TEXT in body
    sw_body = _fallback_reply(SimpleNamespace(unavailable=True, problems=[]), "sw")
    assert platform.help_text("sw") in sw_body


def test_platform_partial_business_name_is_rejected_and_reply_history_is_saved(monkeypatch):
    class Pool:
        def __init__(self):
            self.saved = None
            self.lookups = 0

        async def fetchrow(self, _query, _sender):
            return {"history": '[{"role":"assistant","content":"Earlier reply"}]'}

        async def fetch(self, _query, *_args):
            self.lookups += 1
            return []

        async def execute(self, query, *args):
            if "INSERT INTO platform_agent_sessions" in query:
                self.saved = args

    class Context:
        def __init__(self):
            self.pool = Pool()

    sent = []

    async def send_plain(_ctx, sender, body):
        sent.append((sender, body))

    monkeypatch.setattr(platform.outbound, "send_plain", send_plain)
    ctx = Context()

    async def run():
        assert await platform.handle(ctx, P, "Del brian") is True

    asyncio.run(run())

    assert ctx.pool.saved is not None
    assert ctx.pool.saved[0] == P
    reply = json.loads(ctx.pool.saved[1])[-1]
    assert reply["role"] == "assistant"
    assert "No exact business match" in reply["content"]
    assert sent == [(P, reply["content"])]


def test_primary_and_backup_platform_admin_ids_are_recognized():
    from types import SimpleNamespace

    settings = SimpleNamespace(platform_owner_id="tg:primary", platform_owner_ids="tg:backup1, tg:backup2")
    assert platform.is_platform(settings, "tg:primary")
    assert platform.is_platform(settings, "tg:backup2")
    assert not platform.is_platform(settings, "tg:other")


def test_doctor_checks_backup_admin_ids_for_salon_owner_collisions():
    from types import SimpleNamespace
    from app.doctor import _platform_identity_overlaps

    settings = SimpleNamespace(platform_owner_id="tg:primary", platform_owner_ids="tg:backup")
    ids, overlap = _platform_identity_overlaps(settings, [{"owner_wa_id": "tg:backup"}])
    assert ids == {"tg:primary", "tg:backup"}
    assert overlap == 1


def test_unknown_start_slug_is_not_a_customer_salon_selection():
    import asyncio
    from types import SimpleNamespace
    from app import conversation

    class Pool:
        async def fetchrow(self, query, sender):
            return None

    class Context:
        pool = Pool()

    salon = {"id": "salon-1"}
    msg = SimpleNamespace(text="/start unknown-salon", sender="tg:7001")

    async def run():
        return await conversation._resolve_salon(Context(), msg, salon)

    resolved, linked = asyncio.run(run())
    assert resolved == salon
    assert linked is False


@needs_db
def test_platform_owner_defaults_to_hub_and_can_route_only_to_owned_salon():
    async def run():
        pool, out, salon = await build(DSN, platform_owner_id=P, till=False)
        await pool.execute("UPDATE salons SET owner_wa_id=$1, slug='zuri-cuts'", P)
        await pool.execute("INSERT INTO clients (salon_id, wa_id) VALUES ($1::uuid,$2)", salon, P)
        async with client() as c:
            await post_tg(c, tg_update(1, 9001, "nataka fade tomorrow afternoon"))
            assert out.tg_bodies("9001")[-1] == platform.help_text("sw")
            assert await pool.fetchval("SELECT count(*) FROM bookings") == 0
            assert await pool.fetchval("SELECT count(*) FROM clients WHERE wa_id=$1", P) == 1

            await post_tg(c, tg_update(2, 9001, "/owner zuri-cuts | today"))
            assert "No bookings" in out.tg_bodies("9001")[-1]

            await pool.execute("UPDATE salons SET owner_wa_id='tg:other'")
            await post_tg(c, tg_update(3, 9001, "/owner today"))
            assert "No business owned by this account" in out.tg_bodies("9001")[-1]
            assert await pool.fetchval("SELECT count(*) FROM bookings") == 0
        await pool.close()
    asyncio.run(run())


@needs_db
def test_platform_owner_can_register_own_salon_after_confirming_ownership():
    async def run():
        pool, out, salon = await build(DSN, platform_owner_id=P, till=False)
        await pool.execute("UPDATE salons SET owner_wa_id=$1", P)
        initial_salons = await pool.fetchval("SELECT count(*) FROM salons")
        async with client() as c:
            await post_tg(c, tg_update(1, 9001, "What is my role"))
            assert "authenticated as a RemboAI platform administrator" in out.tg_bodies("9001")[-1]

            await post_tg(c, tg_update(2, 9001, "Return list"))
            assert "Businesses:" in out.tg_bodies("9001")[-1]

            await post_tg(c, tg_update(3, 9001, "I want to register a business"))
            assert "business owned by the same account" in out.tg_bodies("9001")[-1]
            assert await pool.fetchval("SELECT state->>'step' FROM onboarding_sessions WHERE sender=$1", P) == "confirm_platform_owner"
            assert await pool.fetchval("SELECT count(*) FROM salons") == initial_salons

            await post_tg(c, tg_update(4, 9001, "Yes"))
            assert await pool.fetchval("SELECT state->>'step' FROM onboarding_sessions WHERE sender=$1", P) == "ask_name"
            await post_tg(c, tg_update(5, 9001, "I want to book Fade tomorrow afternoon"))
            assert await pool.fetchval("SELECT count(*) FROM bookings") == 0
            assert await pool.fetchval("SELECT count(*) FROM clients WHERE wa_id=$1", P) == 0
            assert await pool.fetchval("SELECT state->>'step' FROM onboarding_sessions WHERE sender=$1", P) == "ask_name"

            await post_tg(c, tg_update(6, 9001, "/owner today"))
            assert "No bookings" in out.tg_bodies("9001")[-1]

            await post_tg(c, tg_update(7, 9001, "Stop"))
            assert "Registration cancelled" in out.tg_bodies("9001")[-1]
            assert await pool.fetchval("SELECT count(*) FROM onboarding_sessions WHERE sender=$1", P) == 0
            assert await pool.fetchval("SELECT count(*) FROM bookings") == 0
            assert await pool.fetchval("SELECT count(*) FROM salons") == initial_salons
            assert await pool.fetchval("SELECT count(*) FROM agent_turns WHERE outcome='registration_agent'") == 0
        await pool.close()
    asyncio.run(run())


@needs_db
def test_platform_owner_confirmation_uses_model_for_first_registration_question():
    async def run():
        from app import onboarding_chat
        pool, out, _salon = await build(DSN, platform_owner_id=P, agent_mode="on", till=False)
        out.agent = lambda _body: ("say", "What name should customers know this business by?")
        async with client() as c:
            await post_tg(c, tg_update(1, 9001, "I want to register a business"))
            assert "owned by the same account" in out.tg_bodies("9001")[-1]
            assert out.agent_requests == []  # the ownership disclosure and consent stay code-controlled
            await post_tg(c, tg_update(2, 9001, "Yes"))
            assert "What name should customers know this business by?" in out.tg_bodies("9001")[-1]
        trace = await pool.fetchrow(
            "SELECT outcome, model_calls FROM agent_turns WHERE message_id=$1", "tg:2")
        assert trace["outcome"] == "registration_agent" and trace["model_calls"] == 1
        state = await onboarding_chat.load_session(pool, P)
        assert state["step"] == "ask_name" and not state["data"]
        assert await pool.fetchval("SELECT count(*) FROM salons") == 1
        await pool.close()
    asyncio.run(run())


@needs_db
def test_platform_assistant_uses_read_only_tools_for_natural_questions():
    async def run():
        pool, out, salon = await build(DSN, platform_owner_id=P, agent_mode="on", till=False)
        moves = iter([
            ("tool", "platform_totals", {}),
            ("say", "The platform currently has 1 business, which is active, and 0 bookings."),
        ])
        out.agent = lambda _body: next(moves)
        async with client() as c:
            await post_tg(c, tg_update(1, 9001, "Can you summarize how the platform is doing?"))
            assert "platform currently has 1 business" in out.tg_bodies("9001")[-1]
            assert len(out.agent_requests) == 2
            tool_names = {tool["function"]["name"] for tool in out.agent_requests[0]["tools"]}
            assert tool_names == {"list_businesses", "platform_totals", "business_status"}

            trace = await pool.fetchrow(
                "SELECT outcome, sender, salon_id, model_calls, steps FROM agent_turns WHERE message_id='tg:1'")
            assert trace["outcome"] == "platform_agent"
            assert trace["sender"] == "platform_admin"
            assert trace["salon_id"] is None
            assert trace["model_calls"] == 2
            assert "platform_totals" in str(trace["steps"])
            assert await pool.fetchval("SELECT count(*) FROM platform_agent_sessions WHERE sender=$1", P) == 1
            assert await pool.fetchval("SELECT active FROM salons WHERE id=$1::uuid", salon) is True
            assert await pool.fetchval("SELECT count(*) FROM bookings") == 0
        await pool.close()
    asyncio.run(run())


@needs_db
def test_platform_owner_voice_is_transcribed_and_never_runs_mutating_commands():
    async def run():
        pool, out, salon = await build(DSN, platform_owner_id=P, agent_mode="on", till=False)
        moves = iter([
            ("tool", "platform_totals", {}),
            ("say", "The platform has 1 business and 0 bookings."),
            ("say", "I cannot delete a business for you. Please use the typed command and confirmation."),
        ])
        out.agent = lambda _body: next(moves)
        async with client() as c:
            out.asr_text = "How many businesses do we have?"
            await post_tg(c, tg_update(1, 9001, voice={"file_id": "VOICE1", "duration": 4}))
            assert out.tg_bodies("9001") == [
                "The platform has 1 business and 0 bookings.\n\nType HELP anytime to see what I can do."]
            trace = await pool.fetchrow(
                "SELECT message_kind, user_text FROM conversation_trace WHERE message_id='tg:1'")
            assert trace["message_kind"] == "audio"
            assert trace["user_text"] == "How many businesses do we have?"
            assert await pool.fetchval("SELECT outcome FROM agent_turns WHERE message_id='tg:1'") == "platform_agent"
            assert await pool.fetchval("SELECT count(*) FROM conversation_trace_replies WHERE message_id='tg:1'") == 1

            out.asr_text = "del Zuri Cuts YES"
            await post_tg(c, tg_update(2, 9001, voice={"file_id": "VOICE2", "duration": 4}))
            assert "cannot delete" in out.tg_bodies("9001")[-1].lower()
            assert await pool.fetchval("SELECT count(*) FROM salons") == 1
            assert await pool.fetchval("SELECT active FROM salons WHERE id=$1::uuid", salon) is True
        await pool.close()
    asyncio.run(run())


async def two_salons(dsn):
    pool, out, salon = await build(dsn, platform_owner_id=P)
    await pool.execute("INSERT INTO salons (name, wa_phone_number_id, slug) VALUES ('Malaika', $1, 'malaika')", PID)
    return pool, out, salon


@needs_db
def test_strangers_cannot_use_the_hub():
    async def run():
        pool, out, _salon = await build(DSN, platform_owner_id=P)
        async with client() as c:
            await post(c, payload(A, "shops", mid="h1"))
            assert "Businesses:" not in out.bodies(A)[-1]
        await pool.close()
    asyncio.run(run())


@needs_db
def test_shops_and_stats_list_everything():
    async def run():
        pool, out, _salon = await two_salons(DSN)
        async with client() as c:
            await post_tg(c, tg_update(1, 9001, "shops"))
            body = out.tg_bodies("9001")[-1]
            assert "Zuri Cuts" in body and "Malaika" in body
            await post_tg(c, tg_update(2, 9001, "stats"))
            assert "Businesses: 2" in out.tg_bodies("9001")[-1]
        await pool.close()
    asyncio.run(run())


@needs_db
def test_off_and_on_close_and_reopen():
    async def run():
        pool, out, _salon = await two_salons(DSN)
        async with client() as c:
            await post_tg(c, tg_update(1, 9001, "off malaika"))
            assert "closed" in out.tg_bodies("9001")[-1]
            assert await pool.fetchval("SELECT active FROM salons WHERE slug='malaika'") is False
            await post_tg(c, tg_update(2, 9001, "on malaika"))
            assert await pool.fetchval("SELECT active FROM salons WHERE slug='malaika'") is True
        await pool.close()
    asyncio.run(run())


@needs_db
def test_delete_asks_first_then_removes_everything():
    async def run():
        pool, out, salon = await build(DSN, platform_owner_id=P)
        async with client() as c:
            await post_tg(c, tg_update(1, 9001, "del Zuri Cuts"))
            assert "YES" in out.tg_bodies("9001")[-1]
            assert await pool.fetchval("SELECT count(*) FROM salons") == 1
            await post_tg(c, tg_update(2, 9001, "del Zuri Cuts YES"))
            assert "Deleted Zuri Cuts" in out.tg_bodies("9001")[-1]
            assert await pool.fetchval("SELECT count(*) FROM salons") == 0
            assert await pool.fetchval("SELECT count(*) FROM services") == 0
            assert await pool.fetchval("SELECT count(*) FROM bookings") == 0
        await pool.close()
    asyncio.run(run())


@needs_db
def test_similar_business_names_require_an_exact_target_and_bound_confirmation():
    async def run():
        pool, out, target = await build(DSN, platform_owner_id=P)
        await pool.execute("UPDATE salons SET name='Brian Cuts', slug='brian-cuts'")
        other = await pool.fetchval(
            "INSERT INTO salons (name, slug, tg_bot_id) VALUES ('Brian Cuts Studio', 'brian-cuts-studio', 'tg:112') RETURNING id::text")
        async with client() as c:
            await post_tg(c, tg_update(1, 9001, "del Brian Cuts YES"))
            assert "No matching deletion is awaiting confirmation" in out.tg_bodies("9001")[-1]
            assert await pool.fetchval("SELECT count(*) FROM salons") == 2

            await post_tg(c, tg_update(2, 9001, "del brian"))
            assert "No exact business match" in out.tg_bodies("9001")[-1]
            assert await pool.fetchval("SELECT count(*) FROM salons") == 2

            await post_tg(c, tg_update(3, 9001, "del Brian Cuts"))
            assert "Review: Brian Cuts" in out.tg_bodies("9001")[-1]
            await post_tg(c, tg_update(4, 9001, "del Brian Cuts Studio YES"))
            assert "No matching deletion is awaiting confirmation" in out.tg_bodies("9001")[-1]
            assert await pool.fetchval("SELECT count(*) FROM salons") == 2

            await post_tg(c, tg_update(5, 9001, "del Brian Cuts"))
            await post_tg(c, tg_update(6, 9001, "del brian-cuts YES"))
            assert "Deleted Brian Cuts" in out.tg_bodies("9001")[-1]
            assert await pool.fetchval("SELECT count(*) FROM salons") == 1
            assert await pool.fetchval("SELECT id::text FROM salons") == other
            assert await pool.fetchval("SELECT count(*) FROM services WHERE salon_id=$1::uuid", target) == 0
        await pool.close()
    asyncio.run(run())


@needs_db
def test_warn_sends_the_notice_now():
    async def run():
        pool, out, salon = await build(DSN, platform_owner_id=P)
        await pool.execute("UPDATE salons SET owner_wa_id=$1", P)
        async with client() as c:
            await post_tg(c, tg_update(1, 9001, "warn Zuri Cuts"))
            body = out.tg_bodies("9001")
            assert "Warning sent" in body[-1]
            warned = await pool.fetchval("SELECT inactivity_warned_at IS NOT NULL FROM salons")
            assert warned
        await pool.close()
    asyncio.run(run())


@needs_db
def test_sweep_warns_twice_then_deletes_and_resets_on_activity():
    async def run():
        from app.main import app
        pool, out, salon = await build(DSN, platform_owner_id=P)
        await pool.execute("UPDATE salons SET owner_wa_id=$1", P)
        cid = await pool.fetchval("INSERT INTO clients (salon_id, wa_id) VALUES ($1::uuid, 'x') RETURNING id::text", salon)
        sid = await pool.fetchval("SELECT id::text FROM services LIMIT 1")
        stid = await pool.fetchval("SELECT id::text FROM staff LIMIT 1")
        await pool.execute(
            "INSERT INTO bookings (salon_id, client_id, staff_id, service_id, starts_at, ends_at, status, created_at) "
            "VALUES ($1::uuid, $2::uuid, $3::uuid, $4::uuid, now() + interval '2 days', now() + interval '2 days' + interval '1 hour', 'confirmed', now() - interval '8 days')",
            salon, cid, stid, sid)
        ctx = app.state.ctx
        assert await lifecycle.sweep(ctx) == {"warned": [salon], "final": [], "deleted": [], "reset": []}
        assert "7 days" in out.tg_bodies("9001")[-1]
        await pool.execute("UPDATE bookings SET created_at = now() - interval '13 days 12 hours'")
        assert await lifecycle.sweep(ctx) == {"warned": [], "final": [salon], "deleted": [], "reset": []}
        assert "Last notice" in out.tg_bodies("9001")[-1]
        await pool.execute("UPDATE bookings SET created_at = now() - interval '15 days'")
        assert await lifecycle.sweep(ctx) == {"warned": [], "final": [], "deleted": [salon], "reset": []}
        assert "deleted" in out.tg_bodies("9001")[-1].lower()
        assert await pool.fetchval("SELECT count(*) FROM salons") == 0
        assert await pool.fetchval("SELECT count(*) FROM bookings") == 0
        await pool.close()
    asyncio.run(run())


@needs_db
def test_new_booking_cancels_the_countdown():
    async def run():
        from app.main import app
        pool, out, salon = await build(DSN, platform_owner_id=P)
        await pool.execute("UPDATE salons SET owner_wa_id=$1, inactivity_warned_at=now() - interval '1 day'", P)
        cid = await pool.fetchval("INSERT INTO clients (salon_id, wa_id) VALUES ($1::uuid, 'y') RETURNING id::text", salon)
        sid = await pool.fetchval("SELECT id::text FROM services LIMIT 1")
        stid = await pool.fetchval("SELECT id::text FROM staff LIMIT 1")
        await pool.execute(
            "INSERT INTO bookings (salon_id, client_id, staff_id, service_id, starts_at, ends_at, status) "
            "VALUES ($1::uuid, $2::uuid, $3::uuid, $4::uuid, now() + interval '2 days', now() + interval '2 days' + interval '1 hour', 'confirmed')",
            salon, cid, stid, sid)
        assert await lifecycle.sweep(app.state.ctx) == {"warned": [], "final": [], "deleted": [], "reset": [salon]}
        assert await pool.fetchval("SELECT inactivity_warned_at FROM salons") is None
        await pool.close()
    asyncio.run(run())


@needs_db
def test_owner_keep_cancels_the_countdown():
    async def run():
        pool, out, salon = await build(DSN, platform_owner_id=P)
        await pool.execute("UPDATE salons SET owner_wa_id=$1, inactivity_warned_at=now() - interval '1 day', inactivity_final_at=now() - interval '1 hour'", A)
        async with client() as c:
            await post(c, payload(A, "KEEP", mid="k1"))
            assert "Kept" in out.bodies(A)[-1]
            row = await pool.fetchrow("SELECT inactivity_warned_at, inactivity_final_at FROM salons")
            assert row["inactivity_warned_at"] is None and row["inactivity_final_at"] is None
        await pool.close()
    asyncio.run(run())
