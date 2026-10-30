"""The migration runner: concurrent boots do not collide, and repeat runs apply nothing twice."""
import asyncio
import os

import pytest

DSN = os.environ.get("TEST_DATABASE_URL")
needs_db = pytest.mark.skipif(not DSN, reason="needs TEST_DATABASE_URL")


@needs_db
def test_concurrent_migrates_do_not_collide():
    async def run():
        from pathlib import Path
        from app.db import make_pool, migrate
        pool = await make_pool(DSN)
        await pool.execute("DROP SCHEMA public CASCADE; CREATE SCHEMA public;")
        got = await asyncio.gather(migrate(pool), migrate(pool))
        assert sorted(sum(got, [])) == sorted(f.name for f in Path("migrations").glob("*.sql"))
        assert await migrate(pool) == []
        await pool.close()
    asyncio.run(run())
