"""Print QR sticker: a PNG of one salon's customer invite link, for the shop door.

A customer scans it, WhatsApp or Telegram opens with the business code already
typed, they press send, and the bot welcomes them to that business.

Telegram:  python scripts/invite_qr.py --slug malaika --channel tg --username rembo_test_bot --out malaika.png
WhatsApp:  python scripts/invite_qr.py --slug malaika --channel wa --number 254700111222 --out malaika.png
"""
import argparse
import os
import sys

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from app.channels.invites import invite_for, qr_png


def main() -> None:
    ap = argparse.ArgumentParser(description="Write a customer invite QR sticker.")
    ap.add_argument("--slug", required=True, help="salon invite code, e.g. malaika")
    ap.add_argument("--channel", choices=("tg", "wa"), default="tg")
    ap.add_argument("--username", default="", help="Telegram bot username (tg only)")
    ap.add_argument("--number", default=os.environ.get("CENTRAL_WHATSAPP_NUMBER", ""), help="central WhatsApp number (wa only)")
    ap.add_argument("--out", required=True, help="PNG file to write")
    a = ap.parse_args()
    link = invite_for(a.channel, a.slug, username=a.username or None, number=a.number or None)
    with open(a.out, "wb") as f:
        f.write(qr_png(link))
    print(link)


if __name__ == "__main__":
    main()
