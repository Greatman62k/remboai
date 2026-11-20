"""Query plans, statement counts and connection handling found by measuring a database with 1.26M bookings and 3M inbound messages.
Each test fails on the code before the fix."""
import asyncio
import os
import re
import pathlib
from datetime import datetime, timedelta, timezone

import asyncpg
import pytest

from app import bookings, db, lifecycle
from tests.harness import build

DSN = os.environ.get("TEST_DATABASE_URL")
needs_db = pytest.mark.skipif(not DSN, reason="needs TEST_DATABASE_URL")
APP = pathlib.Path(__file__).resolve().parent.parent / "app"


async def _plan(con, sql: str) -> str:
    async with con.transaction():
        await con.execute("SET LOCAL enable_seqscan = off")
        return "\n".join(r[0] for r in await con.fetch("EXPLAIN " + sql))


# -- indexes: each hot or periodic query can be answered from an index --------------------------------------------------------------
@needs_db
def test_the_per_sender_message_count_uses_a_per_sender_index():
    async def run():
        pool, out, salon = await build(DSN)
        await pool.execute("INSERT INTO inbound_messages (wa_message_id, wa_id, received_at) SELECT 'w'||g, '2547'||(g % 4000), now() - (g % 80000) * interval '1 second' FROM generate_series(1,40000) g")
        await pool.execute("ANALYZE inbound_messages")
        async with pool.acquire() as con:
            plan = await _plan(con, "SELECT count(*) FROM inbound_messages WHERE wa_id='25471234' AND received_at > now() - interval '24 hours'")
        assert "inbound_messages_sender_recent" in plan, plan
        await pool.close()
    asyncio.run(run())


@needs_db
def test_the_visit_check_and_owner_day_views_do_not_scan_all_bookings():
    async def run():
        pool, out, salon = await build(DSN)
        async with pool.acquire() as con:
            visit = await _plan(con, "SELECT b.id FROM bookings b WHERE b.status='confirmed' AND b.ends_at > now() - interval '6 hours' AND b.ends_at <= now() ORDER BY b.ends_at LIMIT 20")
            agenda = await _plan(con, f"SELECT b.id FROM bookings b WHERE b.salon_id='{salon}'::uuid AND b.status IN ('confirmed','held') AND b.starts_at >= now() AND b.starts_at < now() + interval '1 day' ORDER BY b.starts_at")
        assert "bookings_confirmed_ends_idx" in visit, visit
        assert "bookings_salon_starts_idx" in agenda, agenda
        await pool.close()
    asyncio.run(run())


# -- the daily sweep reads each salon's last booking in one pass, not one scan per salon -----------------------------------------------
@needs_db
def test_last_booking_per_salon_is_one_aggregate_and_still_correct():
    async def run():
        pool, out, salon = await build(DSN)
        empty = await pool.fetchval("INSERT INTO salons (name, wa_phone_number_id) VALUES ('Empty','empty-pid') RETURNING id::text")
        cid = await pool.fetchval("INSERT INTO clients (salon_id, wa_id) VALUES ($1::uuid,'c') RETURNING id::text", salon)
        sid = await pool.fetchval("SELECT id::text FROM services WHERE name='Fade'")
        stid = await pool.fetchval("SELECT id::text FROM staff LIMIT 1")
        for days in (30, 5, 12):
            start = datetime.now(timezone.utc) + timedelta(days=days)
            await pool.execute("INSERT INTO bookings (salon_id, staff_id, service_id, client_id, starts_at, ends_at, status, created_at) VALUES ($1::uuid,$2::uuid,$3::uuid,$4::uuid,$5,$6,'cancelled', now() - make_interval(days => $7))",
                               salon, stid, sid, cid, start, start + timedelta(minutes=45), days)
        rows = {r["id"]: r for r in await pool.fetch(lifecycle.SALONS_WITH_LAST_BOOKING_SQL)}
        expect = await pool.fetchval("SELECT max(created_at) FROM bookings WHERE salon_id=$1::uuid", salon)
        assert rows[salon]["last_booking"] == expect and rows[empty]["last_booking"] is None
        async with pool.acquire() as con:
            plan = "\n".join(r[0] for r in await con.fetch("EXPLAIN " + lifecycle.SALONS_WITH_LAST_BOOKING_SQL))
        assert "SubPlan" not in plan, "a correlated subquery scans the bookings table once per salon:\n" + plan
        await pool.close()
    asyncio.run(run())


# -- expiring many holds costs a fixed number of statements, not two per hold --------------------------------------------------------
class _Counting:
    def __init__(self, con): self.con, self.n = con, 0
    async def fetch(self, *a, **k): self.n += 1; return await self.con.fetch(*a, **k)
    async def execute(self, *a, **k): self.n += 1; return await self.con.execute(*a, **k)


