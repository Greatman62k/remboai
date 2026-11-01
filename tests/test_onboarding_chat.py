"""Chat onboarding and salon binding: real Postgres, validated flows, and Telegram fakes at the HTTP layer."""
import asyncio
import os

import pytest

from app import binding
from app.onboarding_chat import (
    apply_corrections,
    apply_durations,
    default_minutes,
    detect_lang,
    examples,
    find_service,
    looks_off_topic,
    parse_deposit,
    parse_owners,
    parse_price,
    parse_service_line,
    parse_time_pair,
    redirect_step,
    wants_keep,
    wants_register,
)
from app.channels.telegram import parse_update
from tests.harness import build, client, post_tg, tg_update

DSN = os.environ.get("TEST_DATABASE_URL")
needs_db = pytest.mark.skipif(not DSN, reason="needs TEST_DATABASE_URL")
BOT = "tg:111"
OWNER_CHAT, CUST_CHAT = 7001, 5001


def test_slugify_and_service_and_time_parsing_are_deterministic():
    assert binding.slugify("Amina's Braids!") == "amina-s-braids"
    assert binding.slugify("  ") == "salon"
    assert parse_service_line("Fade 500 30") == {"name": "Fade", "price": 500, "minutes": 30, "explicit": True}
    assert parse_service_line("Silk press, 2,500 1h") == {"name": "Silk press", "price": 2500, "minutes": 60, "explicit": True}
    assert parse_service_line("Braids KES 1500") == {"name": "Braids", "price": 1500, "minutes": 180, "explicit": False}
    assert parse_service_line("Tea 20") is None
    assert parse_service_line("Fade lots") is None
    assert parse_service_line("x") is None
    assert parse_price("2,500") == 2500
    assert parse_price("10") is None
    assert parse_time_pair("09:00-18:00")[0].strftime("%H:%M") == "09:00"
    assert parse_time_pair("9am-6pm")[1].strftime("%H:%M") == "18:00"
    assert parse_time_pair("18:00-09:00") is None
    assert parse_time_pair("sometime") is None


def test_mangled_pairs_are_rejected_and_bare_evenings_roll():
    from datetime import time
    assert parse_time_pair("0900-1800") == (time(9), time(18))
    assert parse_time_pair("0900 to 1800") == (time(9), time(18))
    assert parse_time_pair("800-1900") is None
    assert parse_time_pair("2500-1800") is None
    assert parse_time_pair("9:3-17") is None
    assert parse_time_pair("25:00-9:99") is None
    assert parse_time_pair("open 9-17") is None
    assert parse_time_pair("9-5") == (time(9), time(17))
    assert parse_time_pair("12-8") == (time(12), time(20))
    assert parse_time_pair("10-2") == (time(10), time(14))
    assert parse_time_pair("2-11") is None
    assert parse_time_pair("7-7") is None
    assert parse_time_pair("09:00-18:00") == (time(9), time(18))
    assert parse_time_pair("9am-5pm") == (time(9), time(17))
    assert default_minutes("Skin fade") == 30
    assert default_minutes("Knotless braids") == 180
    assert default_minutes("Something new") == 45
    assert detect_lang("habari, nataka kujiandikisha") == "sw"
    assert detect_lang("hello, I want to register") == "en"


def test_off_topic_steering_and_service_matching_are_deterministic():
    assert looks_off_topic("Ok") and looks_off_topic("habari")
    assert looks_off_topic("perfect") and looks_off_topic("thank you")
    assert looks_off_topic("sawa") and looks_off_topic("shukran") and looks_off_topic("karibu")
    assert looks_off_topic("What time do you open?")
    assert looks_off_topic("nataka fade kesho")
    assert looks_off_topic("sorry I made a mistake, the name is wrong")
    assert looks_off_topic("")
    assert not looks_off_topic("Amina Braids")
    assert not looks_off_topic("Huduma Cuts")
    assert not looks_off_topic("Al")
    services = [{"name": "Fade", "price": 500, "minutes": 30}, {"name": "Braids", "price": 1500, "minutes": 120}]
    assert find_service(services, "Fade")["price"] == 500
    assert find_service(services, "actually make Fade 600")["price"] == 500
    assert find_service(services, "Wash") is None
    data = {"services": [dict(services[0])]}
    updated, added, bad = apply_corrections(data, "Fade 600")
    assert updated == ["Fade: 600 (30 min)"] and added == [] and bad == []
    updated, added, bad = apply_corrections(data, "Fade 600 45\nWash 300\nlots of money")
    assert updated == ["Fade: 600 (45 min)"] and [a["name"] for a in added] == ["Wash"] and bad == ["lots of money"]


