import dataclasses
import logging

from . import guard
from .conversation import handle as converse
from .whatsapp import Message, mask

log = logging.getLogger("rembo")


async def inbound(ctx, msg: Message) -> None:
    try:
        fresh = await ctx.pool.fetchval(
            "INSERT INTO inbound_messages (wa_message_id, wa_id) VALUES ($1, $2) ON CONFLICT DO NOTHING RETURNING 1", msg.id, msg.sender)
        if not fresh:
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
                return
            day = await ctx.pool.fetchval("SELECT count(*) FROM inbound_messages WHERE wa_id=$1 AND received_at > now() - interval '24 hours'", msg.sender)
            if ctx.settings.per_sender_daily_limit and day and day > ctx.settings.per_sender_daily_limit:
                log.warning("daily limit: ignoring %s (%d messages in 24 hours)", mask(msg.sender), day)
                return
        log.info("message from %s kind=%s", mask(msg.sender), msg.kind)
        if msg.kind == "text" and msg.text.lower() == "/id" and msg.sender.startswith("tg:"):
            await ctx.wa.send_text(None, "", msg.sender, f"Your Telegram id: {msg.sender}")
            return
        msg = dataclasses.replace(msg, text=guard.clean(msg.text), name=guard.clean_name(msg.name))
        await converse(ctx, msg)
    except Exception:
        log.exception("failed handling message %s", msg.id)
