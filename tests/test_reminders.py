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
    salon = await pool.fetchval("INSERT INTO salons (name, wa_phone_number_id) VALUES ('Test','PID1') RETURNING id::text")
    staff = await pool.fetchval("INSERT INTO staff (salon_id, name) VALUES ($1::uuid,'A') RETURNING id::text", salon)
    svc = await pool.fetchval("INSERT INTO services (salon_id, name, duration_min, price_kes) VALUES ($1::uuid,'Fade',30,500) RETURNING id::text", salon)
    client = await pool.fetchval("INSERT INTO clients (salon_id, wa_id) VALUES ($1::uuid,'254700000001') RETURNING id::text", salon)
    starts = datetime.now(NBO) + timedelta(days=2)
    ends = starts + timedelta(minutes=30)
    booking = await pool.fetchval("INSERT INTO bookings (salon_id, staff_id, service_id, client_id, starts_at, ends_at, status) VALUES ($1::uuid,$2::uuid,$3::uuid,$4::uuid,$5,$6,'confirmed') RETURNING id::text", salon, staff, svc, client, starts, ends)
    await pool.execute("INSERT INTO reminder_templates (salon_id, name, body, approved) VALUES ($1::uuid,'reminder_24h','Hi {salon} {when}',true)", salon)
    await pool.execute("INSERT INTO reminder_templates (salon_id, name, body, approved) VALUES ($1::uuid,'reminder_2h','Soon {when}',true)", salon)
    await schedule_reminders(pool, booking)
    count = await pool.fetchval("SELECT count(*) FROM reminder_jobs WHERE booking_id=$1::uuid", booking)
    assert count == 2
    await schedule_reminders(pool, booking)
    assert await pool.fetchval("SELECT count(*) FROM reminder_jobs WHERE booking_id=$1::uuid", booking) == 2
    await pool.close()

@pytest.mark.asyncio
async def test_near_whatsapp_booking_requires_an_approved_soon_template():
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
    await pool.execute("INSERT INTO reminder_templates (salon_id, name, body, approved) VALUES ($1::uuid,'reminder_soon','Your appointment is soon: {service} at {time}',true)", salon)
    await schedule_reminders(pool, booking)
    assert await pool.fetchval("SELECT template_name FROM reminder_jobs WHERE booking_id=$1::uuid", booking) == "reminder_soon"
    await pool.close()

@pytest.mark.asyncio
async def test_a_telegram_booking_gets_one_eta_30_minutes_before():
    import os
    dsn = os.environ.get("TEST_DATABASE_URL")
    if not dsn:
        pytest.skip("needs TEST_DATABASE_URL")
    from app.db import make_pool, migrate
    from app.reminders import schedule_reminders, SOON_TEMPLATE
    pool = await make_pool(dsn)
    await pool.execute("DROP SCHEMA public CASCADE; CREATE SCHEMA public;")
    await migrate(pool)
    salon = await pool.fetchval("INSERT INTO salons (name, tg_bot_id) VALUES ('Soon Salon','tg:111') RETURNING id::text")
    staff = await pool.fetchval("INSERT INTO staff (salon_id, name) VALUES ($1::uuid,'A') RETURNING id::text", salon)
    svc = await pool.fetchval("INSERT INTO services (salon_id, name, duration_min, price_kes) VALUES ($1::uuid,'Fade',30,500) RETURNING id::text", salon)
    client = await pool.fetchval("INSERT INTO clients (salon_id, wa_id, lang) VALUES ($1::uuid,'tg:5001','en') RETURNING id::text", salon)
    starts = datetime.now(NBO) + timedelta(minutes=40)
    booking = await pool.fetchval(
        "INSERT INTO bookings (salon_id, staff_id, service_id, client_id, starts_at, ends_at, status) "
        "VALUES ($1::uuid,$2::uuid,$3::uuid,$4::uuid,$5,$6,'confirmed') RETURNING id::text",
        salon, staff, svc, client, starts, starts + timedelta(minutes=30))
    await schedule_reminders(pool, booking)
    row = await pool.fetchrow("SELECT template_name, run_at, booking_id::text FROM reminder_jobs WHERE booking_id=$1::uuid", booking)
    assert row["template_name"] == SOON_TEMPLATE
    assert row["booking_id"] == booking
    assert abs((row["run_at"] - (starts - timedelta(minutes=30))).total_seconds()) < 1
    await pool.close()

