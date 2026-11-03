"""Telegram as a second channel for the same receptionist. Telegram bots need no business verification and no public URL while
testing (long polling), and they carry everything the WhatsApp flow uses: text, tap buttons, voice notes and photos.
Customers and owners are addressed as 'tg:<chat id>'; a salon's bot is 'tg:<bot id>'."""
from __future__ import annotations

import asyncio
import logging

import httpx

from .whatsapp import Message, _unix_time, mask

log = logging.getLogger("rembo.telegram")
API = "https://api.telegram.org"
MIME = {"oga": "audio/ogg", "ogg": "audio/ogg", "opus": "audio/ogg", "mp3": "audio/mpeg", "m4a": "audio/mp4", "jpg": "image/jpeg", "jpeg": "image/jpeg",
        "png": "image/png", "webp": "image/webp"}


class Telegram:
    def __init__(self, http: httpx.AsyncClient, token: str):
        self.http, self.token = http, token

    @property
    def bot_key(self) -> str:
        return "tg:" + self.token.split(":", 1)[0]

    async def call(self, method: str, payload: dict | None = None, timeout: float = 10.0) -> dict | list | None:
        body = None
        for attempt in range(3):
            try:
                r = await self.http.post(f"{API}/bot{self.token}/{method}", json=payload or {}, timeout=timeout)
                body = r.json()
                break
            except (httpx.ConnectError, httpx.ConnectTimeout) as e:
                # The request never reached Telegram, so trying again cannot send anything twice.
                if attempt == 2:
                    log.warning("telegram %s failed: %s", method, e.__class__.__name__)
                    return None
                await asyncio.sleep(0.5 * (attempt + 1))
            except (httpx.HTTPError, ValueError) as e:
                log.warning("telegram %s failed: %s", method, e.__class__.__name__)
                return None
        if not body.get("ok"):
            log.error("telegram %s refused: %s %s", method, body.get("error_code"), body.get("description"))
            return None
        return body.get("result")

    async def send_text(self, chat: str, body: str) -> bool:
        ok = await self.call("sendMessage", {"chat_id": chat, "text": body[:4096]}) is not None
        if not ok:
            log.error("DELIVERY FAILED to %s on Telegram", mask(chat))
        return ok

    async def send_buttons(self, chat: str, body: str, buttons: list[tuple[str, str]]) -> bool:
        markup = {"inline_keyboard": [[{"text": title[:64], "callback_data": cid[:64]}] for cid, title in buttons]}
        ok = await self.call("sendMessage", {"chat_id": chat, "text": body[:4096], "reply_markup": markup}) is not None
        if not ok:
            log.error("DELIVERY FAILED to %s on Telegram", mask(chat))
        return ok

    async def typing(self, chat: str) -> None:
        """Shows 'typing...' while the model thinks. Best effort."""
        await self.call("sendChatAction", {"chat_id": chat, "action": "typing"}, timeout=3.0)

    async def answer_callback(self, callback_id: str) -> None:
        await self.call("answerCallbackQuery", {"callback_query_id": callback_id})

    async def username(self) -> str | None:
        """The bot's @username, for customer invite links. None when it cannot be read."""
        try:
            me = await self.call("getMe")
            return (me or {}).get("username") if isinstance(me, dict) else None
        except Exception:
            return None

    async def download(self, file_id: str, max_bytes: int) -> tuple[bytes, str] | None:
        info = await self.call("getFile", {"file_id": file_id})
        try:
            size = int(info.get("file_size") or 0)
        except (TypeError, ValueError, AttributeError):
            size = 0
        if not isinstance(info, dict) or not info.get("file_path") or size > max_bytes:
            return None
        try:
            r = await self.http.get(f"{API}/file/bot{self.token}/{info['file_path']}")
            r.raise_for_status()
        except httpx.HTTPError as e:
            log.warning("telegram download failed: %s", e.__class__.__name__)
            return None
        if not 0 < len(r.content) <= max_bytes:
            return None
        return r.content, MIME.get(info["file_path"].rsplit(".", 1)[-1].lower(), "application/octet-stream")

    async def get_updates(self, offset: int | None, wait: int = 25) -> list | None:
        """None means the call failed (bad token, another poller running, network): the caller should back off."""
        res = await self.call("getUpdates", {"offset": offset, "timeout": wait, "allowed_updates": ["message", "callback_query"]}, timeout=wait + 10)
        return res if isinstance(res, list) else None

    async def set_webhook(self, url: str, secret: str) -> bool:
        return await self.call("setWebhook", {"url": url, "secret_token": secret, "allowed_updates": ["message", "callback_query"]}) is True

    async def delete_webhook(self) -> None:
        await self.call("deleteWebhook", {"drop_pending_updates": False})


