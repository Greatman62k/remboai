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
            assert "Msaada kwa mmiliki wa biashara" in out.bodies(OWNER)[-1]
            await post(c, payload(OWNER, "help", mid="o4"))
            assert "Help for business owners" in out.bodies(OWNER)[-1]
            assert out.calls == [out.calls[0]]
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
            assert day.strftime("%d %b") not in offer.split("\n", 1)[1].split("\n")[0]
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
            await post(c, payload(A, "fade", mid="a1"))
            assert "12:00" in out.bodies(A)[-1]
            await post(c, payload(OWNER, f"block {day.isoformat()} 12:00-13:00", mid="o1"))
            assert "Closed" in out.bodies(OWNER)[-1] and "12:00-13:00" in out.bodies(OWNER)[-1]
            await post(c, payload(A, reply_id="slot:0", mid="a2"))
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
            await post(c, payload(OWNER, "cancel 1", mid="o0"))
            assert "don't have a booking number 1" in out.bodies(OWNER)[-1]
            await post(c, payload(OWNER, f"block {day} 11:00-13:00", mid="o1"))
            assert "Heads up, 1 booking(s)" in out.bodies(OWNER)[-1] and "Cust" in out.bodies(OWNER)[-1]
            await post(c, payload(OWNER, "cancel 1", mid="o2"))
            mine = out.bodies(OWNER)[-1]
            assert mine.startswith("Cancelled: Fade") and "refund it to +254711000001" in mine
            assert "had to cancel your booking" in out.bodies(A)[-1] and "KES 200 deposit will be refunded" in out.bodies(A)[-1]
            assert await pool.fetchval("SELECT status FROM bookings") == "cancelled"
            p = await pool.fetchrow("SELECT status, refund_reason FROM payments")
            assert (p["status"], p["refund_reason"]) == ("needs_refund", "cancelled")
            assert await pool.fetchval("SELECT status FROM payment_reviews") == "refund_due"
            await post(c, payload(OWNER, "cancel 1", mid="o3"))
            assert "don't have a booking number 1" in out.bodies(OWNER)[-1]
        await pool.close()
    asyncio.run(run())


@needs_db
def test_owner_marks_a_confirmed_booking_as_no_show_and_a_hold_is_refused_clearly():
    async def run():
        pool, out, salon = await build(DSN)
        await pool.execute("UPDATE salons SET owner_wa_id=$1", OWNER)
        day = open_day().isoformat()
        async with client() as c:
            await booked_and_paid(c, out, pool)
            await post(c, payload(OWNER, f"bookings {day}", mid="o1"))
            await post(c, payload(OWNER, "noshow 1", mid="o2"))
            assert "no-show" in out.bodies(OWNER)[-1].lower()
            assert await pool.fetchval("SELECT status FROM bookings") == "no_show"
            await to_pay(c, out, pool, who=B, mid="h")
            await post(c, payload(OWNER, f"bookings {day}", mid="o3"))
            await post(c, payload(OWNER, "noshow 1", mid="o4"))
            assert "unpaid hold" in out.bodies(OWNER)[-1]
            assert await pool.fetchval("SELECT status FROM bookings WHERE client_id=(SELECT id FROM clients WHERE wa_id=$1)", B) == "held"
        await pool.close()
    asyncio.run(run())