@pytest.mark.asyncio
async def test_soon_reminder_sends_to_telegram_without_a_whatsapp_template_row():
    import os
    dsn = os.environ.get("TEST_DATABASE_URL")
    if not dsn:
        pytest.skip("needs TEST_DATABASE_URL")
    from app.reminders import run_reminders, SOON_TEMPLATE
    from tests.harness import build
    pool, out, salon = await build(dsn)
    from app.main import app
    staff = await pool.fetchval("SELECT id::text FROM staff LIMIT 1")
    svc = await pool.fetchval("SELECT id::text FROM services WHERE name='Fade' LIMIT 1")
    client = await pool.fetchval("INSERT INTO clients (salon_id, wa_id, lang) VALUES ($1::uuid,'tg:5002','en') RETURNING id::text", salon)
    starts = datetime.now(NBO) + timedelta(minutes=10)
    booking = await pool.fetchval(
        "INSERT INTO bookings (salon_id, staff_id, service_id, client_id, starts_at, ends_at, status) "
        "VALUES ($1::uuid,$2::uuid,$3::uuid,$4::uuid,$5,$6,'confirmed') RETURNING id::text",
        salon, staff, svc, client, starts, starts + timedelta(minutes=45))
    await pool.execute(
        "INSERT INTO reminder_jobs (booking_id, salon_id, client_id, wa_id, run_at, template_name) "
        "VALUES ($1::uuid,$2::uuid,$3::uuid,'tg:5002',now()-interval '1 second',$4)",
        booking, salon, client, SOON_TEMPLATE)
    await run_reminders(app.state.ctx)
    assert await pool.fetchval("SELECT status FROM reminder_jobs WHERE booking_id=$1::uuid", booking) == "sent"
    assert len(out.tg_bodies("5002")) == 1
    assert "Fade" in out.tg_bodies("5002")[0] and "minutes" in out.tg_bodies("5002")[0]
    await pool.close()

@pytest.mark.asyncio
async def test_a_booking_within_30_minutes_uses_its_confirmation_without_a_duplicate_eta():
    import os
    dsn = os.environ.get("TEST_DATABASE_URL")
    if not dsn:
        pytest.skip("needs TEST_DATABASE_URL")
    from app.db import make_pool, migrate
    from app.reminders import schedule_reminders
    pool = await make_pool(dsn)
    await pool.execute("DROP SCHEMA public CASCADE; CREATE SCHEMA public;")
    await migrate(pool)
    salon = await pool.fetchval("INSERT INTO salons (name, tg_bot_id) VALUES ('Immediate Salon','tg:111') RETURNING id::text")
    staff = await pool.fetchval("INSERT INTO staff (salon_id, name) VALUES ($1::uuid,'A') RETURNING id::text", salon)
    svc = await pool.fetchval("INSERT INTO services (salon_id, name, duration_min, price_kes) VALUES ($1::uuid,'Fade',30,500) RETURNING id::text", salon)
    client = await pool.fetchval("INSERT INTO clients (salon_id, wa_id) VALUES ($1::uuid,'tg:5003') RETURNING id::text", salon)
    starts = datetime.now(NBO) + timedelta(minutes=10)
    booking = await pool.fetchval(
        "INSERT INTO bookings (salon_id, staff_id, service_id, client_id, starts_at, ends_at, status) "
        "VALUES ($1::uuid,$2::uuid,$3::uuid,$4::uuid,$5,$6,'confirmed') RETURNING id::text",
        salon, staff, svc, client, starts, starts + timedelta(minutes=30))
    await schedule_reminders(pool, booking)
    assert await pool.fetchval("SELECT count(*) FROM reminder_jobs WHERE booking_id=$1::uuid", booking) == 0
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
async def test_a_booking_two_hours_out_still_gets_its_two_hour_reminder():
    import os
    dsn = os.environ.get("TEST_DATABASE_URL")
    if not dsn:
        pytest.skip("needs TEST_DATABASE_URL")
    from app.db import make_pool, migrate
    from app.reminders import schedule_reminders
    pool = await make_pool(dsn)
    await pool.execute("DROP SCHEMA public CASCADE; CREATE SCHEMA public;")
    await migrate(pool)
    salon = await pool.fetchval("INSERT INTO salons (name, wa_phone_number_id) VALUES ('Test','PID5') RETURNING id::text")
    staff = await pool.fetchval("INSERT INTO staff (salon_id, name) VALUES ($1::uuid,'A') RETURNING id::text", salon)
    svc = await pool.fetchval("INSERT INTO services (salon_id, name, duration_min, price_kes) VALUES ($1::uuid,'Fade',30,500) RETURNING id::text", salon)
    client = await pool.fetchval("INSERT INTO clients (salon_id, wa_id) VALUES ($1::uuid,'254700000005') RETURNING id::text", salon)
    starts = datetime.now(NBO) + timedelta(hours=2, minutes=30)
    ends = starts + timedelta(minutes=30)
    booking = await pool.fetchval("INSERT INTO bookings (salon_id, staff_id, service_id, client_id, starts_at, ends_at, status) VALUES ($1::uuid,$2::uuid,$3::uuid,$4::uuid,$5,$6,'confirmed') RETURNING id::text", salon, staff, svc, client, starts, ends)
    await pool.execute("INSERT INTO reminder_templates (salon_id, name, body, approved) VALUES ($1::uuid,'reminder_24h','Hi',true), ($1::uuid,'reminder_2h','Soon',true)", salon)
    await schedule_reminders(pool, booking)
    assert await pool.fetchval("SELECT template_name FROM reminder_jobs") == "reminder_2h"
    await pool.close()

