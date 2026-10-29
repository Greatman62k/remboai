"""Telegram as a second channel: same engine, same rules. Real app and Postgres; Telegram, Safaricom and the model are faked at the HTTP layer."""
import asyncio
import os

import pytest

from app.telegram import Channels, parse_update, run_poller
from tests.harness import CB_SECRET, build, callback, client, open_day, post_tg, tg_update

DSN = os.environ.get("TEST_DATABASE_URL")
needs_db = pytest.mark.skipif(not DSN, reason="needs TEST_DATABASE_URL")
BOT = "tg:111"
C1, C2, OWNER = 5001, 5002, 7001


def test_parsing_text_start_button_voice_photo_contact_and_groups():
    m, cb = parse_update(tg_update(1, C1, "Fade kesho"), BOT)
    assert (m.kind, m.sender, m.text, m.phone_number_id, m.id, cb) == ("text", f"tg:{C1}", "Fade kesho", BOT, "tg:1", None)
    assert parse_update(tg_update(2, C1, "/start"), BOT)[0].text == "hi"
    assert parse_update(tg_update(3, C1, "/start@rembo_bot"), BOT)[0].text == "hi"
    m, cb = parse_update(tg_update(4, C1, data="slot:0"), BOT)
    assert (m.kind, m.reply_id, m.text, cb) == ("interactive", "slot:0", "slot:0", "cb4")      # no label on the message: the id stands in
    tapped = tg_update(4, C1, data="slot:1", reply_markup={"inline_keyboard": [[{"text": "Thu 12:30", "callback_data": "slot:1"}]]})
    assert parse_update(tapped, BOT)[0].text == "Thu 12:30"
    m, _ = parse_update(tg_update(5, C1, voice={"file_id": "VOICE1", "duration": 4}), BOT)
    assert (m.kind, m.media_id) == ("audio", "tg:VOICE1")
    m, _ = parse_update(tg_update(6, C1, caption="like this", photo=[{"file_id": "PHOTO_S", "file_size": 10}, {"file_id": "PHOTO_L", "file_size": 900}]), BOT)
    assert (m.kind, m.media_id, m.text) == ("image", "tg:PHOTO_L", "like this")           # the biggest size, with its caption
    m, _ = parse_update(tg_update(7, C1, contact={"phone_number": "+254712345678"}), BOT)
    assert (m.kind, m.text) == ("text", "+254712345678")
    group = tg_update(8, -100123, "hello")
    group["message"]["chat"]["type"] = "group"
    assert parse_update(group, BOT) == (None, None)                                         # private chats only
    assert parse_update({"update_id": 9}, BOT) == (None, None)


@needs_db
def test_a_telegram_customer_can_book_through_text_and_buttons():
    async def run():
        pool, out, salon = await build(DSN)
        await pool.execute("DELETE FROM salon_payment")                                      # no till: confirm straight away
        out.script["fade"] = {"intent": "book", "service": "Fade", "date": open_day().isoformat(), "window": "afternoon"}
        async with client() as c:
            assert (await post_tg(c, tg_update(1, C1, "fade"))).json() == {"ok": True}
            offer = out.tg_to(str(C1))[-1]
            assert "earliest" in offer["text"].lower() and "Fade" in offer["text"]
            buttons = [b for row in offer["reply_markup"]["inline_keyboard"] for b in row]
            assert buttons[0]["callback_data"] == "slot:0"
            await post_tg(c, tg_update(2, C1, data="slot:0"))
        assert any(m["method"] == "answerCallbackQuery" and m["callback_query_id"] == "cb2" for m in out.tg)   # the button spinner stops
        assert "booked" in out.tg_bodies(str(C1))[-1].lower() and "Fade" in out.tg_bodies(str(C1))[-1]
        assert await pool.fetchval("SELECT status FROM bookings") == "confirmed"
        assert await pool.fetchval("SELECT wa_id FROM clients") == f"tg:{C1}"
        assert out.sent == []                                                                # nothing went to WhatsApp
        await pool.close()
    asyncio.run(run())


@needs_db
def test_a_telegram_customer_is_asked_for_an_mpesa_number_then_pays_and_is_confirmed_on_telegram():
    async def run():
        pool, out, salon = await build(DSN)
        await pool.execute("UPDATE salons SET owner_wa_id=$1", f"tg:{OWNER}")
        out.script["fade"] = {"intent": "book", "service": "Fade", "date": open_day().isoformat(), "window": "afternoon"}
        async with client() as c:
            await post_tg(c, tg_update(1, C1, "fade"))
            await post_tg(c, tg_update(2, C1, data="slot:0"))
            assert out.stk == []                                                             # a Telegram id is not a phone number
            assert "m-pesa number" in out.tg_bodies(str(C1))[-1].lower()
            await post_tg(c, tg_update(3, C1, "0711 000 001"))
            assert len(out.stk) == 1 and out.stk[0]["PhoneNumber"] == "254711000001" and out.stk[0]["Amount"] == 200
            assert "M-Pesa request" in out.tg_bodies(str(C1))[-1]
            await c.post(f"/mpesa/callback/{CB_SECRET}", json=callback("ws_CO_1"))
        assert await pool.fetchval("SELECT status FROM bookings") == "confirmed"
        assert "200" in out.tg_bodies(str(C1))[-1] and "QAB1CD2EF3" in out.tg_bodies(str(C1))[-1]
        assert out.tg_bodies(str(OWNER))[-1].startswith("New booking:")                      # the owner hears on Telegram too
        assert out.sent == []
        await pool.close()
    asyncio.run(run())


