"""Waitlist fill: a cancelled slot goes to waiters in join order, Yes books it,
No/expiry/a taken slot moves the offer on. Real app, real Postgres."""
import os
from datetime import datetime, timedelta
from types import SimpleNamespace
from zoneinfo import ZoneInfo

import pytest

from app import waitlist
from tests.harness import build

DSN = os.environ.get("TEST_DATABASE_URL")
needs_db = pytest.mark.skipif(not DSN, reason="needs TEST_DATABASE_URL")
NBO = ZoneInfo("Africa/Nairobi")
A, B, C = "254711000011", "254711000022", "254711000033"


def chat_for(box: list, to: str):
    async def text(body: str):
        box.append(body)
    return SimpleNamespace(to=to, text=text)


async def setup(pool, salon, service_day, deposit=0):
    from app.main import app
    ctx = app.state.ctx
    await pool.execute("UPDATE salons SET deposit_kes=$2 WHERE id=$1::uuid", salon, deposit)
    ids = {}
    for phone in (A, B, C):
        ids[phone] = await pool.fetchval(
            "INSERT INTO clients (salon_id, wa_id, lang) VALUES ($1::uuid,$2,'en') RETURNING id::text", salon, phone)
    staff = await pool.fetchval("SELECT id::text FROM staff WHERE salon_id=$1::uuid", salon)
    svc = await pool.fetchval("SELECT id::text FROM services WHERE salon_id=$1::uuid AND name='Fade'", salon)
    salon_row = await pool.fetchrow("SELECT *, id::text AS sid FROM salons WHERE id=$1::uuid", salon)
    salon_d = dict(salon_row)
    salon_d["id"] = salon_row["sid"]
    starts = datetime.now(NBO).replace(hour=10, minute=0, second=0, microsecond=0) + timedelta(days=service_day)
    ends = starts + timedelta(minutes=45)
    return ctx, ids, staff, svc, salon_d, starts, ends


@needs_db
@pytest.mark.asyncio
async def test_join_is_idempotent():
    pool, _, salon = await build(DSN)
    ctx, ids, _, svc, _, _, _ = await setup(pool, salon, 2)
    day = (datetime.now(NBO) + timedelta(days=2)).date()
    assert await waitlist.join(pool, salon, ids[B], svc, day) == "joined"
    assert await waitlist.join(pool, salon, ids[B], svc, day) == "already"
    assert await pool.fetchval("SELECT count(*) FROM waitlist") == 1
    await pool.close()


@needs_db
@pytest.mark.asyncio
async def test_cancel_offers_oldest_first_and_yes_books():
    from app import owner
    pool, out, salon = await build(DSN)
    ctx, ids, staff, svc, salon_d, starts, ends = await setup(pool, salon, 2)
    day = starts.date()
    await waitlist.join(pool, salon, ids[B], svc, day)
    await waitlist.join(pool, salon, ids[C], svc, day)
    bid = await pool.fetchval(
        "INSERT INTO bookings (salon_id, staff_id, service_id, client_id, starts_at, ends_at, status) "
        "VALUES ($1::uuid,$2::uuid,$3::uuid,$4::uuid,$5,$6,'confirmed') RETURNING id::text",
        salon, staff, svc, ids[A], starts, ends)
    assert await owner.cancel_booking(ctx, salon_d, bid, NBO) is not None
    first = await pool.fetchrow("SELECT client_id::text AS c, status FROM waitlist ORDER BY created_at")
    assert first["c"] == ids[B] and first["status"] == "offered"
    assert await pool.fetchval("SELECT status FROM waitlist WHERE client_id=$1::uuid", ids[C]) == "waiting"
    assert out.to(B), "the first waiter gets the offer"
    box: list = []
    wid = await pool.fetchval("SELECT id::text FROM waitlist WHERE client_id=$1::uuid", ids[B])
    assert await waitlist.handle_tap(ctx, salon_d, {"id": ids[B], "name": "Bee"}, {}, chat_for(box, B), "en", NBO, f"wait-yes:{wid}")
    assert await pool.fetchval("SELECT status FROM waitlist WHERE id=$1::uuid", wid) == "booked"
    got = await pool.fetchrow("SELECT status, starts_at FROM bookings WHERE client_id=$1::uuid AND status='confirmed'", ids[B])
    assert got is not None and got["starts_at"] == starts
    assert any("booked" in m.lower() for m in box)
    await pool.close()


