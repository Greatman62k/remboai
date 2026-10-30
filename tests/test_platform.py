"""Control hub and lifecycle: the platform owner runs shops, the quiet get warned twice, then deleted."""
import asyncio
import os

import pytest

from app import lifecycle
from tests.harness import PID, build, client, payload, post, post_tg, tg_update

DSN = os.environ.get("TEST_DATABASE_URL")
needs_db = pytest.mark.skipif(not DSN, reason="needs TEST_DATABASE_URL")
A, P = "254711000001", "tg:9001"


async def two_salons(dsn):
    pool, out, salon = await build(dsn, platform_owner_id=P)
    await pool.execute("INSERT INTO salons (name, wa_phone_number_id, slug) VALUES ('Malaika', $1, 'malaika')", PID)
    return pool, out, salon


@needs_db
def test_strangers_cannot_use_the_hub():
    async def run():
        pool, out, _salon = await build(DSN, platform_owner_id=P)
        async with client() as c:
            await post(c, payload(A, "shops", mid="h1"))
            assert "Businesses:" not in out.bodies(A)[-1]
        await pool.close()
    asyncio.run(run())


@needs_db
def test_shops_and_stats_list_everything():
    async def run():
        pool, out, _salon = await two_salons(DSN)
        async with client() as c:
            await post_tg(c, tg_update(1, 9001, "shops"))
            body = out.tg_bodies("9001")[-1]
            assert "Zuri Cuts" in body and "Malaika" in body
            await post_tg(c, tg_update(2, 9001, "stats"))
            assert "Salons: 2" in out.tg_bodies("9001")[-1]
        await pool.close()
    asyncio.run(run())


@needs_db
def test_off_and_on_close_and_reopen():
    async def run():
        pool, out, _salon = await two_salons(DSN)
        async with client() as c:
            await post_tg(c, tg_update(1, 9001, "off malaika"))
            assert "closed" in out.tg_bodies("9001")[-1]
            assert await pool.fetchval("SELECT active FROM salons WHERE slug='malaika'") is False
            await post_tg(c, tg_update(2, 9001, "on malaika"))
            assert await pool.fetchval("SELECT active FROM salons WHERE slug='malaika'") is True
        await pool.close()
    asyncio.run(run())


@needs_db
def test_delete_asks_first_then_removes_everything():
    async def run():
        pool, out, salon = await build(DSN, platform_owner_id=P)
        async with client() as c:
            await post_tg(c, tg_update(1, 9001, "del zuri"))
            assert "YES" in out.tg_bodies("9001")[-1]
            assert await pool.fetchval("SELECT count(*) FROM salons") == 1
            await post_tg(c, tg_update(2, 9001, "del Zuri Cuts YES"))
            assert "Deleted Zuri Cuts" in out.tg_bodies("9001")[-1]
            assert await pool.fetchval("SELECT count(*) FROM salons") == 0
            assert await pool.fetchval("SELECT count(*) FROM services") == 0
            assert await pool.fetchval("SELECT count(*) FROM bookings") == 0
        await pool.close()
    asyncio.run(run())


@needs_db
def test_warn_sends_the_notice_now():
    async def run():
        pool, out, salon = await build(DSN, platform_owner_id=P)
        await pool.execute("UPDATE salons SET owner_wa_id=$1", P)
        async with client() as c:
            await post_tg(c, tg_update(1, 9001, "warn zuri"))
            body = out.tg_bodies("9001")
            assert "Warning sent" in body[-1]
            warned = await pool.fetchval("SELECT inactivity_warned_at IS NOT NULL FROM salons")
            assert warned
        await pool.close()
    asyncio.run(run())


@needs_db
def test_sweep_warns_twice_then_deletes_and_resets_on_activity():
    async def run():
        from app.main import app
        pool, out, salon = await build(DSN, platform_owner_id=P)
        await pool.execute("UPDATE salons SET owner_wa_id=$1", P)
        cid = await pool.fetchval("INSERT INTO clients (salon_id, wa_id) VALUES ($1::uuid, 'x') RETURNING id::text", salon)
        sid = await pool.fetchval("SELECT id::text FROM services LIMIT 1")
        stid = await pool.fetchval("SELECT id::text FROM staff LIMIT 1")
        await pool.execute(
            "INSERT INTO bookings (salon_id, client_id, staff_id, service_id, starts_at, ends_at, status, created_at) "
            "VALUES ($1::uuid, $2::uuid, $3::uuid, $4::uuid, now() + interval '2 days', now() + interval '2 days' + interval '1 hour', 'confirmed', now() - interval '8 days')",
            salon, cid, stid, sid)
        ctx = app.state.ctx
        assert await lifecycle.sweep(ctx) == {"warned": [salon], "final": [], "deleted": [], "reset": []}
        assert "7 days" in out.tg_bodies("9001")[-1]
        await pool.execute("UPDATE bookings SET created_at = now() - interval '13 days 12 hours'")
        assert await lifecycle.sweep(ctx) == {"warned": [], "final": [salon], "deleted": [], "reset": []}
        assert "Last notice" in out.tg_bodies("9001")[-1]
        await pool.execute("UPDATE bookings SET created_at = now() - interval '15 days'")
        assert await lifecycle.sweep(ctx) == {"warned": [], "final": [], "deleted": [salon], "reset": []}
        assert "deleted" in out.tg_bodies("9001")[-1].lower()
        assert await pool.fetchval("SELECT count(*) FROM salons") == 0
        assert await pool.fetchval("SELECT count(*) FROM bookings") == 0
        await pool.close()
    asyncio.run(run())


@needs_db
def test_new_booking_cancels_the_countdown():
    async def run():
        from app.main import app
        pool, out, salon = await build(DSN, platform_owner_id=P)
        await pool.execute("UPDATE salons SET owner_wa_id=$1, inactivity_warned_at=now() - interval '1 day'", P)
        cid = await pool.fetchval("INSERT INTO clients (salon_id, wa_id) VALUES ($1::uuid, 'y') RETURNING id::text", salon)
        sid = await pool.fetchval("SELECT id::text FROM services LIMIT 1")
        stid = await pool.fetchval("SELECT id::text FROM staff LIMIT 1")
        await pool.execute(
            "INSERT INTO bookings (salon_id, client_id, staff_id, service_id, starts_at, ends_at, status) "
            "VALUES ($1::uuid, $2::uuid, $3::uuid, $4::uuid, now() + interval '2 days', now() + interval '2 days' + interval '1 hour', 'confirmed')",
            salon, cid, stid, sid)
        assert await lifecycle.sweep(app.state.ctx) == {"warned": [], "final": [], "deleted": [], "reset": [salon]}
        assert await pool.fetchval("SELECT inactivity_warned_at FROM salons") is None
        await pool.close()
    asyncio.run(run())


@needs_db
def test_owner_keep_cancels_the_countdown():
    async def run():
        pool, out, salon = await build(DSN, platform_owner_id=P)
        await pool.execute("UPDATE salons SET owner_wa_id=$1, inactivity_warned_at=now() - interval '1 day', inactivity_final_at=now() - interval '1 hour'", A)
        async with client() as c:
            await post(c, payload(A, "KEEP", mid="k1"))
            assert "Kept" in out.bodies(A)[-1]
            row = await pool.fetchrow("SELECT inactivity_warned_at, inactivity_final_at FROM salons")
            assert row["inactivity_warned_at"] is None and row["inactivity_final_at"] is None
        await pool.close()
    asyncio.run(run())
