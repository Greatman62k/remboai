"""The owner's side: commands over WhatsApp, time off, cancelling with the customer told, the daily summary."""
import asyncio
import os
from datetime import time

import pytest

from app import owner
from tests.harness import build, callback, client, open_day, payload, post
from tests.test_payments import A, B, OWNER, deliver, to_pay

DSN = os.environ.get("TEST_DATABASE_URL")
needs_db = pytest.mark.skipif(not DSN, reason="needs TEST_DATABASE_URL")


def test_time_ranges_in_the_ways_owners_write_them():
    assert owner.parse_range("friday 14:00-16:00") == (time(14), time(16))
    assert owner.parse_range("friday 2pm to 4pm") == (time(14), time(16))
    assert owner.parse_range("ijumaa 2-4") == (time(14), time(16))
    assert owner.parse_range("friday 9-1") == (time(9), time(13))
    assert owner.parse_range("2026-11-06") is None and owner.parse_range("friday") is None
    assert owner.parse_range("friday 16:00-14:00") is False


async def booked_and_paid(c, out, pool):
    await to_pay(c, out, pool)
    await deliver(c, callback("ws_CO_1"))


@needs_db
def test_owner_sees_the_day_with_payment_status_in_english_and_swahili():
    async def run():
        pool, out, salon = await build(DSN)
        await pool.execute("UPDATE salons SET owner_wa_id=$1", OWNER)
        day = open_day().isoformat()
        async with client() as c:
            await booked_and_paid(c, out, pool)
            await post(c, payload(OWNER, f"bookings {day}", mid="o1"))
            assert out.bodies(OWNER)[-1].splitlines()[1] == "1. 12:00 Fade - Cust (+254711000001) deposit KES 200 paid"
            await post(c, payload(OWNER, "ratiba " + day, mid="o2"))
            assert out.bodies(OWNER)[-1].startswith("Miadi ") and "amana KES 200 imelipwa" in out.bodies(OWNER)[-1]
            await post(c, payload(OWNER, "msaada", mid="o3"))
            assert "Amri za saluni" in out.bodies(OWNER)[-1]
            await post(c, payload(OWNER, "help", mid="o4"))
            assert "Salon commands" in out.bodies(OWNER)[-1]
            assert out.calls == [out.calls[0]]                                            # only the customer's booking used the model
        await pool.close()
    asyncio.run(run())


@needs_db
def test_blocking_a_day_removes_it_from_offers_and_unblock_brings_it_back():
    async def run():
        pool, out, salon = await build(DSN)
        await pool.execute("UPDATE salons SET owner_wa_id=$1", OWNER)
        day = open_day()
        async with client() as c:
            await post(c, payload(OWNER, f"block {day.isoformat()}", mid="o1"))
            assert out.bodies(OWNER)[-1].startswith("Closed ")
            out.script["fade"] = {"intent": "book", "service": "Fade", "date": day.isoformat(), "window": "afternoon"}
            await post(c, payload(A, "fade", mid="a1"))
            offer = out.bodies(A)[-1]
            assert day.strftime("%d %b") not in offer.split("\n", 1)[1].split("\n")[0]    # first offered slot is on another day
            await post(c, payload(OWNER, f"unblock {day.isoformat()}", mid="o2"))
            assert out.bodies(OWNER)[-1].startswith("Reopened")
            await post(c, payload(OWNER, f"unblock {day.isoformat()}", mid="o3"))
            assert "nothing blocked" in out.bodies(OWNER)[-1]
            await post(c, payload(B, "fade", mid="b1"))
            assert day.strftime("%d %b") in out.bodies(B)[-1]
        await pool.close()
    asyncio.run(run())