@pytest.mark.asyncio
async def test_a_hold_keeps_its_reminder_until_it_confirms():
    import os
    dsn = os.environ.get("TEST_DATABASE_URL")
    if not dsn:
        pytest.skip("needs TEST_DATABASE_URL")
    from app.db import make_pool, migrate
    from app.reminders import run_reminders, schedule_reminders
    from tests.harness import build
    pool, out, salon = await build(dsn)
    from app.main import app
    ctx = app.state.ctx
    staff = await pool.fetchval("SELECT id::text FROM staff")
    svc = await pool.fetchval("SELECT id::text FROM services WHERE name='Fade'")
    client = await pool.fetchval("INSERT INTO clients (salon_id, wa_id) VALUES ($1::uuid,'254700000006') RETURNING id::text", salon)
    starts = datetime.now(NBO) + timedelta(days=2)
    booking = await pool.fetchval(
        "INSERT INTO bookings (salon_id, staff_id, service_id, client_id, starts_at, ends_at, status) VALUES ($1::uuid,$2::uuid,$3::uuid,$4::uuid,$5,$6,'held') RETURNING id::text",
        salon, staff, svc, client, starts, starts + timedelta(minutes=45))
    await pool.execute("INSERT INTO reminder_templates (salon_id, name, body, approved) VALUES ($1::uuid,'reminder_2h','Soon {when}',true)", salon)
    await schedule_reminders(pool, booking)
    await pool.execute("UPDATE reminder_jobs SET run_at=now() - interval '1 minute' WHERE booking_id=$1::uuid", booking)
    await run_reminders(ctx)
    assert await pool.fetchval("SELECT status FROM reminder_jobs") == "pending"
    await pool.execute("UPDATE bookings SET status='confirmed' WHERE id=$1::uuid", booking)
    await pool.execute("UPDATE reminder_jobs SET run_at=now() - interval '1 minute' WHERE booking_id=$1::uuid", booking)
    await run_reminders(ctx)
    assert await pool.fetchval("SELECT status FROM reminder_jobs") == "sent"
    await pool.close()

