"""Isolated backend process for the cross-repository Glance acceptance run.

Run only with docker-compose.glance-integration.yml and the test PostgreSQL/Redis
services. It seeds synthetic records, issues a pairing code through the Telegram
owner handler backed by the harness's intercepted Telegram transport, and serves
the real FastAPI app without production lifespan workers.
"""
from __future__ import annotations

import asyncio
import json
import os
import re
import secrets
import uuid
from datetime import datetime, timezone
from pathlib import Path
from zoneinfo import ZoneInfo

import uvicorn

from app.ai.voice import Transcript, Transcriber
from app.agent.client import Step
from app.handoffs import create_handoff, record_customer_message
from app.main import app
from tests.harness import build

OWNER = "tg:700000091"
BOT_ID = "tg:111"
HMAC_KEY = secrets.token_hex(32)
ORIGIN = os.environ.get("GLANCE_INTEGRATION_ORIGIN", "http://127.0.0.1:5173")


class DeterministicModel:
    def __init__(self):
        self.calls = 0

    async def step(self, system, messages, tools):
        assert tools == []
        self.calls += 1
        text = (
            "I’ll check the current walk-in availability and update you shortly."
            if self.calls > 1
            else "Thanks for reaching out. I’ll check the current walk-in availability."
        )
        return Step(text=text, model="day5-controlled-model")


class RecordingVoice:
    def __init__(self, transcriber: Transcriber):
        self.inner = transcriber
        self.enabled = transcriber.enabled
        self.calls: list[dict] = []

    async def transcribe(self, audio, mime, lang, salon_id=None):
        self.calls.append({"size": len(audio), "mime": mime, "lang": lang, "salon_id": salon_id})
        return await self.inner.transcribe(audio, mime, lang, salon_id=salon_id)


