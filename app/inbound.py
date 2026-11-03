import dataclasses
import hashlib
import logging
from datetime import datetime, timedelta, timezone

from . import guard
from .channels import outbound
from .conversation import handle as converse
from .channels.whatsapp import Message, mask

log = logging.getLogger("rembo")
TELEGRAM_STALE_AFTER = timedelta(minutes=30)


async def _reject_stale_telegram(ctx, msg: Message) -> bool:
    """Do not act on a Telegram update delivered after its useful reply window."""
    if not msg.sender.startswith("tg:") or msg.sent_at is None:
        return False
    sent_at = msg.sent_at
    if sent_at.tzinfo is None:
        sent_at = sent_at.replace(tzinfo=timezone.utc)
    if datetime.now(timezone.utc) - sent_at <= TELEGRAM_STALE_AFTER:
        return False
    from .messages import t
    from .smalltalk import language
    body = t(language(msg.text or "", "en"), "stale_message")
    digest = hashlib.sha256(f"plain\x00{msg.sender}\x00{body}".encode()).hexdigest()[:32]
    already_notified = await ctx.pool.fetchval(
        "SELECT 1 FROM outbound_log o JOIN inbound_messages i ON i.wa_message_id=o.message_id "
        "WHERE i.wa_id=$1 AND o.digest=$2 AND o.sent_at > now()-interval '30 minutes' LIMIT 1",
        msg.sender, digest)
    if already_notified:
        return True
    if not await outbound.send_plain(ctx, msg.sender, body):
        raise RuntimeError("could not send stale-message notice")
    log.info("declined stale Telegram update %s from %s", msg.id, mask(msg.sender))
    return True


async def _process(ctx, msg: Message) -> None:
    """Everything done with one message once it has been accepted. Returning normally means "dealt with" (including messages deliberately
    dropped for abuse or rate limits); raising means "not dealt with, try again"."""
    msg = dataclasses.replace(msg, text=guard.clean(msg.text), name=guard.clean_name(msg.name))
    await outbound.record_inbound(ctx, msg)
    if await _reject_stale_telegram(ctx, msg):
        return
    if await guard.blocked(ctx.pool, msg.sender, ctx.settings.strike_limit):
        log.warning("ignoring %s: too many abuse signals today", mask(msg.sender))
        return
    redis = getattr(ctx, "redis", None)
    if redis is not None:
        try:
            from .ratelimit import RateLimiter
            limiter = RateLimiter(redis, ctx.settings)
            status = await limiter.check_sender("global", msg.sender)
            if status != "ok":
                log.warning("rate limit: %s %s", mask(msg.sender), status)
                if status == "notify":
                    await guard.strike(ctx.pool, msg.sender, "burst")
                    await _warn_slow_down(ctx, msg)
                return
        except Exception:
            pass
        d = await ctx.pool.fetchval("SELECT count(*) FROM inbound_messages WHERE wa_id=$1 AND received_at > now() - interval '24 hours'", msg.sender)
        if ctx.settings.per_sender_daily_limit and d and d > ctx.settings.per_sender_daily_limit:
            log.warning("daily limit: ignoring %s (%d messages in 24 hours)", mask(msg.sender), d)
            return
    else:
        recent = await ctx.pool.fetchval("SELECT count(*) FROM inbound_messages WHERE wa_id=$1 AND received_at > now() - interval '1 minute'", msg.sender)
        if recent and recent > ctx.settings.rate_limit_per_min:
            log.warning("rate limit: ignoring %s (%d messages in a minute)", mask(msg.sender), recent)
            if recent == ctx.settings.rate_limit_per_min + 1:
                await guard.strike(ctx.pool, msg.sender, "burst")
                await _warn_slow_down(ctx, msg)
            return
        day = await ctx.pool.fetchval("SELECT count(*) FROM inbound_messages WHERE wa_id=$1 AND received_at > now() - interval '24 hours'", msg.sender)
        if ctx.settings.per_sender_daily_limit and day and day > ctx.settings.per_sender_daily_limit:
            log.warning("daily limit: ignoring %s (%d messages in 24 hours)", mask(msg.sender), day)
            return
    log.info("message from %s kind=%s", mask(msg.sender), msg.kind)
    await converse(ctx, msg)


async def _warn_slow_down(ctx, msg: Message) -> None:
    """The first message over the line gets an answer, not silence: only the flood after it is ignored."""
    try:
        from .db import salon_by_phone_id
        from .ratelimit import TOO_FAST
        salon = await salon_by_phone_id(ctx.pool, msg.phone_number_id, ctx.settings)
        if salon is None:
            return
        await outbound.send_text(ctx, salon, msg.sender, TOO_FAST)
    except Exception:
        log.warning("could not warn %s about the rate limit", mask(msg.sender))


async def inbound(ctx, msg: Message) -> None:
    """Handle one message at least once, and not again once it has been handled.

    The message is recorded when it first arrives (that row is also what the rate limits count), but it only counts as done when
    `completed_at` is set after the handler returned. If the handler raises, or the process dies half way, the row stays open and the
    next delivery of the same message (the queue retries the job) handles it again. Only a message that was completed is skipped as a duplicate.

    This is at-least-once, not exactly-once: a crash after a reply was sent but before completion is recorded sends that reply again."""
    fresh = await ctx.pool.fetchval(
        "INSERT INTO inbound_messages (wa_message_id, wa_id) VALUES ($1, $2) ON CONFLICT DO NOTHING RETURNING 1", msg.id, msg.sender)
    if not fresh:
        done = await ctx.pool.fetchval("SELECT completed_at IS NOT NULL FROM inbound_messages WHERE wa_message_id=$1", msg.id)
        if done:
            return
        log.warning("message %s was accepted earlier but never finished: handling it again", msg.id)
    try:
        async with outbound.message_scope(msg.id, conversational=True):
            await _process(ctx, msg)
    except Exception:
        log.exception("failed handling message %s; it stays open for a retry", msg.id)
        raise
    await ctx.pool.execute("UPDATE inbound_messages SET completed_at=now(), state_before=NULL WHERE wa_message_id=$1 AND completed_at IS NULL", msg.id)
