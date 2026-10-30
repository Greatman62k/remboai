"""WhatsApp Cloud API: verify, parse, send. No business logic lives here."""
from __future__ import annotations

import hashlib
import hmac
import logging
from dataclasses import dataclass

import httpx

log = logging.getLogger("rembo.wa")

ERROR_HINTS = {
    190: "access token expired or invalid; a temporary token lasts about 24 hours, create a permanent system-user token",
    10: "token lacks whatsapp_business_messaging permission",
    100: "invalid parameter; check the phone number ID and that the token belongs to the same app",
    131030: "recipient is not on the test number's allowed list (API Setup > To)",
    131047: "more than 24 hours since the customer last wrote; free text is refused, send a template",
    131026: "message undeliverable; the recipient may not have WhatsApp or has not accepted the terms",
    133010: "phone number not registered with the Cloud API",
    131031: "the WhatsApp business account is locked or restricted; check Business Settings > Security centre and the account quality page, then see Meta's verification notice",
    131042: "payment problem on the WhatsApp business account; check the payment method under WhatsApp Manager > Payment settings",
    131051: "unsupported message type",
    130429: "sending too fast (throughput limit); slow down and retry",
}


def explain(code) -> str:
    try:
        return ERROR_HINTS.get(int(code), "see Meta's WhatsApp Cloud API error-code reference")
    except (TypeError, ValueError):
        return "unknown error code"


def mask(wa_id: str) -> str:
    return "..." + wa_id[-4:] if wa_id else ""


def verify_signature(raw: bytes, header: str | None, app_secret: str) -> bool:
    """X-Hub-Signature-256 is 'sha256=' + HMAC-SHA256 of the exact request bytes, keyed by the app secret."""
    if not header or not header.startswith("sha256="):
        return False
    try:
        given = header[7:].encode("ascii")
    except UnicodeEncodeError:
        return False
    want = hmac.new(app_secret.encode(), raw, hashlib.sha256).hexdigest().encode("ascii")
    return hmac.compare_digest(want, given)


@dataclass(frozen=True, slots=True)
class Message:
    id: str
    sender: str
    name: str | None
    phone_number_id: str
    kind: str
    text: str
    reply_id: str | None = None
    media_id: str | None = None


@dataclass(frozen=True, slots=True)
class Failure:
    to: str
    code: int | None
    title: str


def _values(payload: dict):
    for entry in payload.get("entry") or []:
        for change in entry.get("changes") or []:
            yield change.get("value") or {}


def parse_messages(payload: dict) -> list[Message]:
    out: list[Message] = []
    for v in _values(payload):
        pid = (v.get("metadata") or {}).get("phone_number_id")
        names = {c.get("wa_id"): (c.get("profile") or {}).get("name") for c in v.get("contacts") or []}
        for m in v.get("messages") or []:
            sender, mid, kind = m.get("from"), m.get("id"), m.get("type", "")
            if not (pid and sender and mid):
                continue
            text, reply_id, media_id = "", None, None
            if kind == "audio":
                media_id = (m.get("audio") or {}).get("id")
            elif kind == "image":
                media_id, text = (m.get("image") or {}).get("id"), (m.get("image") or {}).get("caption") or ""
            elif kind == "text":
                text = (m.get("text") or {}).get("body", "")
            elif kind == "interactive":
                reply = (m.get("interactive") or {}).get("button_reply") or (m.get("interactive") or {}).get("list_reply") or {}
                reply_id, text = reply.get("id"), reply.get("title", "")
            out.append(Message(mid, sender, names.get(sender), pid, kind, text.strip(), reply_id, media_id))
    return out


def parse_failures(payload: dict) -> list[Failure]:
    """Delivery receipts that say a message we sent did not arrive."""
    out: list[Failure] = []
    for v in _values(payload):
        for s in v.get("statuses") or []:
            if s.get("status") != "failed":
                continue
            for e in s.get("errors") or [{}]:
                out.append(Failure(s.get("recipient_id", ""), e.get("code"), e.get("title") or e.get("message") or ""))
    return out


class WhatsApp:
    def __init__(self, http: httpx.AsyncClient, graph_version: str):
        self.http, self.version = http, graph_version

    async def send_text(self, phone_number_id: str, token: str, to: str, body: str) -> bool:
        return await self._send(phone_number_id, token, to, {"type": "text", "text": {"body": body[:4096], "preview_url": False}})

    async def send_buttons(self, phone_number_id: str, token: str, to: str, body: str, buttons: list[tuple[str, str]]) -> bool:
        """Up to three quick-reply buttons (id, title); WhatsApp limits a title to 20 characters."""
        action = {"buttons": [{"type": "reply", "reply": {"id": i, "title": t[:20]}} for i, t in buttons[:3]]}
        return await self._send(phone_number_id, token, to, {"type": "interactive", "interactive": {"type": "button", "body": {"text": body[:1024]}, "action": action}})

    async def download_media(self, token: str, media_id: str, max_bytes: int = 2_000_000) -> tuple[bytes, str] | None:
        """Fetch a voice note (or other media) Meta is holding for us. Two steps: ask for the URL, then download it with the same token."""
        headers = {"Authorization": f"Bearer {token}"}
        try:
            info = await self.http.get(f"https://graph.facebook.com/{self.version}/{media_id}", headers=headers)
            info.raise_for_status()
            meta = info.json()
            if int(meta.get("file_size") or 0) > max_bytes:
                log.warning("media %s is too large (%s bytes)", media_id, meta.get("file_size"))
                return None
            blob = await self.http.get(meta["url"], headers=headers)
            blob.raise_for_status()
        except (httpx.HTTPError, KeyError, ValueError) as e:
            log.warning("could not download media %s: %s", media_id, e.__class__.__name__)
            return None
        return (blob.content, meta.get("mime_type") or "audio/ogg") if 0 < len(blob.content) <= max_bytes else None

    async def send_template(self, phone_number_id: str, token: str, to: str, template: str, lang: str, body_params: list[str]) -> bool:
        comps = [{"type": "body", "parameters": [{"type": "text", "text": p[:1024]} for p in body_params]}] if body_params else []
        return await self._send(phone_number_id, token, to, {"type": "template", "template": {"name": template, "language": {"code": lang}, "components": comps}})

    async def _send(self, phone_number_id: str, token: str, to: str, content: dict) -> bool:
        url = f"https://graph.facebook.com/{self.version}/{phone_number_id}/messages"
        payload = {"messaging_product": "whatsapp", "to": to, **content}
        try:
            r = await self.http.post(url, json=payload, headers={"Authorization": f"Bearer {token}"})
        except httpx.HTTPError as e:
            log.warning("send to %s failed: %s", mask(to), e.__class__.__name__)
            return False
        if r.status_code < 400:
            return True
        try:
            err = r.json().get("error") or {}
        except ValueError:
            err = {}
        log.error("send to %s FAILED http=%s code=%s | %s", mask(to), r.status_code, err.get("code"), explain(err.get("code")))
        return False
