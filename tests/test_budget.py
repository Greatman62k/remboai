"""A flood of messages must not run up a bill: past the daily ceilings the bot degrades instead of spending."""
import asyncio
import os

import pytest

from app.ai.budget import Budget
from tests.harness import PID, app, build, client, open_day, payload, post, tg_update, post_tg

DSN = os.environ.get("TEST_DATABASE_URL")
needs_db = pytest.mark.skipif(not DSN, reason="needs TEST_DATABASE_URL")
A = "254711000001"


def photo(who: str, mid: str) -> dict:
    body = payload(who, "", mid=mid)
    body["entry"][0]["changes"][0]["value"]["messages"][0] = {"from": who, "id": mid, "type": "image", "image": {"id": "IMG1", "mime_type": "image/jpeg"}}
    return body


@needs_db
def test_the_counter_stops_at_the_limit_and_zero_means_unlimited():
    async def run():
        pool, out, salon = await build(DSN, llm_daily_limit=2, vision_daily_limit=0)
        b = Budget(pool, app.state.settings)
        assert [await b.take("text") for _ in range(4)] == [True, True, False, False]
        assert all([await b.take("vision") for _ in range(5)])
        assert (await b.today())["text"] == 2
        await pool.close()
    asyncio.run(run())


@needs_db
def test_past_the_text_ceiling_the_model_is_not_called_and_keyword_rules_still_answer():
    async def run():
        pool, out, salon = await build(DSN, llm_daily_limit=1)
        app.state.ctx.llm.budget = Budget(pool, app.state.settings)
        out.script["fade kesho"] = {"intent": "book", "service": "Fade", "date": open_day().isoformat(), "window": "afternoon"}
        async with client() as c:
            await post(c, payload(A, "fade kesho", mid="m1"))
            await post(c, payload(A, "hello there", mid="m2"))
            await post(c, payload("254711000002", "hi, i need a haircut", mid="m3"))
        assert len(out.calls) == 1
        assert len(out.bodies(A)) == 2 and len(out.bodies("254711000002")) == 1
        await pool.close()
    asyncio.run(run())


@needs_db
def test_past_the_photo_ceiling_the_customer_gets_the_menu_and_no_model_call_is_made():
    async def run():
        pool, out, salon = await build(DSN, vision_daily_limit=1)
        app.state.ctx.vision.budget = Budget(pool, app.state.settings)
        out.media_mime = "image/jpeg"
        async with client() as c:
            await post(c, photo(A, "p1"))
            await post(c, photo("254711000002", "p2"))
        assert len(out.vision) == 1
        assert "Fade" in out.bodies("254711000002")[-1]
        await pool.close()
    asyncio.run(run())


@needs_db
def test_past_the_voice_ceiling_the_customer_is_asked_to_type_and_the_speech_service_is_not_called():
    async def run():
        pool, out, salon = await build(DSN, voice_daily_limit=1)
        app.state.ctx.voice.budget = Budget(pool, app.state.settings)
        voice = lambda who, mid: {**payload(who, "", mid=mid), "entry": [{"changes": [{"value": {"metadata": {"phone_number_id": PID}, "contacts": [{"wa_id": who, "profile": {"name": "C"}}],
                                  "messages": [{"from": who, "id": mid, "type": "audio", "audio": {"id": "M1", "mime_type": "audio/ogg"}}]}}]}]}
        async with client() as c:
            await post(c, voice(A, "v1"))
            await post(c, voice("254711000002", "v2"))
        assert len(out.asr) == 1
        await pool.close()
    asyncio.run(run())


def test_an_unknown_kind_is_denied_not_given_a_free_pass():
    async def run():
        b = Budget(None, __import__("types").SimpleNamespace(llm_daily_limit=5, vision_daily_limit=5, voice_daily_limit=5))
        assert await b.take("telepathy") is False
    asyncio.run(run())


@needs_db
def test_one_person_cannot_send_more_than_the_daily_message_limit():
    async def run():
        pool, out, salon = await build(DSN, per_sender_daily_limit=3, rate_limit_per_min=100)
        async with client() as c:
            for i in range(3):
                await post_tg(c, tg_update(100 + i, 5002, "hi"))
            for i in range(6):
                await post_tg(c, tg_update(i + 1, 5001, "hi"))
        assert len(out.tg_to("5001")) == len(out.tg_to("5002")) > 0
        await pool.close()
    asyncio.run(run())