def test_owners_kinds_and_change_requests_parse():
    assert parse_owners("Amina") == ["Amina"]
    assert parse_owners("Brian, Kevin\nAmina") == ["Brian", "Kevin", "Amina"]
    assert parse_owners("Amina, amina , BRIAN") == ["Amina", "BRIAN"]
    assert parse_owners("x") is None and parse_owners("") is None
    assert examples("kinyozi")["prices"].startswith("Fade") and "Braids" not in examples("kinyozi")["prices"]
    assert examples("salon")["prices"].startswith("Braids") and "Fade" not in examples("salon")["prices"]
    assert examples(None)["fix"] == "Braids 1600"
    assert redirect_step("i want to change the time") == "ask_durations"
    assert redirect_step("change the price please") == "ask_prices"
    assert redirect_step("badilisha muda wa kufungua") == "ask_hours"
    assert redirect_step("the name is wrong") == "ask_name"
    assert redirect_step("Fade 600") is None and redirect_step("YES") is None
    assert wants_keep("they are ok", False) and wants_keep("keep them", False)
    assert wants_keep("all good", False) and wants_keep("no change", False)
    assert wants_keep("Fade 600", False) is False and wants_keep("Amina", True) is False
    assert wants_keep("ok", False) is False and wants_keep("ok", True) is True
    assert wants_keep("sawa", True) is True
    assert wants_register("i want to register a business")
    assert wants_register("nataka kusajili saluni yangu")
    assert wants_register("naomba kufungua kinyozi")
    assert wants_register("how do i set up my business")
    assert wants_register("i want to sign up my salon")
    assert wants_register("how do i join as a salon")
    assert not wants_register("i want to register a complaint")
    assert not wants_register("book a salon for tomorrow")
    assert not wants_register("what are your opening hours")
    assert not wants_register("how do i set up an appointment")
    assert not wants_register("Fade 500 30")


def test_deposit_amounts_parse_with_a_plain_no_for_free():
    assert parse_deposit("200") == 200
    assert parse_deposit("1,500") == 1500
    assert parse_deposit("KES 200 per booking") == 200
    assert parse_deposit("no") == 0
    assert parse_deposit("no deposit") == 0
    assert parse_deposit("no 200") is None
    assert parse_deposit("none 200") is None
    assert parse_deposit("bure") == 0
    assert parse_deposit("") is None
    assert parse_deposit("much") is None
    assert parse_deposit("99999999") is None
    assert redirect_step("change the deposit please") == "ask_deposit"


def test_bare_times_belong_to_the_only_service():
    one = [{"name": "Fade", "price": 500, "minutes": 30}]
    assert apply_durations([dict(one[0])], "45") == (1, [])
    svc = dict(one[0])
    assert apply_durations([svc], "2h") == (1, []) and svc["minutes"] == 120
    assert apply_durations([dict(one[0])], "500") == (0, ["500"])
    two = [dict(one[0]), {"name": "Braids", "price": 1500, "minutes": 120}]
    assert apply_durations([dict(s) for s in two], "45") == (0, ["45"])
    assert apply_durations([dict(s) for s in two], "Fade 45") == (1, [])


def test_start_deep_link_survives_telegram_parsing():
    m, _ = parse_update(tg_update(1, CUST_CHAT, "/start malaika"), BOT)
    assert m.text == "/start malaika"
    assert parse_update(tg_update(2, CUST_CHAT, "/start"), BOT)[0].text == "hi"


