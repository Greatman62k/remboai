"""Everything the app sends to a person goes through here, for two reasons.

1. Which token. A salon may have its own WhatsApp token (stored encrypted); one that has none uses the platform's. The rule lives in one place.
2. Retries. A message that failed half way is handled again, and must not repeat what it already did. While a message is being handled
   (`message_scope`) each send is recorded; the same send in a retry is recognised and skipped.

   Exactly-once is impossible across a network call: if the process dies after WhatsApp accepted a message but before it was recorded,
   the retry sends it again. The window is the time between those two statements, not the length of the whole handler."""
from __future__ import annotations

import hashlib
import logging
from collections import Counter
from contextlib import asynccontextmanager
from contextvars import ContextVar
from dataclasses import dataclass, field

from app.token_crypto import decrypt_token

log = logging.getLogger("rembo.outbound")


@dataclass
class _Scope:
    message_id: str
    seen: Counter = field(default_factory=Counter)


_scope: ContextVar[_Scope | None] = ContextVar("rembo_message_scope", default=None)


@asynccontextmanager
async def message_scope(message_id: str):
    token = _scope.set(_Scope(message_id))
    try:
        yield
    finally:
        _scope.reset(token)


def current_message_id() -> str | None:
    s = _scope.get()
    return s.message_id if s else None


def salon_token(ctx, salon: dict | None) -> str:
    """The salon's own WhatsApp token if it has one (decrypting it), otherwise the platform token."""
    stored = (salon or {}).get("wa_access_token")
    if stored:
        try:
            own = decrypt_token(stored, getattr(ctx.settings, "token_encryption_key", "") or None, require_encryption=False)
        except RuntimeError:
            log.error("the stored WhatsApp token for salon %s cannot be decrypted; using the platform token", (salon or {}).get("id"))
            own = None
        if own:
            return own
    return ctx.settings.wa_access_token


async def _once(ctx, kind: str, to: str, body: str, send) -> bool:
    scope = _scope.get()
    if scope is None:
        return bool(await send())
    digest = hashlib.sha256(f"{kind}\x00{to}\x00{body}".encode()).hexdigest()[:32]
    scope.seen[digest] += 1
    occ = scope.seen[digest]
    if await ctx.pool.fetchval("SELECT 1 FROM outbound_log WHERE message_id=$1 AND digest=$2 AND occ=$3", scope.message_id, digest, occ):
        log.info("not sending again: already sent while handling %s", scope.message_id)
        return True
    ok = bool(await send())
    if ok:
        await ctx.pool.execute("INSERT INTO outbound_log (message_id, digest, occ) VALUES ($1,$2,$3) ON CONFLICT DO NOTHING", scope.message_id, digest, occ)
    return ok


async def send_text(ctx, salon: dict, to: str, body: str) -> bool:
    token = salon_token(ctx, salon)
    return await _once(ctx, "text", to, body, lambda: ctx.wa.send_text(salon["wa_phone_number_id"], token, to, body))


async def send_plain(ctx, to: str, body: str) -> bool:
    """A message that belongs to no salon (the Telegram bot telling someone their own id): there is no token to choose."""
    return await _once(ctx, "plain", to, body, lambda: ctx.wa.send_text(None, "", to, body))


async def send_buttons(ctx, salon: dict, to: str, body: str, buttons: list[tuple[str, str]]) -> bool:
    token = salon_token(ctx, salon)
    return await _once(ctx, "buttons", to, body + "\x00" + repr(buttons), lambda: ctx.wa.send_buttons(salon["wa_phone_number_id"], token, to, body, buttons))
