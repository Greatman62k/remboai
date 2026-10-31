"""Still-coming 3h reminders: Keep confirms, Cancel frees the slot, silence
releases it toward the waitlist. Real app, real Postgres."""
import os
from datetime import datetime, timedelta
from types import SimpleNamespace
from zoneinfo import ZoneInfo

import pytest

from app import waitlist
from app.reminders import handle_still_tap, release_silent, run_reminders, schedule_reminders
from tests.harness import build

DSN = os.environ.get("TEST_DATABASE_URL")
needs_db = pytest.mark.skipif(not DSN, reason="needs TEST_DATABASE_URL")
NBO = ZoneInfo("Africa/Nairobi")
A, B = "254711000044", "254711000055"


def chat_for(box: list, to: str):
    async def text(body: str):
        box.append(body)
    return SimpleNamespace(to=to, text=text)


async def setup(pool, salon, hours_out, deposit=0):
    from app.main import app
    ctx = app.state.ctx
    await pool.execute("UPDATE salons SET deposit_kes=$2 WHERE id=$1::uuid", salon, deposit)
    aids, bids = None, None
    aids = await pool.fetchval(
        "INSERT INTO clients (salon_id, wa_id, lang) VALUES ($1::uuid,$2,'en') RETURNING id::text", salon, A)
    bids = await pool.fetchval(
        "INSERT INTO clients (salon_id, wa_id, lang) VALUES ($1::uuid,$2,'en') RETURNING id::text", salon, B)
    staff = await pool.fetchval("SELECT id::text FROM staff WHERE salon_id=$1::uuid", salon)
    svc = await pool.fetchval("SELECT id::text FROM services WHERE salon_id=$1::uuid AND name='Fade'", salon)
    salon_row = await pool.fetchrow("SELECT *, id::text AS sid FROM salons WHERE id=$1::uuid", salon)
    salon_d = dict(salon_row)
    salon_d["id"] = salon_row["sid"]
    starts = datetime.now(NBO) + timedelta(hours=hours_out)
    ends = starts + timedelta(minutes=45)
    bid = await pool.fetchval(
        "INSERT INTO bookings (salon_id, staff_id, service_id, client_id, starts_at, ends_at, status) "
        "VALUES ($1::uuid,$2::uuid,$3::uuid,$4::uuid,$5,$6,'confirmed') RETURNING id::text",
        salon, staff, svc, aids, starts, ends)
    return ctx, aids, bids, staff, svc, salon_d, starts, ends, bid


@needs_db
@pytest.mark.asyncio
async def test_schedule_creates_3h_job():
    pool, _, salon = await build(DSN)
    ctx, aids, _, _, _, _, starts, _, bid = await setup(pool, salon, 48)
    for name in ("reminder_24h", "reminder_2h", "reminder_3h"):
        await pool.execute("INSERT INTO reminder_templates (salon_id, name, body, approved) VALUES ($1::uuid,$2,'Hi {salon} {when}',true)", salon, name)
    await schedule_reminders(pool, bid)
    assert await pool.fetchval("SELECT count(*) FROM reminder_jobs WHERE booking_id=$1::uuid", bid) == 3
    await pool.close()


@needs_db
@pytest.mark.asyncio
async def test_still_nudge_carries_buttons():
    pool, out, salon = await build(DSN)
    ctx, aids, _, _, _, _, starts, _, bid = await setup(pool, salon, 4)
    await pool.execute("INSERT INTO reminder_templates (salon_id, name, body, approved) VALUES ($1::uuid,'reminder_3h','Still on for {when}?',true)", salon)
    await schedule_reminders(pool, bid)
    await pool.execute("UPDATE reminder_jobs SET run_at=now() - make_interval(mins => 1) WHERE booking_id=$1::uuid", bid)
    await run_reminders(ctx)
    assert await pool.fetchval("SELECT status FROM reminder_jobs WHERE booking_id=$1::uuid", bid) == "sent"
    interactive = [m for m in out.to(A) if m.get("type") == "interactive"]
    assert interactive and f"still-keep:{bid}" in str(interactive[0])
    await pool.close()


@needs_db
@pytest.mark.asyncio
async def test_keep_confirms_and_blocks_release():
    pool, _, salon = await build(DSN)
    ctx, aids, _, _, _, salon_d, starts, _, bid = await setup(pool, salon, 5)
    await pool.execute(
        "INSERT INTO reminder_jobs (booking_id, salon_id, client_id, wa_id, run_at, template_name, status, sent_at) "
        "SELECT $1::uuid, salon_id, client_id, $2, now(), 'reminder_3h', 'sent', now() FROM bookings WHERE id=$1::uuid", bid, A)
    box: list = []
    assert await handle_still_tap(ctx, salon_d, {"id": aids}, {}, chat_for(box, A), "en", NBO, f"still-keep:{bid}")
    assert await pool.fetchval("SELECT replied FROM reminder_jobs WHERE booking_id=$1::uuid", bid) is True
    assert await pool.fetchval("SELECT status FROM bookings WHERE id=$1::uuid", bid) == "confirmed"
    assert await release_silent(ctx) == 0, "an answered nudge never releases"
    await pool.close()


@needs_db
@pytest.mark.asyncio
async def test_cancel_frees_slot_to_waitlist():
    pool, _, salon = await build(DSN)
    ctx, aids, bids, _, svc, salon_d, starts, _, bid = await setup(pool, salon, 5)
    await waitlist.join(pool, salon, bids, svc, starts.date())
    box: list = []
    assert await handle_still_tap(ctx, salon_d, {"id": aids}, {}, chat_for(box, A), "en", NBO, f"still-cancel:{bid}")
    assert await pool.fetchval("SELECT status FROM bookings WHERE id=$1::uuid", bid) == "cancelled"
    assert await pool.fetchval("SELECT status FROM waitlist WHERE client_id=$1::uuid", bids) == "offered"
    await pool.close()


@needs_db
@pytest.mark.asyncio
async def test_silence_releases_slot_to_waitlist():
    pool, out, salon = await build(DSN)
    ctx, aids, bids, _, svc, salon_d, starts, _, bid = await setup(pool, salon, 1)
    await waitlist.join(pool, salon, bids, svc, starts.date())
    await pool.execute(
        "INSERT INTO reminder_jobs (booking_id, salon_id, client_id, wa_id, run_at, template_name, status, sent_at) "
        "SELECT $1::uuid, salon_id, client_id, $2, now() - make_interval(hours => 2), 'reminder_3h', 'sent', now() - make_interval(hours => 2) "
        "FROM bookings WHERE id=$1::uuid", bid, A)
    assert await release_silent(ctx) == 1
    assert await pool.fetchval("SELECT status FROM bookings WHERE id=$1::uuid", bid) == "cancelled"
    assert await pool.fetchval("SELECT status FROM waitlist WHERE client_id=$1::uuid", bids) == "offered"
    assert any("released" in m.get("text", {}).get("body", "").lower() for m in out.to(A))
    await pool.close()