@needs_db
def test_slugs_are_stable_unique_and_reserved():
    async def run():
        pool, out, _seed = await build(DSN)
        first = await pool.fetchval("INSERT INTO salons (name, tg_bot_id) VALUES ('Malaika', 'tg:111') RETURNING id::text")
        assert await binding.ensure_slug(pool, first, "Malaika") == "malaika"
        assert await binding.ensure_slug(pool, first, "Renamed") == "malaika"
        second = await pool.fetchval("INSERT INTO salons (name, tg_bot_id) VALUES ('Malaika', 'tg:111') RETURNING id::text")
        assert await binding.ensure_slug(pool, second, "Malaika") == "malaika-2"
        reg = await pool.fetchval("INSERT INTO salons (name, tg_bot_id) VALUES ('Register', 'tg:111') RETURNING id::text")
        assert await binding.ensure_slug(pool, reg, "Register") == "register-salon"
        assert await binding.bind_sender(pool, "tg:1", "no-such-salon") is None
        assert await binding.bind_sender(pool, "tg:1", "MALAIKA") == first
        assert await binding.bound_salon(pool, "tg:1") == first
        assert await binding.bind_sender(pool, "tg:1", "malaika-2") == second
        assert await binding.bound_salon(pool, "tg:1") == second
        assert await binding.bound_salon(pool, "tg:nobody") is None
        await pool.close()
    asyncio.run(run())


@needs_db
def test_registration_is_limited_per_sender_per_day():
    async def run():
        pool, out, _seed = await build(DSN)
        assert [await binding.registration_allowed(pool, "tg:9") for _ in range(3)] == [True, True, True]
        assert await binding.registration_allowed(pool, "tg:9") is False
        assert await binding.registration_allowed(pool, "tg:other") is True
        await pool.close()
    asyncio.run(run())


@needs_db
def test_chat_onboarding_registers_a_salon_with_no_model_calls():
    async def run():
        pool, out, seed = await build(DSN)
        sender = f"tg:{OWNER_CHAT}"
        async with client() as c:
            await post_tg(c, tg_update(1, OWNER_CHAT, "/start register"))
            assert "salon name" in out.tg_bodies(str(OWNER_CHAT))[-1].lower()
            await post_tg(c, tg_update(2, OWNER_CHAT, "Zuri Cuts"))
            assert "different" in out.tg_bodies(str(OWNER_CHAT))[-1].lower()
            await post_tg(c, tg_update(3, OWNER_CHAT, "NO"))
            assert "salon name" in out.tg_bodies(str(OWNER_CHAT))[-1].lower()
            await post_tg(c, tg_update(4, OWNER_CHAT, "Amina Braids"))
            assert "barbershop" in out.tg_bodies(str(OWNER_CHAT))[-1].lower()
            await post_tg(c, tg_update(5, OWNER_CHAT, "2"))
            assert "your name" in out.tg_bodies(str(OWNER_CHAT))[-1].lower()
            await post_tg(c, tg_update(6, OWNER_CHAT, "Amina"))
            assert "one service per line" in out.tg_bodies(str(OWNER_CHAT))[-1].lower()
            await post_tg(c, tg_update(7, OWNER_CHAT, "Fade 500\nBraids 1500 180"))
            body = out.tg_bodies(str(OWNER_CHAT))[-1]
            assert "Fade" in body and "1500" in body and "YES" in body and "(~" in body
            await post_tg(c, tg_update(8, OWNER_CHAT, "YES"))
            assert "how long" in out.tg_bodies(str(OWNER_CHAT))[-1].lower()
            await post_tg(c, tg_update(9, OWNER_CHAT, "YES"))
            assert "opening hours" in out.tg_bodies(str(OWNER_CHAT))[-1].lower()
            await post_tg(c, tg_update(10, OWNER_CHAT, "09:00-18:00"))
            assert "deposit" in out.tg_bodies(str(OWNER_CHAT))[-1].lower()
            await post_tg(c, tg_update(11, OWNER_CHAT, "200"))
            assert "ready to open" in out.tg_bodies(str(OWNER_CHAT))[-1].lower()
            assert "KES 200" in out.tg_bodies(str(OWNER_CHAT))[-1]
            await post_tg(c, tg_update(12, OWNER_CHAT, "YES"))
            live = out.tg_bodies(str(OWNER_CHAT))[-1]
            assert "is live" in live.lower() and "amina-braids" in live
        assert out.calls == []
        row = await pool.fetchrow("SELECT name, owner_wa_id, tg_bot_id, slug, open_hours, deposit_kes FROM salons WHERE slug='amina-braids'")
        assert row["deposit_kes"] == 200
        assert row is not None and row["owner_wa_id"] == sender and row["tg_bot_id"] == BOT
        import json
        hours = row["open_hours"] if isinstance(row["open_hours"], dict) else json.loads(row["open_hours"])
        assert hours["mon"] == ["09:00", "18:00"] and hours["sun"] == ["09:00", "18:00"]
        assert await pool.fetchval("SELECT name FROM staff WHERE salon_id=(SELECT id FROM salons WHERE slug='amina-braids')") == "Amina"
        services = await pool.fetch("SELECT name, price_kes, duration_min FROM services WHERE salon_id=(SELECT id FROM salons WHERE slug='amina-braids') ORDER BY name")
        assert [(s["name"], s["price_kes"], s["duration_min"]) for s in services] == [("Braids", 1500, 180), ("Fade", 500, 30)]
        assert await pool.fetchval("SELECT count(*) FROM onboarding_sessions WHERE sender=$1", sender) == 0
        await pool.close()
    asyncio.run(run())


