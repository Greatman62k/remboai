"""The keyword path used to cancel the customer's soonest booking outright, even when they had several. It now asks which."""
import asyncio
import os
from datetime import datetime, timedelta, timezone
from zoneinfo import ZoneInfo

import pytest

from app import booking_flow as bf
from app import bookings
from app.agent.tools import CaptureChat
from tests.harness import build

DSN = os.environ.get("TEST_DATABASE_URL")
pytestmark = pytest.mark.skipif(not DSN, reason="needs TEST_DATABASE_URL")
TZ = ZoneInfo("Africa/Nairobi")


async def _setup(n_bookings: int):
    pool, out, salon_id = await build(DSN)
    from app import conversation
    from app.main import app
    ctx = app.state.ctx
    salon = await conversation._salon_row(ctx, salon_id)
    cid = await pool.fetchval("INSERT INTO clients (salon_id, wa_id, lang) VALUES ($1::uuid,'cust','en') RETURNING id::text", salon_id)
    other = await pool.fetchval("INSERT INTO clients (salon_id, wa_id, lang) VALUES ($1::uuid,'someone-else','en') RETURNING id::text", salon_id)
    sid = await pool.fetchval("SELECT id::text FROM services WHERE name='Fade'")
    stid = await pool.fetchval("SELECT id::text FROM staff LIMIT 1")
    ids = []
    for i in range(n_bookings):
        start = datetime.now(timezone.utc) + timedelta(days=2 + i)
        ids.append(await bookings.hold_slot(pool, salon_id=salon_id, staff_id=stid, service_id=sid, client_id=cid, starts_at=start,
                                            ends_at=start + timedelta(minutes=45), minutes=30))
    start = datetime.now(timezone.utc) + timedelta(days=9)
    theirs = await bookings.hold_slot(pool, salon_id=salon_id, staff_id=stid, service_id=sid, client_id=other, starts_at=start,
                                      ends_at=start + timedelta(minutes=45), minutes=30)
    client = {"id": cid, "lang": "en", "wa_id": "cust"}
    return pool, ctx, salon, client, ids, theirs


async def _status(pool, bid):
    return await pool.fetchval("SELECT status FROM bookings WHERE id=$1::uuid", bid)


def _args(ctx, salon, client, chat):
    return (ctx, salon, client, {"history": []}, [], TZ, datetime.now(TZ), chat, "en")


def test_with_several_bookings_it_asks_and_cancels_nothing():
    async def run():
        pool, ctx, salon, client, ids, theirs = await _setup(3)
        chat = CaptureChat("cust")
        await bf.cancel_or_ask(*_args(ctx, salon, client, chat))
        assert [await _status(pool, b) for b in ids] == ["held", "held", "held"], "nothing may be cancelled until the customer says which"
        assert [b[0] for b in chat.buttons_] == [f"cxl:{b}" for b in ids]
        assert all(len(label) <= 20 for _, label in chat.buttons_)
        await pool.close()
    asyncio.run(run())


def test_with_one_booking_it_cancels_it_as_before():
    async def run():
        pool, ctx, salon, client, ids, theirs = await _setup(1)
        chat = CaptureChat("cust")
        await bf.cancel_or_ask(*_args(ctx, salon, client, chat))
        assert await _status(pool, ids[0]) == "cancelled" and not chat.buttons_
        await pool.close()
    asyncio.run(run())


def test_with_no_bookings_it_says_so():
    async def run():
        pool, ctx, salon, client, ids, theirs = await _setup(0)
        chat = CaptureChat("cust")
        await bf.cancel_or_ask(*_args(ctx, salon, client, chat))
        assert "couldn't find an upcoming booking" in chat.joined
        assert await _status(pool, theirs) == "held", "someone else's booking is never touched"
        await pool.close()
    asyncio.run(run())


def test_tapping_one_cancels_only_that_one():
    async def run():
        pool, ctx, salon, client, ids, theirs = await _setup(3)
        chat = CaptureChat("cust")
        assert await bf.handle_cancel_tap(*_args(ctx, salon, client, chat), f"cxl:{ids[1]}")
        assert [await _status(pool, b) for b in ids] == ["held", "cancelled", "held"]
        await pool.close()
    asyncio.run(run())


def test_a_forged_tap_cannot_cancel_another_customers_booking():
    async def run():
        pool, ctx, salon, client, ids, theirs = await _setup(2)
        chat = CaptureChat("cust")
        assert await bf.handle_cancel_tap(*_args(ctx, salon, client, chat), f"cxl:{theirs}")
        assert await _status(pool, theirs) == "held"
        assert [await _status(pool, b) for b in ids] == ["held", "held"]
        assert "couldn't find an upcoming booking" in chat.joined
        await pool.close()
    asyncio.run(run())


def test_other_buttons_are_not_claimed_by_the_cancel_handler():
    async def run():
        pool, ctx, salon, client, ids, theirs = await _setup(1)
        chat = CaptureChat("cust")
        for rid in ("manualpay:paid:" + ids[0], "slot:1", "cxl:not-a-uuid", "", None):
            assert await bf.handle_cancel_tap(*_args(ctx, salon, client, chat), rid) is False
        assert await _status(pool, ids[0]) == "held"
        await pool.close()
    asyncio.run(run())


def test_end_to_end_cancel_asks_which_then_cancels_only_the_tapped_one():
    from tests.harness import client, open_day, payload, post
    A = "254711000001"

    async def run():
        pool, out, salon = await build(DSN)
        out.script.update({"book silk": {"intent": "book", "service": "silk", "date": open_day().isoformat(), "window": "morning"},
                           "cancel it": {"intent": "cancel"}})
        async with client() as c:
            await post(c, payload(A, "book silk", mid="1"))
            await post(c, payload(A, reply_id="slot:0", mid="2"))
            first = await pool.fetchval("SELECT id::text FROM bookings WHERE status='held'")
            cid = await pool.fetchval("SELECT client_id::text FROM bookings WHERE id=$1::uuid", first)
            sid = await pool.fetchval("SELECT id::text FROM services WHERE name='Fade'")
            stid = await pool.fetchval("SELECT id::text FROM staff LIMIT 1")
            start = datetime.now(timezone.utc) + timedelta(days=12)
            second = await bookings.hold_slot(pool, salon_id=salon, staff_id=stid, service_id=sid, client_id=cid, starts_at=start,
                                              ends_at=start + timedelta(minutes=45), minutes=30)
            assert second
            await post(c, payload(A, "cancel it", mid="3"))
            assert "more than one upcoming booking" in out.bodies(A)[-1]
            assert await pool.fetchval("SELECT count(*) FROM bookings WHERE status='held'") == 2, "asking must not cancel anything"
            await post(c, payload(A, reply_id=f"cxl:{second}", mid="4"))
            assert await _status(pool, second) == "cancelled" and await _status(pool, first) == "held"
            assert "cancelled" in out.bodies(A)[-1]
        await pool.close()
    asyncio.run(run())