@needs_db
@pytest.mark.asyncio
async def test_offer_uses_salon_day_and_time_when_slot_crosses_utc_midnight():
    pool, out, salon = await build(DSN)
    ctx, ids, staff, svc, salon_d, _starts, _ends = await setup(pool, salon, 2)
    starts = datetime.now(NBO).replace(hour=0, minute=30, second=0, microsecond=0) + timedelta(days=1)
    ends = starts + timedelta(minutes=45)
    await waitlist.join(pool, salon, ids[B], svc, starts.date())
    bid = await pool.fetchval(
        "INSERT INTO bookings (salon_id, staff_id, service_id, client_id, starts_at, ends_at, status) "
        "VALUES ($1::uuid,$2::uuid,$3::uuid,$4::uuid,$5,$6,'cancelled') RETURNING id::text",
        salon, staff, svc, ids[A], starts, ends)

    await waitlist.offer_for_booking(ctx, salon_d, bid)

    assert await pool.fetchval("SELECT status FROM waitlist WHERE client_id=$1::uuid", ids[B]) == "offered"
    assert "00:30" in out.bodies(B)[-1]
    await pool.close()


@needs_db
@pytest.mark.asyncio
async def test_no_taps_yes_after_expiry_passes_to_next():
    pool, out, salon = await build(DSN)
    ctx, ids, staff, svc, salon_d, starts, ends = await setup(pool, salon, 3)
    day = starts.date()
    await waitlist.join(pool, salon, ids[B], svc, day)
    await waitlist.join(pool, salon, ids[C], svc, day)
    await waitlist.offer_freed_slot(ctx, salon_d, staff, "Brian", svc, "Fade", starts, ends)
    wid_b = await pool.fetchval("SELECT id::text FROM waitlist WHERE client_id=$1::uuid", ids[B])
    await pool.execute("UPDATE waitlist SET offer_expires_at=now() - make_interval(mins => 1) WHERE id=$1::uuid", wid_b)
    box: list = []
    assert await waitlist.handle_tap(ctx, salon_d, {"id": ids[B]}, {}, chat_for(box, B), "en", NBO, f"wait-yes:{wid_b}")
    assert await pool.fetchval("SELECT status FROM waitlist WHERE id=$1::uuid", wid_b) == "expired"
    assert any("expired" in m.lower() for m in box)
    assert await pool.fetchval("SELECT status FROM waitlist WHERE client_id=$1::uuid", ids[C]) == "offered"
    assert out.to(C), "the next waiter gets the offer after expiry"
    await pool.close()


@needs_db
@pytest.mark.asyncio
async def test_decline_passes_to_next():
    pool, out, salon = await build(DSN)
    ctx, ids, staff, svc, salon_d, starts, ends = await setup(pool, salon, 4)
    day = starts.date()
    await waitlist.join(pool, salon, ids[B], svc, day)
    await waitlist.join(pool, salon, ids[C], svc, day)
    await waitlist.offer_freed_slot(ctx, salon_d, staff, "Brian", svc, "Fade", starts, ends)
    wid_b = await pool.fetchval("SELECT id::text FROM waitlist WHERE client_id=$1::uuid", ids[B])
    box: list = []
    assert await waitlist.handle_tap(ctx, salon_d, {"id": ids[B]}, {}, chat_for(box, B), "en", NBO, f"wait-no:{wid_b}")
    assert await pool.fetchval("SELECT status FROM waitlist WHERE id=$1::uuid", wid_b) == "declined"
    assert await pool.fetchval("SELECT status FROM waitlist WHERE client_id=$1::uuid", ids[C]) == "offered"
    await pool.close()