@pytest.mark.asyncio
async def test_a_visit_gets_thanked_afterwards_only_where_the_salon_opted_in():
    import os
    dsn = os.environ.get("TEST_DATABASE_URL")
    if not dsn:
        pytest.skip("needs TEST_DATABASE_URL")
    from app.db import make_pool, migrate
    from app.reminders import run_reminders, schedule_reminders, THANKS_TEMPLATE
    from tests.harness import build
    pool, out, salon = await build(dsn)
    from app.main import app
    ctx = app.state.ctx
    staff = await pool.fetchval("SELECT id::text FROM staff")
    svc = await pool.fetchval("SELECT id::text FROM services WHERE name='Fade'")
    client = await pool.fetchval("INSERT INTO clients (salon_id, wa_id) VALUES ($1::uuid,'254700000007') RETURNING id::text", salon)
    starts = datetime.now(NBO) + timedelta(days=2)
    booking = await pool.fetchval(
        "INSERT INTO bookings (salon_id, staff_id, service_id, client_id, starts_at, ends_at, status) VALUES ($1::uuid,$2::uuid,$3::uuid,$4::uuid,$5,$6,'confirmed') RETURNING id::text",
        salon, staff, svc, client, starts, starts + timedelta(minutes=45))
    await schedule_reminders(pool, booking)
    assert await pool.fetchval("SELECT count(*) FROM reminder_jobs") == 0
    await pool.execute("INSERT INTO reminder_templates (salon_id, name, body, approved) VALUES ($1::uuid,$2,'Asante {name}! Karibu tena.',true)", salon, THANKS_TEMPLATE)
    await schedule_reminders(pool, booking)
    assert await pool.fetchval("SELECT template_name FROM reminder_jobs") == THANKS_TEMPLATE
    await pool.execute("UPDATE reminder_jobs SET run_at=now() - interval '1 minute' WHERE booking_id=$1::uuid", booking)
    await run_reminders(ctx)
    assert await pool.fetchval("SELECT status FROM reminder_jobs") == "pending"
    await pool.execute("UPDATE bookings SET starts_at=now() - interval '3 hours', ends_at=now() - interval '2 hours' WHERE id=$1::uuid", booking)
    await pool.execute("UPDATE reminder_jobs SET run_at=now() - interval '1 minute' WHERE booking_id=$1::uuid", booking)
    await run_reminders(ctx)
    assert await pool.fetchval("SELECT status FROM reminder_jobs") == "sent"
    assert "Asante" in str(out.sent[-1])
    await pool.close()

@pytest.mark.asyncio
async def test_no_thanks_for_a_visit_that_did_not_happen():
    import os
    dsn = os.environ.get("TEST_DATABASE_URL")
    if not dsn:
        pytest.skip("needs TEST_DATABASE_URL")
    from app.db import make_pool, migrate
    from app.reminders import run_reminders, schedule_reminders, THANKS_TEMPLATE
    from tests.harness import build
    pool, out, salon = await build(dsn)
    from app.main import app
    ctx = app.state.ctx
    staff = await pool.fetchval("SELECT id::text FROM staff")
    svc = await pool.fetchval("SELECT id::text FROM services WHERE name='Fade'")
    client = await pool.fetchval("INSERT INTO clients (salon_id, wa_id) VALUES ($1::uuid,'254700000008') RETURNING id::text", salon)
    starts = datetime.now(NBO) - timedelta(days=1)
    booking = await pool.fetchval(
        "INSERT INTO bookings (salon_id, staff_id, service_id, client_id, starts_at, ends_at, status) VALUES ($1::uuid,$2::uuid,$3::uuid,$4::uuid,$5,$6,'no_show') RETURNING id::text",
        salon, staff, svc, client, starts, starts + timedelta(minutes=45))
    await pool.execute("INSERT INTO reminder_templates (salon_id, name, body, approved) VALUES ($1::uuid,$2,'Asante!',true)", salon, THANKS_TEMPLATE)
    await pool.execute("INSERT INTO reminder_jobs (booking_id, salon_id, client_id, wa_id, run_at, template_name) VALUES ($1::uuid,$2::uuid,$3::uuid,'254700000008',now(),'thanks_visit')", booking, salon, client)
    await run_reminders(ctx)
    assert await pool.fetchval("SELECT status FROM reminder_jobs") == "cancelled"
    await pool.close()

