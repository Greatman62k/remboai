"""What happens to one inbound message, whichever channel it came from: once only, within the rate limit, then the conversation."""
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
        recent = await ctx.pool.fetchval(
            "SELECT count(*) FROM inbound_messages WHERE wa_id=$1 AND received_at > now() - interval '1 minute'", msg.sender)
        if recent > ctx.settings.rate_limit_per_min:
            log.warning("rate limit: ignoring %s (%d messages in a minute)", mask(msg.sender), recent)
            if recent == ctx.settings.rate_limit_per_min + 1:   # one strike per burst, not one per message
                await guard.strike(ctx.pool, msg.sender, "burst")
            return
        day = await ctx.pool.fetchval(
            "SELECT count(*) FROM inbound_messages WHERE wa_id=$1 AND received_at > now() - interval '24 hours'", msg.sender)
        if ctx.settings.per_sender_daily_limit and day > ctx.settings.per_sender_daily_limit:
            log.warning("daily limit: ignoring %s (%d messages in 24 hours)", mask(msg.sender), day)
            return
        log.info("message from %s kind=%s", mask(msg.sender), msg.kind)
        if msg.kind == "text" and msg.text.lower() == "/id" and msg.sender.startswith("tg:"):
            # lets an owner find the id to put in --owner
            await ctx.wa.send_text(None, "", msg.sender, f"Your Telegram id: {msg.sender}")
            return
        msg = dataclasses.replace(msg, text=guard.clean(msg.text), name=guard.clean_name(msg.name))   # invisible characters out, names tidy
        await converse(ctx, msg)
    except Exception:
        log.exception("failed handling message %s", msg.id)