@needs_db
def test_voice_notes_and_photos_work_over_telegram():
    async def run():
        pool, out, salon = await build(DSN)
        await pool.execute("DELETE FROM salon_payment")
        out.script["nataka fade kesho mchana"] = {"intent": "book", "service": "Fade", "date": open_day().isoformat(), "window": "afternoon", "language": "sw"}
        async with client() as c:
            await post_tg(c, tg_update(1, C1, voice={"file_id": "VOICE1", "duration": 3}))
            assert "nataka fade kesho mchana" in out.tg_bodies(str(C1))[0]
            assert out.asr[0]["bytes"] == b"TG-FILE-BYTES" and out.asr[0]["type"] == "audio/ogg"
            await post_tg(c, tg_update(2, C2, caption="I want this", photo=[{"file_id": "PHOTO1", "file_size": 500}]))
        assert any(v["image"].startswith("data:image/jpeg;base64,") for v in out.vision)
        assert "Fade" in " ".join(out.tg_bodies(str(C2)))
        await pool.close()
    asyncio.run(run())


@needs_db
def test_the_owner_commands_work_from_a_telegram_owner_and_a_customer_cannot_use_them():
    async def run():
        pool, out, salon = await build(DSN)
        await pool.execute("UPDATE salons SET owner_wa_id=$1", f"tg:{OWNER}")
        async with client() as c:
            await post_tg(c, tg_update(1, OWNER, "today"))
            await post_tg(c, tg_update(2, C1, "today"))
        assert out.tg_bodies(str(OWNER)) and "booking" in out.tg_bodies(str(OWNER))[0].lower()
        assert out.tg_bodies(str(OWNER))[0] != out.tg_bodies(str(C1))[0]                     # the customer got the customer flow, not the owner's list
        await pool.close()
    asyncio.run(run())


@needs_db
def test_id_command_tells_a_telegram_user_their_id_and_a_repeat_update_is_ignored():
    async def run():
        pool, out, salon = await build(DSN)
        async with client() as c:
            await post_tg(c, tg_update(1, OWNER, "/id"))
            await post_tg(c, tg_update(1, OWNER, "/id"))                                      # Telegram redelivers the same update_id
        assert out.tg_bodies(str(OWNER)) == [f"Your Telegram id: tg:{OWNER}"]
        await pool.close()
    asyncio.run(run())


@needs_db
def test_the_webhook_needs_the_secret_header_and_a_wrong_one_does_nothing():
    async def run():
        pool, out, salon = await build(DSN)
        async with client() as c:
            assert (await post_tg(c, tg_update(1, C1, "hi"), secret="nope")).status_code == 404
            assert (await c.post("/telegram/webhook", json=tg_update(2, C1, "hi"))).status_code == 404
            assert (await c.post("/telegram/webhook", content=b"not json", headers={"x-telegram-bot-api-secret-token": "tgsecret"})).status_code == 400
        assert out.tg == []
        await pool.close()
    asyncio.run(run())


@needs_db
def test_the_webhook_is_closed_when_telegram_is_not_configured():
    async def run():
        pool, out, salon = await build(DSN, telegram_webhook_secret="")
        async with client() as c:
            assert (await post_tg(c, tg_update(1, C1, "hi"), secret="")).status_code == 404   # an empty secret must never match an empty header
        await pool.close()
    asyncio.run(run())


@needs_db
def test_the_poller_handles_updates_in_order_acknowledges_buttons_and_backs_off_on_errors():
    async def run():
        from app import main
        from app.inbound import inbound
        pool, out, salon = await build(DSN)
        await pool.execute("DELETE FROM salon_payment")
        ctx = main.app.state.ctx
        out.script["fade"] = {"intent": "book", "service": "Fade", "date": open_day().isoformat(), "window": "afternoon"}
        out.tg_updates = [tg_update(1, C1, "fade")]
        task = asyncio.create_task(run_poller(ctx, main.app.state.tg, inbound))
        for _ in range(100):
            await asyncio.sleep(0.05)
            if out.tg_to(str(C1)):
                break
        assert "earliest" in out.tg_to(str(C1))[-1]["text"].lower() and "Fade" in out.tg_to(str(C1))[-1]["text"]
        assert any(m["method"] == "deleteWebhook" for m in out.tg)                           # polling and a webhook cannot coexist
        out.tg_updates = [tg_update(2, C1, data="slot:0")]
        for _ in range(100):
            await asyncio.sleep(0.05)
            if await pool.fetchval("SELECT count(*) FROM bookings WHERE status='confirmed'"):
                break
        assert await pool.fetchval("SELECT count(*) FROM bookings WHERE status='confirmed'") == 1
        assert any(m["method"] == "answerCallbackQuery" for m in out.tg)
        task.cancel()
        with pytest.raises(asyncio.CancelledError):
            await task
        await pool.close()
    asyncio.run(run())


def test_channels_route_by_address():
    class Rec:
        def __init__(self, name, log):
            self.name, self.log = name, log

        async def send_text(self, *a):
            self.log.append((self.name, a))
            return True

        async def send_buttons(self, *a):
            self.log.append((self.name, a))
            return True

    async def run():
        log = []
        hub = Channels(Rec("wa", log), None)
        await hub.send_text("PID", "TOK", "254711000001", "hello")
        assert log == [("wa", ("PID", "TOK", "254711000001", "hello"))]
        assert await hub.send_text("PID", "TOK", "tg:55", "hello") is False                   # no Telegram configured: a failed send, not a crash
        log.clear()
        hub = Channels(Rec("wa", log), Rec("tg", log))
        await hub.send_text("PID", "TOK", "tg:55", "hello")
        await hub.send_buttons("PID", "TOK", "tg:55", "pick", [("a", "A")])
        assert log == [("tg", ("55", "hello")), ("tg", ("55", "pick", [("a", "A")]))]
    asyncio.run(run())