@needs_db
def test_owner_pushes_a_stylist_late_and_everyone_hears_about_it(monkeypatch):
    async def run():
        from datetime import datetime, time, timedelta
        from zoneinfo import ZoneInfo
        from app import owner as _owner
        from app.main import app
        pool, out, salon = await build(DSN)
        await pool.execute("UPDATE salons SET owner_wa_id=$1", OWNER)
        ctx = app.state.ctx
        nbo = ZoneInfo("Africa/Nairobi")
        fixed_now = datetime.combine(datetime.now(nbo).date(), time(22, 30), nbo)

        class FixedDateTime(datetime):
            @classmethod
            def now(cls, tz=None):
                return fixed_now.astimezone(tz) if tz else fixed_now.replace(tzinfo=None)

        monkeypatch.setattr(_owner, "datetime", FixedDateTime)
        staff = await pool.fetchval("SELECT id::text FROM staff")
        svc = await pool.fetchval("SELECT id::text FROM services WHERE name='Fade'")
        cid = await pool.fetchval("INSERT INTO clients (salon_id, wa_id, lang) VALUES ($1::uuid,$2,'en') RETURNING id::text", salon, A)
        start = FixedDateTime.now(nbo) + timedelta(hours=1)
        day = start.date()
        booking = await pool.fetchval(
            "INSERT INTO bookings (salon_id, staff_id, service_id, client_id, starts_at, ends_at, status) "
            "VALUES ($1::uuid,$2::uuid,$3::uuid,$4::uuid,$5,$6,'confirmed') RETURNING id::text",
            salon, staff, svc, cid, start, start + timedelta(minutes=45))
        shop = {"id": salon, "name": "Zuri Cuts", "owner_wa_id": OWNER, "wa_phone_number_id": "PID1", "wa_access_token": None}
        await _owner._late(ctx, shop, OWNER, "Brian", "30", nbo, day, "en")
        mine = out.bodies(OWNER)[-1]
        assert "Brian" in mine and "30" in mine and "Fade" in mine
        shifted_start = await pool.fetchval("SELECT starts_at FROM bookings")
        assert shifted_start == start + timedelta(minutes=30)
        assert shifted_start.astimezone(nbo).date() == day + timedelta(days=1)
        assert "moved into tomorrow" in mine.lower()
        cust = out.bodies(A)[-1]
        assert "30 minutes late" in cust and "Keep it" in str(out.to(A)[-1])
        assert await pool.fetchval("SELECT count(*) FROM delays") == 1
        await _owner._late(ctx, shop, OWNER, "Brian", "30", nbo, day, "en")
        assert await pool.fetchval("SELECT starts_at FROM bookings") == start + timedelta(minutes=30)
        assert await pool.fetchval("SELECT count(*) FROM delays") == 1
        async with client() as c:
            await post(c, payload(A, reply_id=f"late-keep:{booking}", mid="k1"))
            assert "new time" in out.bodies(A)[-1]
            n_b = len(out.to(B))
            await post(c, payload(B, reply_id=f"late-keep:{booking}", mid="k2"))
            assert len(out.to(B)) == n_b
            assert await pool.fetchval("SELECT status FROM bookings") == "confirmed"
        await pool.close()
    asyncio.run(run())


@needs_db
def test_a_customer_answers_a_lateness_notice_cancel_or_new_time():
    async def run():
        from datetime import datetime, timedelta
        from zoneinfo import ZoneInfo
        pool, out, salon = await build(DSN)
        await pool.execute("UPDATE salons SET owner_wa_id=$1", OWNER)
        nbo = ZoneInfo("Africa/Nairobi")
        staff = await pool.fetchval("SELECT id::text FROM staff")
        svc = await pool.fetchval("SELECT id::text FROM services WHERE name='Fade'")
        mk = lambda who, hrs: pool.fetchval(
            "INSERT INTO clients (salon_id, wa_id, lang) VALUES ($1::uuid,$2,'en') RETURNING id::text", salon, who)
        a = await mk(A, 0)
        b = await mk(B, 0)
        base = (datetime.now(nbo) + timedelta(days=1)).replace(hour=10, minute=0, second=0, microsecond=0)
        mkb = lambda cid, h: pool.fetchval(
            "INSERT INTO bookings (salon_id, staff_id, service_id, client_id, starts_at, ends_at, status) "
            "VALUES ($1::uuid,$2::uuid,$3::uuid,$4::uuid,$5,$6,'confirmed') RETURNING id::text",
            salon, staff, svc, cid, base + timedelta(hours=h), base + timedelta(hours=h, minutes=45))
        ba, bb = await mkb(a, 0), await mkb(b, 1)
        async with client() as c:
            await post(c, payload(A, reply_id=f"late-cancel:{ba}", mid="c1"))
            assert "has been cancelled" in out.bodies(A)[-1]
            assert await pool.fetchval("SELECT status FROM bookings WHERE id=$1::uuid", ba) == "cancelled"
            await post(c, payload(B, reply_id=f"late-move:{bb}", mid="m1"))
            assert "new time" in out.bodies(B)[-1]
            old = await pool.fetchval("SELECT starts_at FROM bookings WHERE id=$1::uuid", bb)
            await post(c, payload(B, reply_id="slot:0", mid="m2"))
            new = await pool.fetchval("SELECT starts_at FROM bookings WHERE id=$1::uuid", bb)
            assert "now " in out.bodies(B)[-1] and new != old
            assert "Moved:" in out.bodies(OWNER)[-1]
        await pool.close()
    asyncio.run(run())


