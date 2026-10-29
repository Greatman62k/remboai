#!/usr/bin/env python3
"""Create (or update) a salon with a stylist and services, so a real WhatsApp number has something to book.

    DATABASE_URL=... python scripts/seed_salon.py --name "Brian's Kinyozi" --phone-number-id 1234567890 \
        --stylist Brian --service "Fade:500:45:kinyozi,cut" --service "Shave:300:20"

Deposits: add --till HEADOFFICE:TILL (Buy Goods till; the head-office number is the Daraja shortcode) or --paybill NUMBER.
With neither, or --deposit 0, bookings are confirmed without a deposit. --owner is the owner's WhatsApp number (254...), or tg:<chat id> on Telegram (send /id to the bot to see it).
Telegram: add --telegram-bot-id (the number before the colon in the bot token), with or without a WhatsApp number.

--service is NAME:PRICE_KES:MINUTES[:alias,alias]. The phone number ID is the one on Meta's API Setup page (not the phone number)."""
import argparse
import asyncio
import os
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
from app import db  # noqa: E402


async def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--name", required=True)
    ap.add_argument("--phone-number-id", help="WhatsApp phone number ID from Meta's API Setup page")
    ap.add_argument("--telegram-bot-id", help="Telegram bot id: the number before the colon in the bot token")
    ap.add_argument("--stylist", action="append", required=True)
    ap.add_argument("--service", action="append", required=True)
    ap.add_argument("--deposit", type=int, default=200)
    ap.add_argument("--till", help="HEADOFFICE:TILL")
    ap.add_argument("--paybill")
    ap.add_argument("--owner", help="owner's WhatsApp number (2547...) or Telegram id (tg:123456789, send /id to the bot to find it)")
    a = ap.parse_args()
    pool = await db.make_pool(os.environ["DATABASE_URL"])
    await db.migrate(pool)
    if not (a.phone_number_id or a.telegram_bot_id):
        sys.exit("give --phone-number-id (WhatsApp) and/or --telegram-bot-id")
    wa_id, tg_id = a.phone_number_id, f"tg:{a.telegram_bot_id}" if a.telegram_bot_id else None
    salon = await pool.fetchval("SELECT id::text FROM salons WHERE wa_phone_number_id=$1 OR tg_bot_id=$2", wa_id, tg_id)
    if salon:
        await pool.execute(
            "UPDATE salons SET name=$2, deposit_kes=$3, owner_wa_id=COALESCE($4, owner_wa_id), wa_phone_number_id=COALESCE($5, wa_phone_number_id), "
            "tg_bot_id=COALESCE($6, tg_bot_id) WHERE id=$1::uuid", salon, a.name, a.deposit, a.owner, wa_id, tg_id)
    else:
        salon = await pool.fetchval(
            "INSERT INTO salons (name, wa_phone_number_id, tg_bot_id, deposit_kes, owner_wa_id) VALUES ($1,$2,$3,$4,$5) RETURNING id::text",
            a.name, wa_id, tg_id, a.deposit, a.owner)
    if a.till or a.paybill:
        kind, shortcode, party_b = ("till", *a.till.split(":")) if a.till else ("paybill", a.paybill, a.paybill)
        await pool.execute(
            "INSERT INTO salon_payment (salon_id, kind, shortcode, party_b) VALUES ($1::uuid,$2,$3,$4) "
            "ON CONFLICT (salon_id) DO UPDATE SET kind=EXCLUDED.kind, shortcode=EXCLUDED.shortcode, party_b=EXCLUDED.party_b",
            salon, kind, shortcode, party_b)
    staff = []
    for name in a.stylist:
        sid = await pool.fetchval(
            "SELECT id::text FROM staff WHERE salon_id=$1::uuid AND lower(name)=lower($2) ORDER BY id LIMIT 1", salon, name)
        if sid:
            await pool.execute("UPDATE staff SET name=$2, active=true WHERE id=$1::uuid", sid, name)
        else:
            sid = await pool.fetchval("INSERT INTO staff (salon_id, name) VALUES ($1::uuid,$2) RETURNING id::text", salon, name)
        staff.append(sid)
    for spec in a.service:
        parts = spec.split(":")
        name, price, mins = parts[0], int(parts[1]), int(parts[2])
        aliases = [x.strip() for x in parts[3].split(",")] if len(parts) > 3 else []
        sid = await pool.fetchval(
            "SELECT id::text FROM services WHERE salon_id=$1::uuid AND lower(name)=lower($2) ORDER BY id LIMIT 1", salon, name)
        if sid:
            await pool.execute(
                "UPDATE services SET name=$2, aliases=$3, duration_min=$4, price_kes=$5, active=true WHERE id=$1::uuid",
                sid, name, aliases, mins, price)
        else:
            sid = await pool.fetchval(
                "INSERT INTO services (salon_id,name,aliases,duration_min,price_kes) VALUES ($1::uuid,$2,$3,$4,$5) RETURNING id::text",
                salon, name, aliases, mins, price)
        for st in staff:
            await pool.execute("INSERT INTO staff_services VALUES ($1::uuid,$2::uuid) ON CONFLICT DO NOTHING", st, sid)
    print(f"salon {a.name} ready with {len(staff)} stylist(s) and {len(a.service)} service(s)")
    await pool.close()
    return 0


if __name__ == "__main__":
    sys.exit(asyncio.run(main()))