def parse_update(update: dict, bot_key: str) -> tuple[Message | None, str | None]:
    """Telegram update -> (Message, callback id to acknowledge). Private chats only."""
    try:
        uid = update.get("update_id")
        cq = update.get("callback_query")
        m = update.get("message") or (cq or {}).get("message")
        if uid is None or not m or (m.get("chat") or {}).get("type") != "private":
            return None, None
        sender = f"tg:{m['chat']['id']}"
        who = (cq or m).get("from") or {}
        name = who.get("first_name")
        mid = f"tg:{uid}"
        if cq:
            data = cq.get("data") or ""
            rows = (m.get("reply_markup") or {}).get("inline_keyboard") or []
            title = next((b.get("text") for row in rows for b in row if b.get("callback_data") == data and b.get("text")), None)
            return Message(mid, sender, name, bot_key, "interactive", title or data, data, None), cq.get("id")
        sent_at = _unix_time(m.get("date"))
        if m.get("voice") or m.get("audio"):
            return Message(mid, sender, name, bot_key, "audio", "", None, "tg:" + (m.get("voice") or m.get("audio"))["file_id"], sent_at), None
        if m.get("photo"):
            best = max(m["photo"], key=lambda p: p.get("file_size") or p.get("width", 0) * p.get("height", 0))
            return Message(mid, sender, name, bot_key, "image", (m.get("caption") or "").strip(), None, "tg:" + best["file_id"], sent_at), None
        if m.get("contact"):
            return Message(mid, sender, name, bot_key, "text", str(m["contact"].get("phone_number", "")), None, None, sent_at), None
        if m.get("text"):
            text = m["text"].strip()
            if text.split("@")[0] == "/start":
                args = text[len(text.split("@")[0]):].strip()
                if args.startswith("@"):
                    args = args.split(None, 1)[1] if len(args.split(None, 1)) > 1 else ""
                    args = args.strip()
                text = f"/start {args}" if args else "hi"
            return Message(mid, sender, name, bot_key, "text", text, None, None, sent_at), None
        return Message(mid, sender, name, bot_key, "other", "", None, None, sent_at), None
    except (KeyError, TypeError, IndexError, AttributeError):
        log.warning("ignoring a malformed Telegram update")
        return None, None


async def bot_username(wa) -> str | None:
    """The Telegram bot's @username behind a channel sender, or None on WhatsApp-only setups."""
    tg = getattr(wa, "tg", None)
    return await tg.username() if tg is not None else None


class Channels:
    """Presents WhatsApp and Telegram as one sender. The address decides: 'tg:...' goes to Telegram, anything else to WhatsApp."""

    def __init__(self, wa, tg: Telegram | None = None):
        self.wa, self.tg = wa, tg

    async def send_text(self, phone_number_id, token: str, to: str, body: str) -> bool:
        if to.startswith("tg:"):
            return await self.tg.send_text(to[3:], body) if self.tg else False
        return await self.wa.send_text(phone_number_id, token, to, body)

    async def send_buttons(self, phone_number_id, token: str, to: str, body: str, buttons: list[tuple[str, str]]) -> bool:
        if to.startswith("tg:"):
            return await self.tg.send_buttons(to[3:], body, buttons) if self.tg else False
        return await self.wa.send_buttons(phone_number_id, token, to, body, buttons)

    async def send_template(self, phone_number_id, token: str, to: str, template: str, lang: str, body_params: list[str]) -> bool:
        """WhatsApp message templates (the only thing WhatsApp lets a business send outside the 24-hour window). Telegram has no
        templates and no such window, so a Telegram address is refused here and the caller sends plain text instead."""
        if to.startswith("tg:"):
            return False
        return await self.wa.send_template(phone_number_id, token, to, template, lang, body_params)

    async def typing(self, to: str) -> None:
        if to.startswith("tg:") and self.tg:
            await self.tg.typing(to[3:])

    async def download_media(self, token: str, media_id: str, max_bytes: int = 2_000_000):
        if media_id.startswith("tg:"):
            return await self.tg.download(media_id[3:], max_bytes) if self.tg else None
        return await self.wa.download_media(token, media_id, max_bytes)


POLL_ATTEMPTS = 3


async def _handle_with_retries(ctx, msg: Message, handle) -> None:
    """Polling has no job queue behind it, so a failing handler is retried here, a few times, before the update is given up on.
    (inbound() records completion, so a retry of a message that did get through is skipped, not answered twice.)"""
    for attempt in range(1, POLL_ATTEMPTS + 1):
        try:
            await handle(ctx, msg)
            return
        except asyncio.CancelledError:
            raise
        except Exception:
            if attempt == POLL_ATTEMPTS:
                log.error("giving up on Telegram update %s after %d attempts", msg.id, attempt)
                try:
                    from app.messages import t
                    await ctx.wa.send_text(msg.phone_number_id, "", msg.sender, t("en", "went_wrong"))
                except Exception:
                    log.warning("could not tell %s the handler failed", msg.sender)
                return
            log.warning("handling Telegram update %s failed (attempt %d); retrying", msg.id, attempt)
            await asyncio.sleep(2 * attempt)


async def run_poller(ctx, tg: Telegram, handle) -> None:
    """Long polling: no public URL needed, so a laptop is enough for testing. One update at a time, in order."""
    await tg.delete_webhook()
    offset = None
    log.info("Telegram polling started for %s", tg.bot_key)
    while True:
        try:
            updates = await tg.get_updates(offset)
            if updates is None:
                await asyncio.sleep(3)
                continue
            for update in updates:
                msg, cb = parse_update(update, tg.bot_key)
                if cb:
                    await tg.answer_callback(cb)
                if msg:
                    await _handle_with_retries(ctx, msg, handle)
                uid = update.get("update_id") if isinstance(update, dict) else None
                if uid is not None:
                    offset = uid + 1
        except asyncio.CancelledError:
            raise
        except Exception:
            log.exception("telegram polling error")
            await asyncio.sleep(3)