@needs_db
def test_onboarding_twin_yes_branch_and_voice_price_list():
    async def run():
        from app.main import app
        pool, out, seed = await build(DSN)
        ctx = app.state.ctx
        from app import onboarding_chat
        sender = f"tg:{OWNER_CHAT}"
        assert await onboarding_chat.begin(ctx, sender, BOT, "/start register") is True
        await onboarding_chat.advance(ctx, sender, BOT, "Zuri Cuts")
        assert "different" in out.tg_bodies(str(OWNER_CHAT))[-1].lower()
        await onboarding_chat.advance(ctx, sender, BOT, "YES")
        assert "barbershop" in out.tg_bodies(str(OWNER_CHAT))[-1].lower()
        await onboarding_chat.advance(ctx, sender, BOT, "kinyozi")
        assert "your name" in out.tg_bodies(str(OWNER_CHAT))[-1].lower()
        await onboarding_chat.advance(ctx, sender, BOT, "Amina")
        out.asr_text = "Fade 500 30"
        async with client() as c:
            await post_tg(c, tg_update(50, OWNER_CHAT, voice={"file_id": "VOICE1", "duration": 3}))
        bodies = out.tg_bodies(str(OWNER_CHAT))
        assert any("500" in b for b in bodies[-2:])
        await pool.close()
    asyncio.run(run())


@needs_db
def test_affirmations_are_never_stored_as_names():
    async def run():
        from app.main import app
        pool, out, seed = await build(DSN)
        ctx = app.state.ctx
        from app import onboarding_chat
        sender, chat = "tg:8002", "8002"
        await onboarding_chat.begin(ctx, sender, BOT, "/start register")
        await onboarding_chat.advance(ctx, sender, BOT, "yes")
        assert "salon name" in out.tg_bodies(chat)[-1].lower()
        await onboarding_chat.advance(ctx, sender, BOT, "Real Salon")
        await onboarding_chat.advance(ctx, sender, BOT, "1")
        await onboarding_chat.advance(ctx, sender, BOT, "ok")
        assert "stylist" in out.tg_bodies(chat)[-1].lower()
        await pool.close()
    asyncio.run(run())


@needs_db
def test_greetings_are_not_stored_as_stylist_names():
    async def run():
        from app.main import app
        pool, out, seed = await build(DSN)
        ctx = app.state.ctx
        from app import onboarding_chat
        sender, chat = "tg:8005", "8005"
        await onboarding_chat.begin(ctx, sender, BOT, "/start register")
        await onboarding_chat.advance(ctx, sender, BOT, "Greet Salon")
        await onboarding_chat.advance(ctx, sender, BOT, "1")
        await onboarding_chat.advance(ctx, sender, BOT, "hello")
        assert "stylist" in out.tg_bodies(chat)[-1].lower()
        await pool.close()
    asyncio.run(run())


