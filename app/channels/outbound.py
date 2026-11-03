"""Everything the app sends to a person goes through here, for two reasons.

1. Which token. A salon may have its own WhatsApp token (stored encrypted); one that has none uses the platform's. The rule lives in one place.
2. Retries. A message that failed half way is handled again, and must not repeat what it already did. While a message is being handled
   (`message_scope`) each send is recorded; the same send in a retry is recognised and skipped.

   Exactly-once is impossible across a network call: if the process dies after WhatsApp accepted a message but before it was recorded,
   the retry sends it again. The window is the time between those two statements, not the length of the whole handler."""
from __future__ import annotations

import hashlib
import hmac
import json
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
    conversational: bool = False
    seen: Counter = field(default_factory=Counter)
    sequence: int = 0


_scope: ContextVar[_Scope | None] = ContextVar("rembo_message_scope", default=None)


@asynccontextmanager
async def message_scope(message_id: str, conversational: bool = False):
    token = _scope.set(_Scope(message_id, conversational))
    try:
        yield
    finally:
        _scope.reset(token)


def current_message_id() -> str | None:
    s = _scope.get()
    return s.message_id if s else None


def trace_thread(ctx, sender: str) -> str:
    """Return a stable opaque thread key without truncating sender IDs into collisions."""
    settings = ctx.settings
    secret = (getattr(settings, "wa_app_secret", "") or
              getattr(settings, "token_encryption_key", "") or
              getattr(settings, "ops_api_key", "") or
              getattr(settings, "database_url", ""))
    channel = "telegram" if sender.startswith("tg:") else "whatsapp"
    value = f"remboai:conversation-trace:v1:{channel}:{sender}".encode()
    digest = hmac.new(str(secret).encode(), value, hashlib.sha256).hexdigest()[:32]
    return f"{channel}:{digest}"


async def rekey_legacy_trace_threads(pool, settings) -> int:
    """Replace recent suffix-only thread keys using the sender IDs retained with inbound events."""
    from types import SimpleNamespace

    ctx = SimpleNamespace(settings=settings)
    async with pool.acquire() as con:
        async with con.transaction():
            rows = await con.fetch(
                "SELECT c.message_id, c.thread, i.wa_id FROM conversation_trace c "
                "JOIN inbound_messages i ON i.wa_message_id=c.message_id "
                "WHERE c.thread NOT LIKE 'whatsapp:%' AND c.thread NOT LIKE 'telegram:%' "
                "AND c.thread NOT LIKE 'owner:%' FOR UPDATE OF c")
            if not rows:
                return 0
            await con.executemany(
                "UPDATE conversation_trace SET thread=$2 WHERE message_id=$1 AND thread=$3",
                [(row["message_id"], trace_thread(ctx, row["wa_id"]), row["thread"]) for row in rows])
            return len(rows)


async def record_inbound(ctx, msg) -> None:
    """Keep the sanitized inbound side of a chat turn for the protected conversation trace."""
    await ctx.pool.execute(
        "INSERT INTO conversation_trace (message_id, thread, channel, message_kind, user_text) "
        "VALUES ($1,$2,$3,$4,$5) ON CONFLICT (message_id) DO NOTHING",
        msg.id, trace_thread(ctx, msg.sender), "telegram" if msg.sender.startswith("tg:") else "whatsapp",
        (msg.kind or "other")[:32], (msg.text or "")[:5000],
    )


async def update_inbound_text(ctx, text: str) -> None:
    """Replace an audio placeholder with the sanitized speech transcript, when one exists."""
    scope = _scope.get()
    if scope is not None and scope.conversational:
        await ctx.pool.execute(
            "UPDATE conversation_trace SET user_text=$2 WHERE message_id=$1",
            scope.message_id, (text or "")[:5000],
        )