@needs_db
def test_an_owner_cannot_touch_another_salons_booking_on_the_shared_number():
    async def run():
        from datetime import datetime, timedelta
        from zoneinfo import ZoneInfo
        pool, out, salon = await build(DSN)
        await pool.execute("UPDATE salons SET owner_wa_id=$1", OWNER)
        nbo = ZoneInfo("Africa/Nairobi")
        other = await pool.fetchval("INSERT INTO salons (name, wa_phone_number_id, owner_wa_id) VALUES ('Second', 'PID1', $1) RETURNING id::text", B)
        staff = await pool.fetchval("INSERT INTO staff (salon_id, name) VALUES ($1::uuid,'Zawadi') RETURNING id::text", other)
        svc = await pool.fetchval("INSERT INTO services (salon_id, name, duration_min, price_kes) VALUES ($1::uuid,'Fade',45,500) RETURNING id::text", other)
        cid = await pool.fetchval("INSERT INTO clients (salon_id, wa_id, lang) VALUES ($1::uuid,$2,'en') RETURNING id::text", other, B)
        start = datetime.now(nbo) + timedelta(hours=3)
        booking = await pool.fetchval(
            "INSERT INTO bookings (salon_id, staff_id, service_id, client_id, starts_at, ends_at, status) "
            "VALUES ($1::uuid,$2::uuid,$3::uuid,$4::uuid,$5,$6,'confirmed') RETURNING id::text",
            other, staff, svc, cid, start, start + timedelta(minutes=45))
        async with client() as c:
            await post(c, payload(OWNER, "cancel 0711000002", mid="x1"))
            assert "No upcoming booking found for" in out.bodies(OWNER)[-1]
            assert await pool.fetchval("SELECT status FROM bookings WHERE id=$1::uuid", booking) == "confirmed"
            assert out.bodies(B) == []
        await pool.close()
    asyncio.run(run())


@needs_db
def test_owner_gets_a_shareable_invite_link():
    async def run():
        pool, out, salon = await build(DSN)
        await pool.execute("UPDATE salons SET owner_wa_id=$1", OWNER)
        async with client() as c:
            await post(c, payload(OWNER, "invite", mid="i1"))
            assert "/start zuri-cuts" in out.bodies(OWNER)[-1]
        await pool.close()
    asyncio.run(run())