@needs_db
def test_yes_after_back_keeps_the_listed_prices():
    async def run():
        from app.main import app
        pool, out, seed = await build(DSN)
        ctx = app.state.ctx
        from app import onboarding_chat
        sender, chat = "tg:8003", "8003"
        await onboarding_chat.begin(ctx, sender, BOT, "/start register")
        await onboarding_chat.advance(ctx, sender, BOT, "Steady Cuts")
        await onboarding_chat.advance(ctx, sender, BOT, "1")
        await onboarding_chat.advance(ctx, sender, BOT, "Amina")
        await onboarding_chat.advance(ctx, sender, BOT, "Fade 500 30")
        await onboarding_chat.advance(ctx, sender, BOT, "BACK")
        await onboarding_chat.advance(ctx, sender, BOT, "YES")
        assert "Fade: 500" in out.tg_bodies(chat)[-1]
        await pool.close()
    asyncio.run(run())


@needs_db
def test_dropped_duration_lines_are_shown_not_silently_lost():
    async def run():
        from app.main import app
        pool, out, seed = await build(DSN)
        ctx = app.state.ctx
        from app import onboarding_chat
        sender, chat = "tg:8004", "8004"
        await onboarding_chat.begin(ctx, sender, BOT, "/start register")
        await onboarding_chat.advance(ctx, sender, BOT, "Time Salon")
        await onboarding_chat.advance(ctx, sender, BOT, "1")
        await onboarding_chat.advance(ctx, sender, BOT, "Amina")
        await onboarding_chat.advance(ctx, sender, BOT, "Fade 500")
        await onboarding_chat.advance(ctx, sender, BOT, "YES")
        await onboarding_chat.advance(ctx, sender, BOT, "Fade 30\nxyz")
        body = out.tg_bodies(chat)[-1]
        assert "xyz" in body and "opening hours" in body.lower()
        await pool.close()
    asyncio.run(run())


@needs_db
def test_price_board_photos_get_a_typed_list_answer():
    async def run():
        from app.main import app
        pool, out, seed = await build(DSN)
        ctx = app.state.ctx
        from app import onboarding_chat
        sender, chat = "tg:8001", "8001"
        await onboarding_chat.begin(ctx, sender, BOT, "/start register")
        await onboarding_chat.advance(ctx, sender, BOT, "Photo Salon")
        await onboarding_chat.advance(ctx, sender, BOT, "1")
        await onboarding_chat.advance(ctx, sender, BOT, "Amina")
        await onboarding_chat.advance(ctx, sender, BOT, "see the board", {"kind": "image"})
        assert "type the list" in out.tg_bodies(chat)[-1].lower()
        await pool.close()
    asyncio.run(run())