async def set_conversation_salon(ctx, salon_id: str) -> None:
    """Attach the resolved business context to this turn without storing channel IDs or recipient numbers."""
    scope = _scope.get()
    if scope is not None and scope.conversational:
        await ctx.pool.execute(
            "UPDATE conversation_trace SET salon_id=$2::uuid WHERE message_id=$1",
            scope.message_id, salon_id,
        )


async def _record_reply(ctx, scope: _Scope, sequence: int, kind: str, body: str,
                        buttons: list[tuple[str, str]] | None, send_ok: bool) -> None:
    await ctx.pool.execute(
        "INSERT INTO conversation_trace_replies (message_id, sequence, message_kind, body, buttons, send_ok) "
        "VALUES ($1,$2,$3,$4,$5::jsonb,$6) "
        "ON CONFLICT (message_id, sequence) DO UPDATE SET message_kind=EXCLUDED.message_kind, body=EXCLUDED.body, "
        "buttons=EXCLUDED.buttons, send_ok=EXCLUDED.send_ok",
        scope.message_id, sequence, kind, (body or "")[:4096],
        json.dumps(buttons, ensure_ascii=False) if buttons else None, send_ok,
    )


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


async def _once(ctx, kind: str, to: str, body: str, send,
                buttons: list[tuple[str, str]] | None = None, digest_body: str | None = None) -> bool:
    scope = _scope.get()
    if scope is None:
        return bool(await send())
    scope.sequence += 1
    sequence = scope.sequence
    digest = hashlib.sha256(f"{kind}\x00{to}\x00{digest_body if digest_body is not None else body}".encode()).hexdigest()[:32]
    scope.seen[digest] += 1
    occ = scope.seen[digest]
    if await ctx.pool.fetchval("SELECT 1 FROM outbound_log WHERE message_id=$1 AND digest=$2 AND occ=$3", scope.message_id, digest, occ):
        log.info("not sending again: already sent while handling %s", scope.message_id)
        if scope.conversational:
            await _record_reply(ctx, scope, sequence, kind, body, buttons, True)
        return True
    ok = bool(await send())
    if ok:
        await ctx.pool.execute("INSERT INTO outbound_log (message_id, digest, occ) VALUES ($1,$2,$3) ON CONFLICT DO NOTHING", scope.message_id, digest, occ)
    if scope.conversational:
        await _record_reply(ctx, scope, sequence, kind, body, buttons, ok)
    return ok


async def send_text(ctx, salon: dict, to: str, body: str) -> bool:
    token = salon_token(ctx, salon)
    return await _once(ctx, "text", to, body, lambda: ctx.wa.send_text(salon["wa_phone_number_id"], token, to, body))


async def send_plain(ctx, to: str, body: str) -> bool:
    """A message that belongs to no salon (the Telegram bot telling someone their own id): there is no token to choose."""
    return await _once(ctx, "plain", to, body, lambda: ctx.wa.send_text(None, "", to, body))


async def send_unbound_text(ctx, phone_number_id, to: str, body: str) -> bool:
    """Send before a salon is bound, while retaining the normal per-inbound retry guard."""
    return await _once(ctx, "plain", to, body, lambda: ctx.wa.send_text(phone_number_id, "", to, body))


async def send_unbound_buttons(ctx, phone_number_id, to: str, body: str, buttons: list[tuple[str, str]]) -> bool:
    """Button counterpart to send_unbound_text for setup flows before salon creation."""
    digest_body = body + "\x00" + repr(buttons)
    return await _once(ctx, "buttons", to, body, lambda: ctx.wa.send_buttons(phone_number_id, "", to, body, buttons), buttons, digest_body)


async def send_buttons(ctx, salon: dict, to: str, body: str, buttons: list[tuple[str, str]]) -> bool:
    token = salon_token(ctx, salon)
    digest_body = body + "\x00" + repr(buttons)
    return await _once(ctx, "buttons", to, body, lambda: ctx.wa.send_buttons(salon["wa_phone_number_id"], token, to, body, buttons), buttons, digest_body)
