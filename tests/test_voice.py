"""Voice notes: Meta holds the audio, our speech service turns it into text, and the normal conversation takes over."""
import asyncio
import os
from types import SimpleNamespace

import pytest

from app.ai.voice import Transcriber
from app.channels.whatsapp import parse_messages
from tests.harness import PID, build, client, open_day, payload, post

DSN = os.environ.get("TEST_DATABASE_URL")
needs_db = pytest.mark.skipif(not DSN, reason="needs TEST_DATABASE_URL")
A = "254711000001"


def voice(wa_id: str = A, mid: str = "v1", media: str = "MEDIA1") -> dict:
    body = payload(wa_id, "", mid=mid)
    body["entry"][0]["changes"][0]["value"]["messages"][0] = {"from": wa_id, "id": mid, "type": "audio", "audio": {"id": media, "mime_type": "audio/ogg; codecs=opus", "voice": True}}
    return body


def test_auth_headers_carry_our_token_and_proxy_keys_when_set():
    plain = Transcriber(None, SimpleNamespace(asr_token="tok", asr_proxy_key="", asr_proxy_secret=""))
    assert plain.auth_headers == {"X-Asr-Token": "tok"}
    proxied = Transcriber(None, SimpleNamespace(asr_token="tok", asr_proxy_key="kid", asr_proxy_secret="sec"))
    assert proxied.auth_headers == {"X-Asr-Token": "tok", "Modal-Key": "kid", "Modal-Secret": "sec"}


def test_an_audio_message_carries_its_media_id():
    [m] = parse_messages(voice())
    assert (m.kind, m.media_id, m.text) == ("audio", "MEDIA1", "")


@needs_db
def test_a_voice_note_becomes_a_booking_conversation_and_the_customer_is_told_what_was_heard():
    async def run():
        pool, out, salon = await build(DSN)
        out.script["nataka fade kesho mchana"] = {"intent": "book", "service": "Fade", "date": open_day().isoformat(), "window": "afternoon", "language": "sw"}
        async with client() as c:
            await post(c, voice())
        assert "nataka fade kesho mchana" in out.bodies(A)[0]
        assert "Nafasi za mapema za Fade" in out.bodies(A)[1]
        [call] = out.asr
        assert call["auth"] == "asrtok" and call["type"] == "audio/ogg" and call["bytes"] == b"OggS-fake-voice-note"
        assert call["lang"] is None
        await pool.close()
    asyncio.run(run())


@needs_db
def test_a_known_swahili_speaker_gets_swahili_recognition_and_replies():
    async def run():
        pool, out, salon = await build(DSN)
        out.script["nataka fade kesho mchana"] = {"intent": "book", "service": "Fade", "date": open_day().isoformat(), "window": "afternoon", "language": "sw"}
        async with client() as c:
            await post(c, voice(mid="v1"))
            await post(c, voice(mid="v2"))
        assert [x["lang"] for x in out.asr] == [None, "swh_Latn"]
        assert out.bodies(A)[2].startswith("Nimesikia:")
        await pool.close()
    asyncio.run(run())


@needs_db
def test_when_recognition_fails_the_customer_is_asked_to_retry_and_nothing_is_booked():
    async def run():
        pool, out, salon = await build(DSN)
        async with client() as c:
            out.asr_down = True
            await post(c, voice(mid="v1"))
            assert ("could not" in out.bodies(A)[-1].lower() or "couldn't" in out.bodies(A)[-1].lower()) and "make it out" in out.bodies(A)[-1].lower()
            out.asr_down = False
            out.asr_text = "   "
            await post(c, voice(mid="v2"))
            assert ("could not" in out.bodies(A)[-1].lower() or "couldn't" in out.bodies(A)[-1].lower()) and "make it out" in out.bodies(A)[-1].lower()
            out.media_gone = True
            await post(c, voice(mid="v3"))
            assert ("could not" in out.bodies(A)[-1].lower() or "couldn't" in out.bodies(A)[-1].lower()) and "make it out" in out.bodies(A)[-1].lower()
            out.media_gone, out.media_size = False, 5_000_000
            await post(c, voice(mid="v4"))
            assert ("could not" in out.bodies(A)[-1].lower() or "couldn't" in out.bodies(A)[-1].lower()) and "make it out" in out.bodies(A)[-1].lower() and len(out.asr) == 1
        assert await pool.fetchval("SELECT count(*) FROM bookings") == 0
        await pool.close()
    asyncio.run(run())


@needs_db
def test_without_a_speech_service_voice_notes_get_the_text_only_reply():
    async def run():
        pool, out, salon = await build(DSN, asr_url="")
        async with client() as c:
            await post(c, voice())
        assert "text messages" in out.bodies(A)[-1].lower() and out.asr == []
        await pool.close()
    asyncio.run(run())
