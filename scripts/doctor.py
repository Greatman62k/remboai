#!/usr/bin/env python3
"""Check the whole set-up in one go:  python scripts/doctor.py   (reads .env; prints no secrets)."""
import asyncio
import sys
from pathlib import Path

import httpx

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
from app import db  # noqa: E402
from app.config import get_settings  # noqa: E402
from app.doctor import run_checks  # noqa: E402


async def main() -> int:
    s = get_settings()
    pool = await db.make_pool(s.database_url)
    await db.migrate(pool)
    async with httpx.AsyncClient(timeout=10) as http:
        checks = await run_checks(s, http, pool)
    await pool.close()
    for c in checks:
        mark = "ok  " if c.ok else ("warn" if c.warn else "FAIL")
        print(f"[{mark}] {c.name}: {c.detail}")
    bad = [c for c in checks if not c.ok and not c.warn]
    print("\nAll set." if not bad else f"\n{len(bad)} problem(s) to fix before going live.")
    return 1 if bad else 0


if __name__ == "__main__":
    sys.exit(asyncio.run(main()))