@pytest.mark.asyncio
async def test_a_finished_visit_is_checked_once_and_a_yes_earns_feedback():
    import os
    dsn = os.environ.get("TEST_DATABASE_URL")
    if not dsn:
        pytest.skip("needs TEST_DATABASE_URL")
    from app.reminders import ask_about_visits
    from tests.harness import build, client, payload, post
    pool, out, salon = await build(dsn)
    from app.main import app
    ctx = app.state.ctx
    staff = await pool.fetchval("SELECT id::text FROM staff")
    svc = await pool.fetchval("SELECT id::text FROM services WHERE name='Fade'")
    a = await pool.fetchval("INSERT INTO clients (salon_id, wa_id, lang) VALUES ($1::uuid,'254711000001','en') RETURNING id::text", salon)
    ended = datetime.now(NBO) - timedelta(hours=1)
    booking = await pool.fetchval(
        "INSERT INTO bookings (salon_id, staff_id, service_id, client_id, starts_at, ends_at, status) VALUES ($1::uuid,$2::uuid,$3::uuid,$4::uuid,$5,$6,'confirmed') RETURNING id::text",
        salon, staff, svc, a, ended - timedelta(minutes=45), ended)
    assert await ask_about_visits(pool, ctx) == 1
    assert "attended" in out.bodies("254711000001")[-1]
    assert await ask_about_visits(pool, ctx) == 0
    assert len(out.bodies("254711000001")) == 1
    async with client() as c:
        await post(c, payload("254711000001", reply_id=f"visit-yes:{booking}", mid="v1"))
        assert "How was the service" in out.bodies("254711000001")[-1]
        await post(c, payload("254711000001", "5, Brian was great", mid="v2"))
        assert "Thank you for the feedback" in out.bodies("254711000001")[-1]
    row = await pool.fetchrow("SELECT attended, feedback FROM visit_checks")
    assert (row["attended"], row["feedback"]) == (True, "5, Brian was great")
    await pool.close()

@pytest.mark.asyncio
async def test_a_missed_visit_alerts_the_owner_and_offers_a_way_back():
    import os
    dsn = os.environ.get("TEST_DATABASE_URL")
    if not dsn:
        pytest.skip("needs TEST_DATABASE_URL")
    from app.reminders import ask_about_visits
    from tests.harness import build, client, payload, post
    pool, out, salon = await build(dsn)
    await pool.execute("UPDATE salons SET owner_wa_id='254799999999'")
    from app.main import app
    ctx = app.state.ctx
    staff = await pool.fetchval("SELECT id::text FROM staff")
    svc = await pool.fetchval("SELECT id::text FROM services WHERE name='Fade'")
    a = await pool.fetchval("INSERT INTO clients (salon_id, wa_id, lang) VALUES ($1::uuid,'254711000002','en') RETURNING id::text", salon)
    ended = datetime.now(NBO) - timedelta(hours=1)
    booking = await pool.fetchval(
        "INSERT INTO bookings (salon_id, staff_id, service_id, client_id, starts_at, ends_at, status) VALUES ($1::uuid,$2::uuid,$3::uuid,$4::uuid,$5,$6,'confirmed') RETURNING id::text",
        salon, staff, svc, a, ended - timedelta(minutes=45), ended)
    await ask_about_visits(pool, ctx)
    async with client() as c:
        n_owner = len(out.to("254799999999"))
        await post(c, payload("254711000002", reply_id=f"visit-no:{booking}", mid="w1"))
        assert "owner has been told" in out.bodies("254711000002")[-1]
        assert "NOT attended" in out.bodies("254799999999")[-1] and len(out.to("254799999999")) == n_owner + 1
        n_b = len(out.to("254711000001"))
        await post(c, payload("254711000001", reply_id=f"visit-no:{booking}", mid="w2"))
        assert len(out.to("254711000001")) == n_b
    assert await pool.fetchval("SELECT attended FROM visit_checks") is False
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


