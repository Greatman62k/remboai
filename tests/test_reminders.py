import pytest
from datetime import datetime, timedelta
from zoneinfo import ZoneInfo

NBO = ZoneInfo("Africa/Nairobi")

@pytest.mark.asyncio
async def test_schedule_creates_two_jobs():
    import os
    dsn = os.environ.get("TEST_DATABASE_URL")
    if not dsn:
        pytest.skip("needs TEST_DATABASE_URL")
    from app.db import make_pool, migrate
    from app.reminders import schedule_reminders
    pool = await make_pool(dsn)
    await pool.execute("DROP SCHEMA public CASCADE; CREATE SCHEMA public;")
    await migrate(pool)
    # create salon, client, booking far in future
    salon = await pool.fetchval("INSERT INTO salons (name, wa_phone_number_id) VALUES ('Test','PID1') RETURNING id::text")
    staff = await pool.fetchval("INSERT INTO staff (salon_id, name) VALUES ($1::uuid,'A') RETURNING id::text", salon)
    svc = await pool.fetchval("INSERT INTO services (salon_id, name, duration_min, price_kes) VALUES ($1::uuid,'Fade',30,500) RETURNING id::text", salon)
    client = await pool.fetchval("INSERT INTO clients (salon_id, wa_id) VALUES ($1::uuid,'254700000001') RETURNING id::text", salon)
    starts = datetime.now(NBO) + timedelta(days=2)
    ends = starts + timedelta(minutes=30)
    booking = await pool.fetchval("INSERT INTO bookings (salon_id, staff_id, service_id, client_id, starts_at, ends_at, status) VALUES ($1::uuid,$2::uuid,$3::uuid,$4::uuid,$5,$6,'confirmed') RETURNING id::text", salon, staff, svc, client, starts, ends)
    # add templates
    await pool.execute("INSERT INTO reminder_templates (salon_id, name, body, approved) VALUES ($1::uuid,'reminder_24h','Hi {salon} {when}',true)", salon)
    await pool.execute("INSERT INTO reminder_templates (salon_id, name, body, approved) VALUES ($1::uuid,'reminder_2h','Soon {when}',true)", salon)
    await schedule_reminders(pool, booking)
    count = await pool.fetchval("SELECT count(*) FROM reminder_jobs WHERE booking_id=$1::uuid", booking)
    assert count == 2
    # idempotent
    await schedule_reminders(pool, booking)
    assert await pool.fetchval("SELECT count(*) FROM reminder_jobs WHERE booking_id=$1::uuid", booking) == 2
    await pool.close()

@pytest.mark.asyncio
async def test_schedule_skips_if_soon():
    import os
    dsn = os.environ.get("TEST_DATABASE_URL")
    if not dsn:
        pytest.skip("needs TEST_DATABASE_URL")
    from app.db import make_pool, migrate
    from app.reminders import schedule_reminders
    pool = await make_pool(dsn)
    await pool.execute("DROP SCHEMA public CASCADE; CREATE SCHEMA public;")
    await migrate(pool)
    salon = await pool.fetchval("INSERT INTO salons (name, wa_phone_number_id) VALUES ('Test','PID2') RETURNING id::text")
    staff = await pool.fetchval("INSERT INTO staff (salon_id, name) VALUES ($1::uuid,'A') RETURNING id::text", salon)
    svc = await pool.fetchval("INSERT INTO services (salon_id, name, duration_min, price_kes) VALUES ($1::uuid,'Fade',30,500) RETURNING id::text", salon)
    client = await pool.fetchval("INSERT INTO clients (salon_id, wa_id) VALUES ($1::uuid,'254700000002') RETURNING id::text", salon)
    starts = datetime.now(NBO) + timedelta(hours=1)
    ends = starts + timedelta(minutes=30)
    booking = await pool.fetchval("INSERT INTO bookings (salon_id, staff_id, service_id, client_id, starts_at, ends_at, status) VALUES ($1::uuid,$2::uuid,$3::uuid,$4::uuid,$5,$6,'confirmed') RETURNING id::text", salon, staff, svc, client, starts, ends)
    await pool.execute("INSERT INTO reminder_templates (salon_id, name, body, approved) VALUES ($1::uuid,'reminder_24h','Hi',true)", salon)
    await schedule_reminders(pool, booking)
    assert await pool.fetchval("SELECT count(*) FROM reminder_jobs") == 0
    await pool.close()