@needs_db
def test_owner_cancels_and_moves_by_customer_number():
    async def run():
        from datetime import datetime, timedelta
        from zoneinfo import ZoneInfo
        pool, out, salon = await build(DSN)
        await pool.execute("UPDATE salons SET owner_wa_id=$1", OWNER)
        nbo = ZoneInfo("Africa/Nairobi")
        staff = await pool.fetchval("SELECT id::text FROM staff")
        svc = await pool.fetchval("SELECT id::text FROM services WHERE name='Fade'")
        a = await pool.fetchval("INSERT INTO clients (salon_id, wa_id, lang) VALUES ($1::uuid,$2,'en') RETURNING id::text", salon, A)
        morrow = open_day()
        mkb = lambda cid, h: pool.fetchval(
            "INSERT INTO bookings (salon_id, staff_id, service_id, client_id, starts_at, ends_at, status) "
            "VALUES ($1::uuid,$2::uuid,$3::uuid,$4::uuid,$5,$6,'confirmed') RETURNING id::text",
            salon, staff, svc, cid, datetime.combine(morrow, time(10, 0), nbo) + timedelta(hours=h),
            datetime.combine(morrow, time(10, 0), nbo) + timedelta(hours=h, minutes=45))
        ba, bb = await mkb(a, 0), await mkb(a, 2)
        async with client() as c:
            await post(c, payload(OWNER, "cancel 0711000001", mid="c1"))
            assert "More than one" in out.bodies(OWNER)[-1]
            await post(c, payload(OWNER, "cancel 2", mid="c2"))
            assert "Cancelled:" in out.bodies(OWNER)[-1]
            assert await pool.fetchval("SELECT status FROM bookings WHERE id=$1::uuid", bb) == "cancelled"
            assert "had to cancel" in out.bodies(A)[-1]
            await post(c, payload(OWNER, f"move 0711000001 {morrow.strftime('%A')} 14:00", mid="m1"))
            assert "Moved:" in out.bodies(OWNER)[-1]
            got = await pool.fetchval("SELECT starts_at FROM bookings WHERE id=$1::uuid", ba)
            assert got.astimezone(nbo).strftime("%H:%M") == "14:00"
            assert "now " in out.bodies(A)[-1] and out.bodies(B) == []
        await pool.close()
    asyncio.run(run())


@needs_db
def test_blocking_a_day_warns_its_customers_and_keeps_the_bookings_for_the_owner_to_move():
    async def run():
        from datetime import datetime, timedelta
        from zoneinfo import ZoneInfo
        pool, out, salon = await build(DSN)
        await pool.execute("UPDATE salons SET owner_wa_id=$1", OWNER)
        nbo = ZoneInfo("Africa/Nairobi")
        staff = await pool.fetchval("SELECT id::text FROM staff")
        svc = await pool.fetchval("SELECT id::text FROM services WHERE name='Fade'")
        a = await pool.fetchval("INSERT INTO clients (salon_id, wa_id, lang) VALUES ($1::uuid,$2,'en') RETURNING id::text", salon, A)
        morrow = (datetime.now(nbo) + timedelta(days=1)).date()
        await pool.execute(
            "INSERT INTO bookings (salon_id, staff_id, service_id, client_id, starts_at, ends_at, status) "
            "VALUES ($1::uuid,$2::uuid,$3::uuid,$4::uuid,$5,$6,'confirmed')",
            salon, staff, svc, a, datetime.combine(morrow, time(10, 0), nbo), datetime.combine(morrow, time(10, 45), nbo))
        async with client() as c:
            await post(c, payload(OWNER, f"block {morrow.strftime('%A')}", mid="b1"))
            assert "have been told" in out.bodies(OWNER)[-1]
            assert "have to close" in out.bodies(A)[-1]
            assert await pool.fetchval("SELECT status FROM bookings") == "confirmed"
        await pool.close()
    asyncio.run(run())


@needs_db
def test_late_with_no_match_or_no_bookings_answers_clearly():
    async def run():
        pool, out, salon = await build(DSN)
        await pool.execute("UPDATE salons SET owner_wa_id=$1", OWNER)
        async with client() as c:
            await post(c, payload(OWNER, "late Nobody 30", mid="l1"))
            assert "No stylist" in out.bodies(OWNER)[-1]
            await post(c, payload(OWNER, "late Brian 30", mid="l2"))
            assert "no upcoming bookings" in out.bodies(OWNER)[-1].lower()
            await post(c, payload(OWNER, "late Brian 0", mid="l3"))
            assert "between 1 and 480" in out.bodies(OWNER)[-1]
        await pool.close()
    asyncio.run(run())


