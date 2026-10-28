"""Telegram as a second channel for the same receptionist. Telegram bots need no business verification and no public URL while
testing (long polling), and they carry everything the WhatsApp flow uses: text, tap buttons, voice notes and photos.
Customers and owners are addressed as 'tg:<chat id>'; a salon's bot is 'tg:<bot id>'."""
from __future__ import annotations

import asyncio
import logging

import httpx

from .whatsapp import Message, mask

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
        try:
            r = await self.http.post(f"{API}/bot{self.token}/{method}", json=payload or {}, timeout=timeout)
            body = r.json()
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

    async def answer_callback(self, callback_id: str) -> None:
        await self.call("answerCallbackQuery", {"callback_query_id": callback_id})   # stops the button's spinner

    async def download(self, file_id: str, max_bytes: int) -> tuple[bytes, str] | None:
        info = await self.call("getFile", {"file_id": file_id})
        if not isinstance(info, dict) or not info.get("file_path") or int(info.get("file_size") or 0) > max_bytes:
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
        # WhatsApp hands over the tapped button's title as the text; the conversation expects some text, so do the same.
        rows = (m.get("reply_markup") or {}).get("inline_keyboard") or []
        title = next((b.get("text") for row in rows for b in row if b.get("callback_data") == data and b.get("text")), None)
        return Message(mid, sender, name, bot_key, "interactive", title or data, data, None), cq.get("id")
    if m.get("voice") or m.get("audio"):
        return Message(mid, sender, name, bot_key, "audio", "", None, "tg:" + (m.get("voice") or m.get("audio"))["file_id"]), None
    if m.get("photo"):
        best = max(m["photo"], key=lambda p: p.get("file_size") or p.get("width", 0) * p.get("height", 0))
        return Message(mid, sender, name, bot_key, "image", (m.get("caption") or "").strip(), None, "tg:" + best["file_id"]), None
    if m.get("contact"):  # a shared contact: handy for "which M-Pesa number?"
        return Message(mid, sender, name, bot_key, "text", str(m["contact"].get("phone_number", "")), None, None), None
    if m.get("text"):
        text = m["text"].strip()
        if text.split("@")[0] == "/start":
            text = "hi"
        return Message(mid, sender, name, bot_key, "text", text, None, None), None
    return Message(mid, sender, name, bot_key, "other", "", None, None), None


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

    async def download_media(self, token: str, media_id: str, max_bytes: int = 2_000_000):
        if media_id.startswith("tg:"):
            return await self.tg.download(media_id[3:], max_bytes) if self.tg else None
        return await self.wa.download_media(token, media_id, max_bytes)


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
                offset = update["update_id"] + 1
                msg, cb = parse_update(update, tg.bot_key)
                if cb:
                    await tg.answer_callback(cb)
                if msg:
                    await handle(ctx, msg)
        except asyncio.CancelledError:
            raise
        except Exception:
            log.exception("telegram polling error")
            await asyncio.sleep(3)
