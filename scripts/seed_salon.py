#!/usr/bin/env python3
"""Create (or update) a salon with a stylist and services, so a real WhatsApp number has something to book.

    DATABASE_URL=... python scripts/seed_salon.py --name "Brian's Kinyozi" --phone-number-id 1234567890 \
        --stylist Brian --service "Fade:500:45:kinyozi,cut" --service "Shave:300:20"

Deposits: Daraja STK uses --till HEAD_OFFICE:TILL (the head-office number is the shortcode), or --paybill NUMBER.
A manual till uses only its seven-digit number: --till 5012345 --manual-till. Do not include a head-office number or other details.
With no payment account, or --deposit 0, bookings are confirmed without a deposit. --owner is the owner's WhatsApp number (254...), or tg:<chat id> on Telegram (send /id to the bot to see it).
Telegram: add --telegram-bot-id (the number before the colon in the bot token), with or without a WhatsApp number.

--service is NAME:PRICE_KES:MINUTES[:alias,alias]. The phone number ID is the one on Meta's API Setup page (not the phone number)."""
import argparse
import asyncio
import os
import re
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
from app import db  # noqa: E402


def parse_till(spec: str, *, manual: bool = False) -> tuple[str, str | None, str]:
    """Parse a Daraja HEAD_OFFICE:TILL or a manual till's bare seven-digit number."""
    raw = (spec or "").strip()
    if manual:
        if not re.fullmatch(r"[0-9]{7}", raw):
            raise ValueError("a manual till must be exactly seven digits, e.g. --till 5012345 --manual-till")
        return "till", None, raw
    parts = raw.split(":")
    if len(parts) != 2 or not all(p.strip() for p in parts):
        raise ValueError("Daraja --till must look like HEAD_OFFICE:TILL, e.g. --till 0712345678:5012345; manual tills use --till 5012345 --manual-till")
    return "till", parts[0].strip(), parts[1].strip()


def parse_service(spec: str) -> tuple[str, int, int, list[str]]:
    """NAME:PRICE_KES:MINUTES[:alias,alias] -> (name, price, minutes, aliases). Raises ValueError."""
    parts = (spec or "").split(":")
    if len(parts) < 3 or not parts[0].strip():
        raise ValueError(f"--service must look like NAME:PRICE:MINUTES[:aliases], got {spec!r}")
    try:
        price, mins = int(parts[1]), int(parts[2])
    except ValueError:
        raise ValueError(f"--service price and minutes must be numbers, got {spec!r}")
    aliases = [x.strip() for x in parts[3].split(",")] if len(parts) > 3 else []
    return parts[0].strip(), price, mins, [a for a in aliases if a]