async def seed_and_serve() -> None:
    ready_path = Path(os.environ.get("GLANCE_INTEGRATION_READY_FILE", "/tmp/glance-integration/ready.json"))
    ready_path.parent.mkdir(parents=True, exist_ok=True)

    pool, outside, salon_id = await build(
        os.environ["TEST_DATABASE_URL"],
        till=False,
        glance_enabled=True,
        glance_pairing_hmac_keyring=json.dumps({"7": HMAC_KEY}),
        glance_pairing_hmac_active_version=7,
        glance_session_ttl_seconds=604800,
        glance_read_limit_per_minute=120,
        glance_write_limit_per_minute=20,
        glance_transcription_limit_per_minute=6,
        glance_cors_origins=ORIGIN,
    )
    await pool.execute(
        "UPDATE salons SET owner_wa_id=$2,tg_bot_id=$3 WHERE id=$1::uuid", salon_id, OWNER, BOT_ID
    )
    other_salon = await pool.fetchval(
        "INSERT INTO salons (name,wa_phone_number_id,tg_bot_id,owner_wa_id) "
        "VALUES ('Synthetic Second Salon','PID-DAY5-SECOND',$1,$2) RETURNING id::text",
        BOT_ID,
        OWNER,
    )

    def create_case(target_salon: str, wa_id: str, name: str, first_message: str):
        async def operation():
            client_id = await pool.fetchval(
                "INSERT INTO clients (salon_id,wa_id,name) VALUES ($1::uuid,$2,$3) RETURNING id::text",
                target_salon,
                wa_id,
                name,
            )
            case = await create_handoff(
                pool,
                target_salon,
                client_id,
                wa_id,
                "human_request",
                first_message,
                source_message_id=f"day5-{wa_id}-initial",
            )
            return case["id"]

        return operation

    context_case = await create_case(
        salon_id,
        "tg:810000091",
        "Synthetic Context Customer",
        "Could I ask the owner about walk-in availability?",
    )()
    # The detail route must retrieve the extra event, return the latest ten in
    # sequence order, and report has_earlier accurately.
    for index in range(1, 12):
        await record_customer_message(
            pool,
            salon_id,
            "tg:810000091",
            f"Synthetic context follow-up {index}.",
            f"day5-context-{index}",
        )

    manual_case = await create_case(
        salon_id,
        "tg:810000092",
        "Synthetic Manual Customer",
        "Please let me know if walk-ins are available.",
    )()
    foreign_case = await create_case(
        other_salon,
        "tg:810000093",
        "Synthetic Other Salon Customer",
        "This case belongs to the other synthetic salon.",
    )()

    # Pair through the authenticated Telegram owner command. Two eligible
    # salons force explicit selection; the outbound Telegram API is intercepted
    # by tests.harness. No external recipient or provider is contacted.
    from app import owner

    salon_row = dict(await pool.fetchrow(
        "SELECT id::text,name,owner_wa_id,tg_bot_id FROM salons WHERE id=$1::uuid", salon_id
    ))
    assert await owner.handle(
        app.state.ctx,
        salon_row,
        OWNER,
        "glance pair",
        ZoneInfo("UTC"),
        datetime.now(timezone.utc),
    )
    keyboard = outside.tg[-1]["reply_markup"]["inline_keyboard"]
    selection = next(
        button["callback_data"]
        for row in keyboard
        for button in row
        if salon_id in button["callback_data"]
    )
    assert await owner.handle_tap(app.state.ctx, salon_row, OWNER, selection)
    pairing_text = outside.tg[-1]["text"]
    code_match = re.search(r"Code: ([0-9A-HJKMNP-TV-Z]{12})", pairing_text)
    assert code_match, "mock Telegram pairing response did not contain the expected code"

    outside.asr_text = "Please make the reply warmer and shorter."
    voice = RecordingVoice(app.state.ctx.voice)
    app.state.ctx.voice = voice
    model = DeterministicModel()
    app.state.ctx.agent = model

    # Uvicorn serves the real FastAPI middleware/router. Lifespan is disabled so
    # the production background workers and external credentials are never used.
    config = uvicorn.Config(
        app,
        host="0.0.0.0",
        port=8080,
        log_level="warning",
        access_log=False,
        lifespan="off",
        server_header=False,
    )
    server = uvicorn.Server(config)
    serving = asyncio.create_task(server.serve())
    for _ in range(400):
        if server.started:
            break
        if serving.done():
            await serving
            raise RuntimeError("Uvicorn stopped before readiness")
        await asyncio.sleep(0.025)
    else:
        server.should_exit = True
        await serving
        raise TimeoutError("Uvicorn did not become ready")

    # The ephemeral pair code is passed only through a permission-restricted
    # temporary file, never stdout, container logs, or repository files.
    run_id = uuid.uuid4().hex
    host_control_dir = Path(os.environ.get("GLANCE_INTEGRATION_HOST_DIR", "/tmp/remboai-glance-day5-user"))
    dispatch_name = f"dispatch-{run_id}"
    done_name = f"done-{run_id}"
    ready_path.write_text(
        json.dumps({
            "api_base": "http://127.0.0.1:18081/api/v1/glance",
            "origin": ORIGIN,
            "pairing_code": code_match.group(1),
            "salon_case_id": context_case,
            "manual_case_id": manual_case,
            "other_salon_case_id": foreign_case,
            "salon_id": salon_id,
            "event_count_seeded": 12,
            "dispatch_signal": str(host_control_dir / dispatch_name),
            "done_signal": str(host_control_dir / done_name),
            "dispatch_signal_in_container": str(ready_path.parent / dispatch_name),
            "done_signal_in_container": str(ready_path.parent / done_name),
        }),
        encoding="utf-8",
    )
    ready_path.chmod(0o600)
    ready = json.loads(ready_path.read_text(encoding="utf-8"))
    dispatch_requested = False
    dispatch_state = None
    try:
        while not server.should_exit:
            if not dispatch_requested and Path(ready["dispatch_signal_in_container"]).exists():
                from app.handoff_outbox import dispatch_one

                dispatch_state = await dispatch_one(app.state.ctx)
                dispatch_requested = True
            if Path(ready["done_signal_in_container"]).exists():
                server.should_exit = True
                break
            await asyncio.sleep(0.25)
    finally:
        server.should_exit = True
        await serving
        assert not outside.sent, "integration dispatched a WhatsApp message"
        customer_sends = [
            call for call in outside.tg
            if call["method"] == "sendMessage" and call.get("chat_id") == "810000092"
        ]
        assert dispatch_state == "submitted" and len(customer_sends) == 1, "intercepted Telegram dispatch did not submit exactly one synthetic reply"
        assert customer_sends[0]["text"] == "Thanks for your message. I will check and update you shortly."
        assert all(
            call["method"] != "sendMessage" or call.get("chat_id") in {"700000091", "810000092"}
            for call in outside.tg
        ), "outbound Telegram transport saw a non-synthetic recipient"
        assert all(call["salon_id"] == salon_id for call in voice.calls)
        assert all(call["mime"] == "audio/wav" and call["lang"] is None for call in voice.calls)
        await app.state.wa.http.aclose()
        await pool.close()


if __name__ == "__main__":
    asyncio.run(seed_and_serve())