@needs_db
def test_only_the_owner_has_commands_and_owner_messages_do_not_enter_customer_flow():
    async def run():
        pool, out, salon = await build(DSN)
        await pool.execute("UPDATE salons SET owner_wa_id=$1", OWNER)
        day = open_day().isoformat()
        out.script["prices?"] = {"intent": "price_list"}
        async with client() as c:
            await post(c, payload(B, f"block {day}", mid="b1"))
            assert await pool.fetchval("SELECT count(*) FROM time_off") == 0
            await post(c, payload(B, "cancel 1", mid="b2"))
            assert await pool.fetchval("SELECT count(*) FROM bookings") == 0
            await post(c, payload(OWNER, "prices?", mid="o1"))
            assert "Fade" in out.bodies(OWNER)[-1] and "KES 500" in out.bodies(OWNER)[-1]
            assert await pool.fetchval("SELECT count(*) FROM bookings") == 0
            await post(c, payload(OWNER, "block", mid="o2"))
            assert "which day" in out.bodies(OWNER)[-1].lower()
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
            assert await owner.daily_summaries(ctx) == 0
            monkeypatch.setattr(owner, "SUMMARY_HOUR", 0)
            assert await owner.daily_summaries(ctx) == 1
            assert await owner.daily_summaries(ctx) == 0
        body = out.bodies(OWNER)[-1]
        assert "Deposits collected today: KES 200 (1)." in body
        await pool.close()
    asyncio.run(run())


@needs_db
def test_owner_sets_the_deposit_and_sees_whether_a_till_takes_it():
    async def run():
        pool, out, salon = await build(DSN)
        await pool.execute("UPDATE salons SET owner_wa_id=$1", OWNER)
        async with client() as c:
            await post(c, payload(OWNER, "deposit 200", mid="d1"))
            assert await pool.fetchval("SELECT deposit_kes FROM salons") == 200
            assert "KES 200" in out.bodies(OWNER)[-1] and "till" in out.bodies(OWNER)[-1].lower()
            await post(c, payload(OWNER, "amana 300", mid="d2"))
            assert await pool.fetchval("SELECT deposit_kes FROM salons") == 300
            assert "Amana imewekwa" in out.bodies(OWNER)[-1]
            await post(c, payload(OWNER, "deposit much", mid="d3"))
            assert "0 to 100000" in out.bodies(OWNER)[-1]
            assert await pool.fetchval("SELECT deposit_kes FROM salons") == 300
            await post(c, payload(OWNER, "deposit 0", mid="d4"))
            assert "off" in out.bodies(OWNER)[-1].lower()
            assert await pool.fetchval("SELECT deposit_kes FROM salons") == 0
        await pool.close()
    asyncio.run(run())


@needs_db
def test_owner_cannot_move_a_booking_onto_a_closed_day():
    async def run():
        from datetime import datetime, timedelta
        from zoneinfo import ZoneInfo
        pool, out, salon = await build(DSN)
        await pool.execute("UPDATE salons SET owner_wa_id=$1", OWNER)
        nbo = ZoneInfo("Africa/Nairobi")
        staff = await pool.fetchval("SELECT id::text FROM staff")
        svc = await pool.fetchval("SELECT id::text FROM services WHERE name='Fade'")
        a = await pool.fetchval("INSERT INTO clients (salon_id, wa_id, lang) VALUES ($1::uuid,$2,'en') RETURNING id::text", salon, A)
        morrow = (datetime.now(nbo) + timedelta(days=1)).date()
        await pool.execute(
            "INSERT INTO bookings (salon_id, staff_id, service_id, client_id, starts_at, ends_at, status) "
            "VALUES ($1::uuid,$2::uuid,$3::uuid,$4::uuid,$5,$6,'confirmed')",
            salon, staff, svc, a, datetime.combine(morrow, time(10, 0), nbo), datetime.combine(morrow, time(10, 45), nbo))
        async with client() as c:
            await post(c, payload(OWNER, "move 254711000001 sunday 10:00", mid="m1"))
            assert "closed" in out.bodies(OWNER)[-1].lower()
            assert await pool.fetchval("SELECT count(*) FROM bookings WHERE starts_at::date=$1", morrow) == 1
        await pool.close()
    asyncio.run(run())
