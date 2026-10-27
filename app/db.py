"""Connection pool and a tiny migration runner: every file in migrations/ is applied once, in name order."""
from pathlib import Path

import asyncpg

MIGRATIONS = Path(__file__).resolve().parent.parent / "migrations"


async def make_pool(dsn: str) -> asyncpg.Pool:
    return await asyncpg.create_pool(dsn, min_size=1, max_size=10)


async def migrate(pool: asyncpg.Pool, directory: Path = MIGRATIONS) -> list[str]:
    """Apply pending migrations; returns the names applied this time."""
    applied_now: list[str] = []
    async with pool.acquire() as con:
        await con.execute("CREATE TABLE IF NOT EXISTS schema_migrations (name text PRIMARY KEY, applied_at timestamptz NOT NULL DEFAULT now())")
        done = {r["name"] for r in await con.fetch("SELECT name FROM schema_migrations")}
        for f in sorted(directory.glob("*.sql")):
            if f.name in done:
                continue
            async with con.transaction():
                await con.execute(f.read_text())
                await con.execute("INSERT INTO schema_migrations (name) VALUES ($1)", f.name)
            applied_now.append(f.name)
    return applied_now
