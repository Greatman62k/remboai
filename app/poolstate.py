"""Small per-pool state (a concurrency cap, in-process locks). asyncpg's Pool cannot be weakly referenced, so the registry holds the pool
itself (which also stops an id() being reused by a different pool) and forgets pools that have been closed."""
from __future__ import annotations

_state: dict[int, tuple[object, dict]] = {}


def state(pool) -> dict:
    entry = _state.get(id(pool))
    if entry is None or entry[0] is not pool:
        for key, (p, _) in list(_state.items()):
            try:
                if p.is_closing():
                    del _state[key]
            except Exception:
                pass
        entry = _state[id(pool)] = (pool, {})
    return entry[1]
