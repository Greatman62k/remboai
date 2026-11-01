"""A lock per key that does not hold a database connection while it is held.

A session advisory lock lives on one connection, so every message being handled pinned a pooled connection for as long as the model call
and the sends took, and the pool could be starved by slow replies. Here the lock is a row with a lease: acquiring and renewing it are
single short statements, and a holder that dies loses it when the lease runs out."""
from __future__ import annotations

import asyncio
import logging
import random
import uuid
from contextlib import asynccontextmanager

from . import poolstate

log = logging.getLogger("rembo.locks")
LEASE_SECONDS = 60
RENEW_SECONDS = 15
WAIT_SECONDS = 120



class LockTimeout(Exception):
    pass


@asynccontextmanager
async def _process_lock(pool, key: str):
    """An asyncio lock per key, created on demand and dropped when nobody holds or awaits it (one per sender would otherwise pile up forever)."""
    table = poolstate.state(pool).setdefault("locks", {})
    entry = table.setdefault(key, [asyncio.Lock(), 0])
    entry[1] += 1
    try:
        async with entry[0]:
            yield
    finally:
        entry[1] -= 1
        if entry[1] == 0 and table.get(key) is entry:
            del table[key]


async def _try_acquire(pool, key: str, owner: uuid.UUID) -> bool:
    return bool(await pool.fetchval(
        "INSERT INTO sender_locks (key, owner, locked_until) VALUES ($1, $2, now() + ($3 * interval '1 second')) "
        "ON CONFLICT (key) DO UPDATE SET owner = EXCLUDED.owner, locked_until = EXCLUDED.locked_until WHERE sender_locks.locked_until < now() "
        "RETURNING 1", key, owner, LEASE_SECONDS))


async def _renew_once(pool, key: str, owner: uuid.UUID) -> bool:
    """One lease renewal. False means someone else owns the lock now: the holder must stop, not carry on."""
    try:
        n = await pool.execute("UPDATE sender_locks SET locked_until = now() + ($3 * interval '1 second') WHERE key=$1 AND owner=$2", key, owner, LEASE_SECONDS)
    except Exception:
        log.warning("could not renew the lock %s", key)
        return False
    if not n.endswith("1"):
        log.error("lost the lock %s while still working on it", key)
        return False
    return True


async def _renew(pool, key: str, owner: uuid.UUID, holder: asyncio.Task | None = None) -> None:
    while True:
        await asyncio.sleep(RENEW_SECONDS)
        if not await _renew_once(pool, key, owner):
            if holder is not None:
                holder.cancel()
            return


@asynccontextmanager
async def hold(pool, key: str):
    """Exclusive per key across every process. Waiters in this process queue on an asyncio lock first, so only one of them polls the database."""
    async with _process_lock(pool, key):
        owner = uuid.uuid4()
        loop = asyncio.get_running_loop()
        deadline, delay = loop.time() + WAIT_SECONDS, 0.05
        while not await _try_acquire(pool, key, owner):
            if loop.time() > deadline:
                raise LockTimeout(key)
            await asyncio.sleep(delay + random.random() * delay)
            delay = min(delay * 2, 1.0)
        beat = asyncio.create_task(_renew(pool, key, owner, asyncio.current_task()))
        try:
            yield
        finally:
            beat.cancel()
            await asyncio.gather(beat, return_exceptions=True)
            try:
                await asyncio.shield(pool.execute("DELETE FROM sender_locks WHERE key=$1 AND owner=$2", key, owner))
            except Exception:
                log.warning("could not release the lock %s; it expires on its own", key)