@needs_db
def test_interruptions_back_and_corrections_stay_in_setup():
    async def run():
        from app.main import app
        pool, out, seed = await build(DSN)
        ctx = app.state.ctx
        from app import onboarding_chat
        sender, chat = f"tg:{OWNER_CHAT}", str(OWNER_CHAT)
        await onboarding_chat.begin(ctx, sender, BOT, "/start register")
        await onboarding_chat.advance(ctx, sender, BOT, "Ok")
        assert "continue with the setup" in out.tg_bodies(chat)[-1].lower()
        assert "salon name" in out.tg_bodies(chat)[-1].lower()
        await onboarding_chat.advance(ctx, sender, BOT, "Amina Cuts")
        assert "barbershop" in out.tg_bodies(chat)[-1].lower()
        await onboarding_chat.advance(ctx, sender, BOT, "BACK")
        assert "salon name" in out.tg_bodies(chat)[-1].lower()
        await onboarding_chat.advance(ctx, sender, BOT, "Amina Cuts")
        await onboarding_chat.advance(ctx, sender, BOT, "2")
        await onboarding_chat.advance(ctx, sender, BOT, "nataka fade")
        assert "continue with the setup" in out.tg_bodies(chat)[-1].lower()
        await onboarding_chat.advance(ctx, sender, BOT, "Amina")
        await onboarding_chat.advance(ctx, sender, BOT, "Fade 500 30\nBraids 1500")
        await onboarding_chat.advance(ctx, sender, BOT, "Fade 600")
        assert "Updated: Fade: 600 (30 min)" in out.tg_bodies(chat)[-1]
        await onboarding_chat.advance(ctx, sender, BOT, "YES")
        await onboarding_chat.advance(ctx, sender, BOT, "45")
        assert "keep the estimates" in out.tg_bodies(chat)[-1].lower()
        await onboarding_chat.advance(ctx, sender, BOT, "Braids 3h")
        assert "opening hours" in out.tg_bodies(chat)[-1].lower()
        await onboarding_chat.advance(ctx, sender, BOT, "09:00-18:00")
        assert "deposit" in out.tg_bodies(chat)[-1].lower()
        await onboarding_chat.advance(ctx, sender, BOT, "0")
        summary = out.tg_bodies(chat)[-1]
        assert "Services:" in summary and "Fade: 600" in summary
        await onboarding_chat.advance(ctx, sender, BOT, "Braids 1600")
        assert "Updated: Braids: 1600 (180 min)" in out.tg_bodies(chat)[-1]
        await onboarding_chat.advance(ctx, sender, BOT, "YES")
        assert "is live" in out.tg_bodies(chat)[-1].lower()
        services = await pool.fetch("SELECT name, price_kes, duration_min FROM services WHERE salon_id=(SELECT id FROM salons WHERE slug='amina-cuts') ORDER BY name")
        assert [(s["name"], s["price_kes"], s["duration_min"]) for s in services] == [("Braids", 1600, 180), ("Fade", 600, 30)]
        await pool.close()
    asyncio.run(run())


@needs_db
def test_kinyozi_flow_many_stylists_and_plain_language_changes():
    async def run():
        from app.main import app
        pool, out, seed = await build(DSN)
        ctx = app.state.ctx
        from app import onboarding_chat
        sender, chat = f"tg:{OWNER_CHAT}", str(OWNER_CHAT)
        await onboarding_chat.begin(ctx, sender, BOT, "/start register")
        await onboarding_chat.advance(ctx, sender, BOT, "Kinyozi Fresh")
        await onboarding_chat.advance(ctx, sender, BOT, "maybe")
        assert "1 for barbershop" in out.tg_bodies(chat)[-1].lower()
        await onboarding_chat.advance(ctx, sender, BOT, "1")
        assert "Amina, Brian" in out.tg_bodies(chat)[-1]
        await onboarding_chat.advance(ctx, sender, BOT, "Brian, Kevin\nAmina")
        body = out.tg_bodies(chat)[-1]
        assert "one service per line" in body.lower() and "[service name]" in body
        await onboarding_chat.advance(ctx, sender, BOT, "Fade 500 30\nClean shave 300")
        assert "(~" in out.tg_bodies(chat)[-1]
        await onboarding_chat.advance(ctx, sender, BOT, "YES")
        assert "Fade: 30 min" in out.tg_bodies(chat)[-1]
        await onboarding_chat.advance(ctx, sender, BOT, "YES")
        await onboarding_chat.advance(ctx, sender, BOT, "08:00-20:00")
        assert "deposit" in out.tg_bodies(chat)[-1].lower()
        await onboarding_chat.advance(ctx, sender, BOT, "no deposit")
        assert "Stylists: Brian, Kevin, Amina" in out.tg_bodies(chat)[-1]
        await onboarding_chat.advance(ctx, sender, BOT, "i want to change the time")
        assert "How long" in out.tg_bodies(chat)[-1]
        await onboarding_chat.advance(ctx, sender, BOT, "Clean shave 1h")
        assert "Ready to open" in out.tg_bodies(chat)[-1]
        await onboarding_chat.advance(ctx, sender, BOT, "change the price please")
        assert "services you offer" in out.tg_bodies(chat)[-1].lower()
        await onboarding_chat.advance(ctx, sender, BOT, "Clean shave 350")
        assert "Here is what I have" in out.tg_bodies(chat)[-1]
        await onboarding_chat.advance(ctx, sender, BOT, "YES")
        assert "Ready to open" in out.tg_bodies(chat)[-1]
        assert "Clean shave: 350 (60 min)" in out.tg_bodies(chat)[-1]
        await onboarding_chat.advance(ctx, sender, BOT, "YES")
        assert "is live" in out.tg_bodies(chat)[-1].lower()
        assert await pool.fetchval("SELECT count(*) FROM staff WHERE salon_id=(SELECT id FROM salons WHERE slug='kinyozi-fresh')") == 3
        assert await pool.fetchval("SELECT count(*) FROM staff_services WHERE staff_id IN (SELECT id FROM staff WHERE salon_id=(SELECT id FROM salons WHERE slug='kinyozi-fresh'))") == 6
        services = await pool.fetch("SELECT name, price_kes, duration_min FROM services WHERE salon_id=(SELECT id FROM salons WHERE slug='kinyozi-fresh') ORDER BY name")
        assert [(s["name"], s["price_kes"], s["duration_min"]) for s in services] == [("Clean shave", 350, 60), ("Fade", 500, 30)]
        await pool.close()
    asyncio.run(run())


