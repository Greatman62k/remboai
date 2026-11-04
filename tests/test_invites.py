"""Invite links and QR stickers: pure unit tests, no database."""
import pytest

from app.channels.invites import channel_of, invite_for, qr_png, sticker_png, tg_link, wa_link


def test_links_carry_the_slug_on_both_channels():
    assert wa_link("254700111222", "malaika") == "https://wa.me/254700111222?text=/start%20malaika"
    assert wa_link("+254 700 111 222", "malaika") == "https://wa.me/254700111222?text=/start%20malaika"
    assert wa_link("", "malaika") is None and wa_link("254700111222", "") is None
    assert tg_link("rembo_test_bot", "malaika") == "https://t.me/rembo_test_bot?start=malaika"
    assert tg_link("@rembo_test_bot", "malaika") == "https://t.me/rembo_test_bot?start=malaika"
    assert tg_link("", "malaika") is None
    assert channel_of("tg:123") == "tg" and channel_of("254700000001") == "wa"
    assert invite_for("tg", "malaika", username="rembo_test_bot").startswith("https://t.me/")
    assert invite_for("wa", "malaika", number="254700111222").startswith("https://wa.me/")
    assert invite_for("tg", "malaika") == "/start malaika"
    assert "/start malaika" in invite_for("wa", "malaika")


def test_qr_sticker_is_a_real_png():
    pytest.importorskip("qrcode")
    png = qr_png("https://wa.me/254700111222?text=/start%20malaika")
    assert png[:8] == b"\x89PNG\r\n\x1a\n" and len(png) > 500


def test_door_sticker_decodes_back_to_the_invite_link():
    pytest.importorskip("qrcode")
    import io

    from PIL import Image
    link = "https://t.me/rembo_test_bot?start=malaika"
    png = sticker_png(link, "Malaika Cuts & Braids, Westlands with a very long name indeed")
    img = Image.open(io.BytesIO(png))
    assert img.format == "PNG" and img.width > 300 and img.height > img.width
    try:
        import cv2
        import numpy as np
    except ImportError:
        return
    text, _, _ = cv2.QRCodeDetector().detectAndDecode(np.array(img.convert("RGB")))
    assert text == link
