"""Per-sender locks: exclusion across processes, and a stolen lock stops its holder instead of double-handling."""
import asyncio
import os
import uuid

import pytest

from app import locks

DSN = os.environ.get("TEST_DATABASE_URL")
needs_db = pytest.mark.skipif(not DSN, reason="needs TEST_DATABASE_URL")


def test_a_database_error_during_renewal_fails_closed():
    class BrokenPool:
        async def execute(self, *_args):
            raise OSError("database unavailable")

    async def run():
        assert await locks._renew_once(BrokenPool(), "sender:k", uuid.uuid4()) is False

    asyncio.run(run())


@needs_db
def test_a_renewal_fails_once_the_lock_is_stolen():
    async def run():
        from app.db import make_pool, migrate
        pool = await make_pool(DSN)
        await pool.execute("DROP SCHEMA public CASCADE; CREATE SCHEMA public;")
        await migrate(pool)
        assert await locks._try_acquire(pool, "k", uuid.uuid4())
        owner = await pool.fetchval("SELECT owner FROM sender_locks WHERE key='k'")
        assert await locks._renew_once(pool, "k", owner) is True
        await pool.execute("UPDATE sender_locks SET owner=$1 WHERE key='k'", uuid.uuid4())
        assert await locks._renew_once(pool, "k", owner) is False
        await pool.close()
    asyncio.run(run())


@needs_db
def test_a_holder_whose_lock_is_stolen_is_cancelled_not_left_working():
    async def run():
        from app.db import make_pool, migrate
        pool = await make_pool(DSN)
        await pool.execute("DROP SCHEMA public CASCADE; CREATE SCHEMA public;")
        await migrate(pool)
        locks.RENEW_SECONDS = 0.05
        worked = []

        async def holder():
            async with locks.hold(pool, "k"):
                try:
                    await asyncio.sleep(30)
                    worked.append(True)
                except asyncio.CancelledError:
                    raise

        try:
            task = asyncio.create_task(holder())
            await asyncio.sleep(0.3)
            await pool.execute("UPDATE sender_locks SET owner=$1, locked_until=now() + interval '1 minute' WHERE key='k'", uuid.uuid4())
            try:
                await asyncio.wait_for(task, timeout=5)
            except asyncio.CancelledError:
                pass
            assert task.cancelled() and not worked
        finally:
            locks.RENEW_SECONDS = 15
            await pool.close()
    asyncio.run(run())