@needs_db
def test_blocking_some_hours_hides_those_starts_and_a_slot_taken_by_the_block_is_refused():
    async def run():
        pool, out, salon = await build(DSN)
        await pool.execute("UPDATE salons SET owner_wa_id=$1", OWNER)
        day = open_day()
        out.script["fade"] = {"intent": "book", "service": "Fade", "date": day.isoformat(), "window": "afternoon"}
        async with client() as c:
            await post(c, payload(A, "fade", mid="a1"))                                   # A is offered 12:00 / 12:30 / 13:00
            assert "12:00" in out.bodies(A)[-1]
            await post(c, payload(OWNER, f"block {day.isoformat()} 12:00-13:00", mid="o1"))
            assert "Closed" in out.bodies(OWNER)[-1] and "12:00-13:00" in out.bodies(OWNER)[-1]
            await post(c, payload(A, reply_id="slot:0", mid="a2"))                        # the 12:00 offer is no longer valid
            assert "just taken" in out.bodies(A)[-1] and "12:00" not in out.bodies(A)[-1]
            assert await pool.fetchval("SELECT count(*) FROM bookings") == 0
            await post(c, payload(B, "fade", mid="b1"))
            assert "13:00" in out.bodies(B)[-1] and "12:30" not in out.bodies(B)[-1]
        await pool.close()
    asyncio.run(run())


@needs_db
def test_owner_cancels_a_paid_booking_the_customer_and_the_owner_both_hear_about_it():
    async def run():
        pool, out, salon = await build(DSN)
        await pool.execute("UPDATE salons SET owner_wa_id=$1", OWNER)
        day = open_day().isoformat()
        async with client() as c:
            await booked_and_paid(c, out, pool)
            await post(c, payload(OWNER, "cancel 1", mid="o0"))                           # before any list: nothing to point at
            assert "don't have a booking number 1" in out.bodies(OWNER)[-1]
            await post(c, payload(OWNER, f"block {day} 11:00-13:00", mid="o1"))             # warns about the overlapping booking
            assert "Heads up, 1 booking(s)" in out.bodies(OWNER)[-1] and "Cust" in out.bodies(OWNER)[-1]
            await post(c, payload(OWNER, "cancel 1", mid="o2"))
            mine = out.bodies(OWNER)[-1]
            assert mine.startswith("Cancelled: Fade") and "refund it to +254711000001" in mine
            assert "had to cancel your booking" in out.bodies(A)[-1] and "KES 200 deposit will be refunded" in out.bodies(A)[-1]
            assert await pool.fetchval("SELECT status FROM bookings") == "cancelled"
            await post(c, payload(OWNER, "cancel 1", mid="o3"))                           # already cancelled: not done twice
            assert "don't have a booking number 1" in out.bodies(OWNER)[-1]
        await pool.close()
    asyncio.run(run())


@needs_db
def test_only_the_owner_has_commands_and_an_owner_can_still_use_the_customer_flow():
    async def run():
        pool, out, salon = await build(DSN)
        await pool.execute("UPDATE salons SET owner_wa_id=$1", OWNER)
        day = open_day().isoformat()
        out.script["prices?"] = {"intent": "price_list"}
        async with client() as c:
            await post(c, payload(B, f"block {day}", mid="b1"))                           # a customer typing a command: nothing happens
            assert await pool.fetchval("SELECT count(*) FROM time_off") == 0
            await post(c, payload(OWNER, "prices?", mid="o1"))
            assert "Fade (KES 500)" in out.bodies(OWNER)[-1]
            await post(c, payload(OWNER, "block", mid="o2"))
            assert "Which day?" in out.bodies(OWNER)[-1]
            await post(c, payload(OWNER, f"block {day} 16:00-14:00", mid="o3"))
            assert "couldn't read that time range" in out.bodies(OWNER)[-1] and await pool.fetchval("SELECT count(*) FROM time_off") == 0
        await pool.close()
    asyncio.run(run())


@needs_db
def test_daily_summary_is_sent_once_per_day_with_deposits_and_tomorrows_bookings(monkeypatch):
    async def run():
        pool, out, salon = await build(DSN)
        await pool.execute("UPDATE salons SET owner_wa_id=$1", OWNER)
        async with client() as c:
            await booked_and_paid(c, out, pool)
            ctx = c._transport.app.state.ctx
            monkeypatch.setattr(owner, "SUMMARY_HOUR", 24)
            assert await owner.daily_summaries(ctx) == 0                                   # not evening yet
            monkeypatch.setattr(owner, "SUMMARY_HOUR", 0)
            assert await owner.daily_summaries(ctx) == 1
            assert await owner.daily_summaries(ctx) == 0                                   # once per day
        body = out.bodies(OWNER)[-1]
        assert "Deposits collected today: KES 200 (1)." in body
        await pool.close()
    asyncio.run(run())