@pytest.mark.asyncio
async def test_customer_and_owner_get_one_eta_30_minutes_before_a_long_range_booking():
    import os
    dsn = os.environ.get("TEST_DATABASE_URL")
    if not dsn:
        pytest.skip("needs TEST_DATABASE_URL")
    from app.reminders import run_reminders, schedule_reminders, SOON_TEMPLATE, OWNER_SOON_TEMPLATE
    from tests.harness import build
    pool, out, salon = await build(dsn)
    await pool.execute("UPDATE salons SET owner_wa_id='tg:6001' WHERE id=$1::uuid", salon)
    from app.main import app
    staff = await pool.fetchval("SELECT id::text FROM staff WHERE salon_id=$1::uuid LIMIT 1", salon)
    svc = await pool.fetchval("SELECT id::text FROM services WHERE salon_id=$1::uuid AND name='Fade'", salon)
    client = await pool.fetchval(
        "INSERT INTO clients (salon_id, wa_id, name, lang) VALUES ($1::uuid,'tg:6002','Amina','en') RETURNING id::text", salon)
    starts = datetime.now(NBO) + timedelta(days=1)
    booking = await pool.fetchval(
        "INSERT INTO bookings (salon_id, staff_id, service_id, client_id, starts_at, ends_at, status) "
        "VALUES ($1::uuid,$2::uuid,$3::uuid,$4::uuid,$5,$6,'confirmed') RETURNING id::text",
        salon, staff, svc, client, starts, starts + timedelta(minutes=45))
    await schedule_reminders(pool, booking)
    rows = await pool.fetch(
        "SELECT template_name, wa_id, run_at FROM reminder_jobs WHERE booking_id=$1::uuid ORDER BY template_name", booking)
    assert {(r["template_name"], r["wa_id"]) for r in rows} == {
        (SOON_TEMPLATE, "tg:6002"), (OWNER_SOON_TEMPLATE, "tg:6001")}
    assert all(abs((r["run_at"] - (starts - timedelta(minutes=30))).total_seconds()) < 1 for r in rows)

    # Make the scheduled job due while keeping the appointment in the future.
    await pool.execute("UPDATE bookings SET starts_at=now()+interval '20 minutes', ends_at=now()+interval '65 minutes' WHERE id=$1::uuid", booking)
    await pool.execute("UPDATE reminder_jobs SET run_at=now()-interval '1 second' WHERE booking_id=$1::uuid", booking)
    await run_reminders(app.state.ctx)
    assert "appointment" in out.tg_bodies("6002")[-1]
    owner_msg = out.tg_bodies("6001")[-1]
    assert "Amina" in owner_msg and "Fade" in owner_msg and "remains confirmed" in owner_msg
    assert await pool.fetchval("SELECT count(*) FROM reminder_jobs WHERE booking_id=$1::uuid AND status='sent'", booking) == 2
    await pool.close()


@pytest.mark.asyncio
async def test_owner_cancellation_eta_notice_survives_customer_reminder_opt_out():
    import os
    dsn = os.environ.get("TEST_DATABASE_URL")
    if not dsn:
        pytest.skip("needs TEST_DATABASE_URL")
    from app.reminders import run_reminders, schedule_owner_cancellation, OWNER_CANCELLED_TEMPLATE
    from tests.harness import build
    pool, out, salon = await build(dsn)
    await pool.execute("UPDATE salons SET owner_wa_id='tg:6011' WHERE id=$1::uuid", salon)
    from app.main import app
    staff = await pool.fetchval("SELECT id::text FROM staff WHERE salon_id=$1::uuid LIMIT 1", salon)
    svc = await pool.fetchval("SELECT id::text FROM services WHERE salon_id=$1::uuid AND name='Fade'", salon)
    client = await pool.fetchval(
        "INSERT INTO clients (salon_id, wa_id, name, reminder_opt_out) "
        "VALUES ($1::uuid,'254700000061','Kendi',true) RETURNING id::text", salon)
    starts = datetime.now(NBO) + timedelta(hours=1)
    booking = await pool.fetchval(
        "INSERT INTO bookings (salon_id, staff_id, service_id, client_id, starts_at, ends_at, status) "
        "VALUES ($1::uuid,$2::uuid,$3::uuid,$4::uuid,$5,$6,'cancelled') RETURNING id::text",
        salon, staff, svc, client, starts, starts + timedelta(minutes=45))
    await schedule_owner_cancellation(pool, booking)
    await schedule_owner_cancellation(pool, booking)
    assert await pool.fetchval("SELECT count(*) FROM reminder_jobs WHERE booking_id=$1::uuid AND template_name=$2", booking, OWNER_CANCELLED_TEMPLATE) == 1
    await run_reminders(app.state.ctx)
    assert "Kendi" in out.tg_bodies("6011")[-1] and "cancelled" in out.tg_bodies("6011")[-1]
    assert await pool.fetchval("SELECT status FROM reminder_jobs WHERE booking_id=$1::uuid", booking) == "sent"
    await pool.close()