async def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--name", required=True)
    ap.add_argument("--phone-number-id", help="WhatsApp phone number ID from Meta's API Setup page")
    ap.add_argument("--telegram-bot-id", help="Telegram bot id: the number before the colon in the bot token")
    ap.add_argument("--stylist", action="append", required=True)
    ap.add_argument("--service", action="append", required=True)
    ap.add_argument("--deposit", type=int, default=200)
    ap.add_argument("--till", help="Daraja: HEAD_OFFICE:TILL; manual: bare seven-digit till number")
    ap.add_argument("--manual-till", action="store_true", help="use with --till <seven-digit-number>; owner confirms each deposit")
    ap.add_argument("--paybill")
    ap.add_argument("--owner", help="owner's WhatsApp number (2547...) or Telegram id (tg:123456789, send /id to the bot to find it)")
    a = ap.parse_args()
    if a.manual_till and not a.till:
        ap.error("--manual-till requires --till with only the seven-digit till number")
    if a.till and a.paybill:
        ap.error("choose --till or --paybill, not both")
    if a.manual_till and a.paybill:
        ap.error("--manual-till applies to a till, not a paybill")
    try:
        till = parse_till(a.till, manual=a.manual_till) if a.till else None
        services = [parse_service(s) for s in a.service]
    except ValueError as e:
        ap.error(str(e))
    pool = await db.make_pool(os.environ["DATABASE_URL"])
    await db.migrate(pool)
    if not (a.phone_number_id or a.telegram_bot_id):
        sys.exit("give --phone-number-id (WhatsApp) and/or --telegram-bot-id")
    wa_id, tg_id = a.phone_number_id, f"tg:{a.telegram_bot_id}" if a.telegram_bot_id else None
    salon = await pool.fetchval(
        "SELECT id::text FROM salons WHERE (wa_phone_number_id=$1 OR tg_bot_id=$2) AND lower(name)=lower($3) ORDER BY created_at, id LIMIT 1",
        wa_id, tg_id, a.name)
    if salon:
        await pool.execute(
            "UPDATE salons SET name=$2, deposit_kes=$3, owner_wa_id=COALESCE($4, owner_wa_id), wa_phone_number_id=COALESCE($5, wa_phone_number_id), "
            "tg_bot_id=COALESCE($6, tg_bot_id) WHERE id=$1::uuid", salon, a.name, a.deposit, a.owner, wa_id, tg_id)
    else:
        salon = await pool.fetchval(
            "INSERT INTO salons (name, wa_phone_number_id, tg_bot_id, deposit_kes, owner_wa_id) VALUES ($1,$2,$3,$4,$5) RETURNING id::text",
            a.name, wa_id, tg_id, a.deposit, a.owner)
    if till or a.paybill:
        kind, shortcode, party_b = till if till else ("paybill", a.paybill, a.paybill)
        await pool.execute(
            "INSERT INTO salon_payment (salon_id, kind, shortcode, party_b, manual) VALUES ($1::uuid,$2,$3,$4,$5) "
            "ON CONFLICT (salon_id) DO UPDATE SET kind=EXCLUDED.kind, shortcode=EXCLUDED.shortcode, party_b=EXCLUDED.party_b, manual=EXCLUDED.manual",
            salon, kind, shortcode, party_b, bool(a.manual_till and till))
    staff = []
    for name in a.stylist:
        sid = await pool.fetchval(
            "SELECT id::text FROM staff WHERE salon_id=$1::uuid AND lower(name)=lower($2) ORDER BY id LIMIT 1", salon, name)
        if sid:
            await pool.execute("UPDATE staff SET name=$2, active=true WHERE id=$1::uuid", sid, name)
        else:
            sid = await pool.fetchval("INSERT INTO staff (salon_id, name) VALUES ($1::uuid,$2) RETURNING id::text", salon, name)
        staff.append(sid)
    def infer_category(n: str) -> str:
        l = n.lower()
        if any(k in l for k in ("fade", "taper", "cut", "shave", "beard", "shape up", "line up", "crew", "buzz", "caesar", "afro", "kinyozi", "nyoa", "nyoh")):
            return "barbering"
        if any(k in l for k in ("braid", "loc", "dread", "twist", "cornrow", "wig", "weave", "knotless", "box", "suka", "sok", "nywele", "rasta")):
            return "braids"
        if any(k in l for k in ("nail", "mani", "pedi", "kucha", "gel", "akriliki")):
            return "nails"
        if any(k in l for k in ("makeup", "make up", "lash", "lashez", "brow", "kope", "nyusi", "vipodozi", "facial", "poda")):
            return "makeup"
        if any(k in l for k in ("massage", "spa", "facial", "steaming", "treatment", "scrub", "tiba", "kanda", "oga")):
            return "other"
        return "hair"
    for name, price, mins, aliases in services:
        cat = infer_category(name)
        sid = await pool.fetchval(
            "SELECT id::text FROM services WHERE salon_id=$1::uuid AND lower(name)=lower($2) ORDER BY id LIMIT 1", salon, name)
        if sid:
            await pool.execute(
                "UPDATE services SET name=$2, aliases=$3, duration_min=$4, price_kes=$5, active=true, category=$6 WHERE id=$1::uuid",
                sid, name, aliases, mins, price, cat)
        else:
            sid = await pool.fetchval(
                "INSERT INTO services (salon_id,name,aliases,duration_min,price_kes,category) VALUES ($1::uuid,$2,$3,$4,$5,$6) RETURNING id::text",
                salon, name, aliases, mins, price, cat)
        for st in staff:
            await pool.execute("INSERT INTO staff_services VALUES ($1::uuid,$2::uuid) ON CONFLICT DO NOTHING", st, sid)
    print(f"salon {a.name} ready with {len(staff)} stylist(s) and {len(a.service)} service(s)")
    await pool.close()
    return 0


if __name__ == "__main__":
    sys.exit(asyncio.run(main()))