@needs_db
def test_keep_old_answer_after_a_stylist_redirect():
    async def run():
        from app.main import app
        pool, out, seed = await build(DSN)
        ctx = app.state.ctx
        from app import onboarding_chat
        sender, chat = f"tg:{OWNER_CHAT}", str(OWNER_CHAT)
        await onboarding_chat.begin(ctx, sender, BOT, "/start register")
        await onboarding_chat.advance(ctx, sender, BOT, "Juma Cuts")
        await onboarding_chat.advance(ctx, sender, BOT, "2")
        await onboarding_chat.advance(ctx, sender, BOT, "they are ok")
        assert "continue with the setup" in out.tg_bodies(chat)[-1].lower()
        await onboarding_chat.advance(ctx, sender, BOT, "Juma")
        await onboarding_chat.advance(ctx, sender, BOT, "Fade 500 30")
        await onboarding_chat.advance(ctx, sender, BOT, "YES")
        await onboarding_chat.advance(ctx, sender, BOT, "09:00-18:00")
        assert "deposit" in out.tg_bodies(chat)[-1].lower()
        await onboarding_chat.advance(ctx, sender, BOT, "0")
        assert "Stylists: Juma" in out.tg_bodies(chat)[-1]
        await onboarding_chat.advance(ctx, sender, BOT, "i want to change stylists")
        assert "your name" in out.tg_bodies(chat)[-1].lower()
        await onboarding_chat.advance(ctx, sender, BOT, "ok")
        assert "Ready to open" in out.tg_bodies(chat)[-1]
        assert "Stylists: Juma" in out.tg_bodies(chat)[-1]
        assert "they are ok" not in out.tg_bodies(chat)[-1].lower()
        await onboarding_chat.advance(ctx, sender, BOT, "YES")
        assert "is live" in out.tg_bodies(chat)[-1].lower()
        assert await pool.fetchval("SELECT name FROM staff WHERE salon_id=(SELECT id FROM salons WHERE slug='juma-cuts')") == "Juma"
        await pool.close()
    asyncio.run(run())


@needs_db
def test_plain_register_sentences_begin_onboarding_in_either_language():
    async def run():
        pool, out, seed = await build(DSN)
        async with client() as c:
            await post_tg(c, tg_update(1, 7001, "i want to register a business"))
            bodies = out.tg_bodies("7001")
            assert "Sure, I can help you with that" in bodies[0]
            assert "salon name" in bodies[-1].lower()
            assert await pool.fetchval("SELECT state->>'step' FROM onboarding_sessions WHERE sender='tg:7001'") == "ask_name"
            await post_tg(c, tg_update(2, 7002, "nataka kusajili saluni yangu"))
            bodies = out.tg_bodies("7002")
            assert "nitakusaidia" in bodies[0]
            assert "inaitwaje" in bodies[-1].lower()
        assert out.calls == []
        await pool.close()
    asyncio.run(run())


