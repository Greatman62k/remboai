"""Customer invite links, one place. Every link carries the salon slug, so the first
message a customer sends already says which business it is for, on any channel.

Telegram taps open the bot with the slug attached; WhatsApp taps open the central
number with the slug prefilled (WhatsApp cannot auto-send, the customer presses
send). The QR sticker encodes exactly these links."""
from __future__ import annotations

import re
from urllib.parse import quote


def digits(number: str | None) -> str:
    return re.sub(r"\D", "", number or "")


def wa_link(number: str | None, slug: str | None) -> str | None:
    n = digits(number)
    if not n or not slug:
        return None
    return f"https://wa.me/{n}?text={quote(f'/start {slug}')}"


def tg_link(username: str | None, slug: str | None) -> str | None:
    u = (username or "").lstrip("@")
    if not u or not slug:
        return None
    return f"https://t.me/{u}?start={slug}"


def channel_of(sender: str) -> str:
    return "tg" if (sender or "").startswith("tg:") else "wa"


def invite_for(channel: str, slug: str | None, username: str | None = None, number: str | None = None) -> str:
    """The shareable invite for a salon on a channel, with a plain-text fallback
    that still carries the slug when no link can be built."""
    if channel == "tg":
        return tg_link(username, slug) or f"/start {slug}"
    if channel == "wa":
        return wa_link(number, slug) or f"Ask customers to message this WhatsApp number with: /start {slug}"
    return f"/start {slug}"


def qr_png(link: str) -> bytes:
    """The invite link as a printable QR sticker (PNG bytes)."""
    import io

    import qrcode
    buf = io.BytesIO()
    qrcode.make(link).save(buf, format="PNG")
    return buf.getvalue()


def sticker_png(link: str, title: str = "", subtitle: str = "Scan to book") -> bytes:
    """The printable door sticker: the QR code with the shop name above it and a short
    instruction below, on a white card with a quiet margin so phones scan it from a distance."""
    import io

    import qrcode
    from PIL import Image, ImageDraw, ImageFont

    qr = qrcode.QRCode(error_correction=qrcode.constants.ERROR_CORRECT_M, box_size=14, border=2)
    qr.add_data(link)
    qr.make(fit=True)
    code = qr.make_image(fill_color="black", back_color="white").convert("RGB")
    pad, head, foot = 48, (120 if title else 48), 110
    card = Image.new("RGB", (code.width + 2 * pad, code.height + head + foot), "white")
    card.paste(code, (pad, head))
    draw = ImageDraw.Draw(card)

    def font(size: int):
        for name in ("DejaVuSans-Bold.ttf", "Arial Bold.ttf", "arialbd.ttf"):
            try:
                return ImageFont.truetype(name, size)
            except OSError:
                continue
        return ImageFont.load_default(size)

    def centred(text: str, y: int, size: int, fill) -> None:
        f = font(size)
        while size > 14 and draw.textlength(text, font=f) > card.width - 2 * pad:
            size -= 2
            f = font(size)
        draw.text(((card.width - draw.textlength(text, font=f)) / 2, y), text, font=f, fill=fill)

    if title:
        centred(title[:40], 36, 44, (10, 10, 12))
    centred(subtitle, head + code.height + 8, 40, (255, 122, 0))
    centred("Booking by RemboAI", head + code.height + 62, 22, (90, 90, 96))
    buf = io.BytesIO()
    card.save(buf, format="PNG")
    return buf.getvalue()