@needs_db
@pytest.mark.asyncio
async def test_taken_slot_keeps_rest_waiting():
    pool, _, salon = await build(DSN)
    ctx, ids, staff, svc, salon_d, starts, ends = await setup(pool, salon, 5)
    day = starts.date()
    await waitlist.join(pool, salon, ids[B], svc, day)
    await waitlist.join(pool, salon, ids[C], svc, day)
    await waitlist.offer_freed_slot(ctx, salon_d, staff, "Brian", svc, "Fade", starts, ends)
    wid_b = await pool.fetchval("SELECT id::text FROM waitlist WHERE client_id=$1::uuid", ids[B])
    await pool.execute(
        "INSERT INTO bookings (salon_id, staff_id, service_id, client_id, starts_at, ends_at, status) "
        "VALUES ($1::uuid,$2::uuid,$3::uuid,$4::uuid,$5,$6,'confirmed')",
        salon, staff, svc, ids[A], starts, ends)
    box: list = []
    assert await waitlist.handle_tap(ctx, salon_d, {"id": ids[B]}, {}, chat_for(box, B), "en", NBO, f"wait-yes:{wid_b}")
    assert any("taken" in m.lower() for m in box)
    assert await pool.fetchval("SELECT status FROM waitlist WHERE client_id=$1::uuid", ids[C]) == "waiting"
    await pool.close()


@needs_db
@pytest.mark.asyncio
async def test_yes_with_deposit_holds_for_payment():
    pool, out, salon = await build(DSN)
    ctx, ids, staff, svc, salon_d, starts, ends = await setup(pool, salon, 2, deposit=200)
    day = starts.date()
    await waitlist.join(pool, salon, ids[B], svc, day)
    await waitlist.offer_freed_slot(ctx, salon_d, staff, "Brian", svc, "Fade", starts, ends)
    wid_b = await pool.fetchval("SELECT id::text FROM waitlist WHERE client_id=$1::uuid", ids[B])
    box: list = []
    assert await waitlist.handle_tap(ctx, salon_d, {"id": ids[B]}, {}, chat_for(box, B), "en", NBO, f"wait-yes:{wid_b}")
    assert await pool.fetchval("SELECT status FROM waitlist WHERE id=$1::uuid", wid_b) == "booked"
    assert await pool.fetchval("SELECT status FROM bookings WHERE client_id=$1::uuid", ids[B]) == "held"
    assert out.stk, "a deposit prompt goes out for the held waitlist booking"
    await pool.close()


@needs_db
@pytest.mark.asyncio
async def test_sweep_expires_stale_offers():
    pool, _, salon = await build(DSN)
    ctx, ids, staff, svc, salon_d, starts, ends = await setup(pool, salon, 6)
    day = starts.date()
    await waitlist.join(pool, salon, ids[B], svc, day)
    await waitlist.join(pool, salon, ids[C], svc, day)
    await waitlist.offer_freed_slot(ctx, salon_d, staff, "Brian", svc, "Fade", starts, ends)
    wid_b = await pool.fetchval("SELECT id::text FROM waitlist WHERE client_id=$1::uuid", ids[B])
    await pool.execute("UPDATE waitlist SET offer_expires_at=now() - make_interval(mins => 1) WHERE id=$1::uuid", wid_b)
    assert await waitlist.sweep_expired(ctx) == 1
    assert await pool.fetchval("SELECT status FROM waitlist WHERE id=$1::uuid", wid_b) == "expired"
    assert await pool.fetchval("SELECT status FROM waitlist WHERE client_id=$1::uuid", ids[C]) == "offered"
    await pool.close()


@needs_db
@pytest.mark.asyncio
async def test_cancel_with_no_waiters_is_quiet():
    from app import owner
    pool, out, salon = await build(DSN)
    ctx, ids, staff, svc, salon_d, starts, ends = await setup(pool, salon, 2)
    bid = await pool.fetchval(
        "INSERT INTO bookings (salon_id, staff_id, service_id, client_id, starts_at, ends_at, status) "
        "VALUES ($1::uuid,$2::uuid,$3::uuid,$4::uuid,$5,$6,'confirmed') RETURNING id::text",
        salon, staff, svc, ids[A], starts, ends)
    before = len(out.sent)
    assert await owner.cancel_booking(ctx, salon_d, bid, NBO) is not None
    assert await pool.fetchval("SELECT count(*) FROM waitlist") == 0
    assert len(out.sent) == before + 1, "only the cancelled customer is told"
    await pool.close()