@needs_db
def test_unlinked_senders_pick_a_business_while_links_and_owners_route_directly():
    async def run():
        pool, out, seed = await build(DSN)
        new = await pool.fetchval("INSERT INTO salons (name, tg_bot_id) VALUES ('Malaika', 'tg:111') RETURNING id::text")
        await binding.ensure_slug(pool, new, "Malaika")
        await pool.execute("UPDATE salons SET owner_wa_id='tg:7003' WHERE id=$1::uuid", new)
        async with client() as c:
            await post_tg(c, tg_update(1, 5001, "hi"))
            picker = out.tg_bodies("5001")[-1]
            assert "Zuri Cuts" in picker and "Malaika" in picker
            assert "t.me/rembo_test_bot?start=malaika" in picker
            assert "Fade Saturday" not in picker
            assert await pool.fetchval("SELECT count(*) FROM sender_salon WHERE sender='tg:5001'") == 0
            assert await pool.fetchval("SELECT count(*) FROM clients WHERE wa_id='tg:5001'") == 0
            await post_tg(c, tg_update(2, 5001, "2"))
            assert "Malaika" in out.tg_bodies("5001")[-1]
            assert await pool.fetchval("SELECT salon_id::text FROM sender_salon WHERE sender='tg:5001'") == new
            await post_tg(c, tg_update(3, 7003, "hi"))
            assert "Malaika" in out.tg_bodies("7003")[-1]
            assert await pool.fetchval("SELECT count(*) FROM sender_salon WHERE sender='tg:7003'") == 0
        assert out.calls == []
        await pool.close()
    asyncio.run(run())


@needs_db
def test_whatsapp_picker_links_carry_the_slug_prefilled():
    async def run():
        from tests.harness import payload, post
        pool, out, seed = await build(DSN, central_whatsapp_number="254700111222")
        new = await pool.fetchval("INSERT INTO salons (name, wa_phone_number_id) VALUES ('Malaika', 'PID1') RETURNING id::text")
        await binding.ensure_slug(pool, new, "Malaika")
        async with client() as c:
            await post(c, payload("254722000002", "hi", mid="w1"))
            picker = out.bodies("254722000002")[-1]
            assert "Zuri Cuts" in picker and "Malaika" in picker
            assert "https://wa.me/254700111222?text=/start%20malaika" in picker
            await post(c, payload("254722000002", "malaika", mid="w2"))
            assert "Malaika" in out.bodies("254722000002")[-1]
            assert await pool.fetchval("SELECT salon_id::text FROM sender_salon WHERE sender='254722000002'") == new
        assert out.calls == []
        await pool.close()
    asyncio.run(run())


@needs_db
def test_id_command_answers_owners_and_guides_customers():
    async def run():
        pool, out, seed = await build(DSN)
        async with client() as c:
            await post_tg(c, tg_update(1, 7001, "/id"))
            body = out.tg_bodies("7001")[-1]
            assert "tg:7001" not in body and "Zuri Cuts" in body
        await pool.execute("UPDATE salons SET owner_wa_id='tg:7001'")
        async with client() as c:
            await post_tg(c, tg_update(2, 7001, "/id"))
            assert "tg:7001" in out.tg_bodies("7001")[-1]
        await pool.close()
    asyncio.run(run())


@needs_db
def test_deep_link_binds_and_routes_while_unknown_links_fall_back():
    async def run():
        pool, out, seed = await build(DSN)
        new = await pool.fetchval("INSERT INTO salons (name, tg_bot_id) VALUES ('Malaika', $1) RETURNING id::text", BOT)
        await binding.ensure_slug(pool, new, "Malaika")
        async with client() as c:
            await post_tg(c, tg_update(1, CUST_CHAT, "/start malaika"))
            assert await pool.fetchval("SELECT salon_id::text FROM sender_salon WHERE sender=$1", f"tg:{CUST_CHAT}") == new
            assert "malaika" in out.tg_bodies(str(CUST_CHAT))[-1].lower()
            await post_tg(c, tg_update(2, 6001, "/start no-such-salon"))
            assert await pool.fetchval("SELECT count(*) FROM sender_salon WHERE sender='tg:6001'") == 0
            assert "zuri cuts" in out.tg_bodies("6001")[-1].lower()
        assert out.calls == []
        await pool.close()
    asyncio.run(run())