@pytest.mark.asyncio
async def test_cancel_reminders():
    import os
    dsn = os.environ.get("TEST_DATABASE_URL")
    if not dsn:
        pytest.skip("needs TEST_DATABASE_URL")
    from app.db import make_pool, migrate
    from app.reminders import schedule_reminders, cancel_reminders
    pool = await make_pool(dsn)
    await pool.execute("DROP SCHEMA public CASCADE; CREATE SCHEMA public;")
    await migrate(pool)
    salon = await pool.fetchval("INSERT INTO salons (name, wa_phone_number_id) VALUES ('Test','PID3') RETURNING id::text")
    staff = await pool.fetchval("INSERT INTO staff (salon_id, name) VALUES ($1::uuid,'A') RETURNING id::text", salon)
    svc = await pool.fetchval("INSERT INTO services (salon_id, name, duration_min, price_kes) VALUES ($1::uuid,'Fade',30,500) RETURNING id::text", salon)
    client = await pool.fetchval("INSERT INTO clients (salon_id, wa_id) VALUES ($1::uuid,'254700000003') RETURNING id::text", salon)
    starts = datetime.now(NBO) + timedelta(days=2)
    ends = starts + timedelta(minutes=30)
    booking = await pool.fetchval("INSERT INTO bookings (salon_id, staff_id, service_id, client_id, starts_at, ends_at, status) VALUES ($1::uuid,$2::uuid,$3::uuid,$4::uuid,$5,$6,'confirmed') RETURNING id::text", salon, staff, svc, client, starts, ends)
    await pool.execute("INSERT INTO reminder_templates (salon_id, name, body, approved) VALUES ($1::uuid,'reminder_24h','Hi',true)", salon)
    await schedule_reminders(pool, booking)
    assert await pool.fetchval("SELECT count(*) FROM reminder_jobs WHERE status='pending'") == 1
    await cancel_reminders(pool, booking)
    assert await pool.fetchval("SELECT count(*) FROM reminder_jobs WHERE status='pending'") == 0
    assert await pool.fetchval("SELECT count(*) FROM reminder_jobs WHERE status='cancelled'") == 1
    await pool.close()

@pytest.mark.asyncio
async def test_opt_out_blocks():
    import os
    dsn = os.environ.get("TEST_DATABASE_URL")
    if not dsn:
        pytest.skip("needs TEST_DATABASE_URL")
    from app.db import make_pool, migrate
    from app.reminders import schedule_reminders
    pool = await make_pool(dsn)
    await pool.execute("DROP SCHEMA public CASCADE; CREATE SCHEMA public;")
    await migrate(pool)
    salon = await pool.fetchval("INSERT INTO salons (name, wa_phone_number_id) VALUES ('Test','PID4') RETURNING id::text")
    staff = await pool.fetchval("INSERT INTO staff (salon_id, name) VALUES ($1::uuid,'A') RETURNING id::text", salon)
    svc = await pool.fetchval("INSERT INTO services (salon_id, name, duration_min, price_kes) VALUES ($1::uuid,'Fade',30,500) RETURNING id::text", salon)
    client = await pool.fetchval("INSERT INTO clients (salon_id, wa_id, reminder_opt_out) VALUES ($1::uuid,'254700000004',true) RETURNING id::text", salon)
    starts = datetime.now(NBO) + timedelta(days=2)
    ends = starts + timedelta(minutes=30)
    booking = await pool.fetchval("INSERT INTO bookings (salon_id, staff_id, service_id, client_id, starts_at, ends_at, status) VALUES ($1::uuid,$2::uuid,$3::uuid,$4::uuid,$5,$6,'confirmed') RETURNING id::text", salon, staff, svc, client, starts, ends)
    await pool.execute("INSERT INTO reminder_templates (salon_id, name, body, approved) VALUES ($1::uuid,'reminder_24h','Hi',true)", salon)
    await schedule_reminders(pool, booking)
    assert await pool.fetchval("SELECT count(*) FROM reminder_jobs") == 0
    await pool.close()