@needs_db
def test_expiring_many_holds_uses_a_fixed_number_of_statements():
    async def run():
        pool, out, salon = await build(DSN)
        cid = await pool.fetchval("INSERT INTO clients (salon_id, wa_id) VALUES ($1::uuid,'c') RETURNING id::text", salon)
        sid = await pool.fetchval("SELECT id::text FROM services WHERE name='Fade'")
        stid = await pool.fetchval("SELECT id::text FROM staff LIMIT 1")
        held = []
        for i in range(6):
            start = datetime.now(timezone.utc) + timedelta(days=2, hours=i * 2)
            b = await bookings.hold_slot(pool, salon_id=salon, staff_id=stid, service_id=sid, client_id=cid, starts_at=start, ends_at=start + timedelta(minutes=45), minutes=10)
            await pool.execute("INSERT INTO reminder_jobs (booking_id, salon_id, client_id, wa_id, run_at, template_name) VALUES ($1::uuid,$2::uuid,$3::uuid,'254700000099', now() + interval '1 day', 'reminder_2h')", b, salon, cid)
            held.append(b)
        await pool.execute("INSERT INTO payments (booking_id, salon_id, phone, amount_kes, method, provider, mpesa_receipt, created_at) VALUES ($1::uuid,$2::uuid,'254700000000',200,'manual','mpesa','ABC123XYZ9', now() - interval '10 minutes')", held[0], salon)   # an old claim does not keep a hold alive
        await pool.execute("UPDATE bookings SET hold_expires_at = now() - interval '1 minute' WHERE status='held'")      # all six lapse together
        async with pool.acquire() as con:
            counted = _Counting(con)
            assert await bookings.expire_stale_holds(counted) == 6
        assert counted.n <= 3, f"{counted.n} statements for 6 expired holds"
        assert await pool.fetchval("SELECT count(*) FROM bookings WHERE status='expired'") == 6
        assert await pool.fetchval("SELECT count(*) FROM reminder_jobs WHERE status='cancelled'") == 6
        assert "hold expired" in await pool.fetchval("SELECT result_desc FROM payments WHERE booking_id=$1::uuid", held[0])
        await pool.close()
    asyncio.run(run())


# -- the pool no longer sends a reset statement on every release ----------------------------------------------------------------------
@needs_db
def test_a_pool_call_is_one_statement_not_two(monkeypatch):
    sent = []
    real = asyncpg.Connection.execute
    async def spy(self, query, *a, **k):
        sent.append(query)
        return await real(self, query, *a, **k)
    async def run():
        pool = await db.make_pool(DSN)
        monkeypatch.setattr(asyncpg.Connection, "execute", spy)
        for _ in range(5):
            await pool.fetchval("SELECT 1")
        monkeypatch.setattr(asyncpg.Connection, "execute", real)
        await pool.close()
    asyncio.run(run())
    assert not [q for q in sent if "pg_advisory_unlock_all" in q], f"{len(sent)} reset statements for 5 calls"


@needs_db
def test_a_connection_released_mid_transaction_is_still_cleaned_up():
    async def run():
        pool = await db.make_pool(DSN, max_size=3)
        con = await pool.acquire()
        await con.execute("BEGIN")
        await con.execute("CREATE TABLE zz_leaked_probe (a int)")
        await pool.release(con)
        async with pool.acquire() as again:
            assert not again.is_in_transaction()
            assert await again.fetchval("SELECT to_regclass('zz_leaked_probe')") is None, "an open transaction must never leak to the next caller"
        await pool.close()
    asyncio.run(run())


@needs_db
def test_migrate_releases_its_session_lock_without_relying_on_a_reset():
    async def run():
        pool = await db.make_pool(DSN, max_size=3)
        await db.migrate(pool)
        other = await asyncpg.connect(DSN)
        try:
            assert await other.fetchval("SELECT pg_try_advisory_lock(hashtext('remboai:migrations'))"), "the migration lock is still held by a pooled connection"
        finally:
            await other.close()
            await pool.close()
    asyncio.run(run())


def test_nothing_in_the_app_depends_on_connection_reset():
    """The pool skips asyncpg's per-release reset (advisory locks, LISTEN, session SET, cursors, temp tables), so none of that may be used."""
    import ast
    risky = [(r"\bpg_advisory_lock\s*\(", "a session-level advisory lock (use pg_advisory_xact_lock)"), (r"\bUN?LISTEN\b", "LISTEN"),
             (r"^\s*SET\s+(?!LOCAL\b)\w+", "a session-level SET (use SET LOCAL)"), (r"\bset_config\s*\([^)]*,\s*false\s*\)", "set_config(..., false)"),
             (r"\bCREATE\s+TEMP", "a temporary table"), (r"\bDECLARE\b.*\bCURSOR\b", "a cursor")]
    db_calls = {"fetch", "fetchrow", "fetchval", "execute", "executemany"}
    found = []
    for path in sorted(APP.rglob("*.py")):
        for node in ast.walk(ast.parse(path.read_text())):
            if isinstance(node, ast.Call) and isinstance(node.func, ast.Attribute):
                if node.func.attr == "cursor":
                    found.append(f"{path.name}:{node.lineno} con.cursor()")
                if node.func.attr in db_calls and node.args and isinstance(node.args[0], ast.Constant) and isinstance(node.args[0].value, str):
                    for pattern, what in risky:                       # only strings handed to the database, not help text that starts with "set"
                        if re.search(pattern, node.args[0].value, re.I | re.S | re.M):
                            found.append(f"{path.name}:{node.lineno} {what}")
    allowed = [f for f in found if f.startswith("db.py") and "advisory lock" in f]     # migrate() takes one and releases it in a finally block
    assert [f for f in found if f not in allowed] == [], "session state that a pooled connection would carry to the next caller"
    assert "pg_advisory_unlock(hashtext('remboai:migrations'))" in (APP / "db.py").read_text()
