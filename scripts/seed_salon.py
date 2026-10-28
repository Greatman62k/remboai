#!/usr/bin/env python3
"""Create (or update) a salon with a stylist and services, so a real WhatsApp number has something to book.

    DATABASE_URL=... python scripts/seed_salon.py --name "Brian's Kinyozi" --phone-number-id 1234567890 \
        --stylist Brian --service "Fade:500:45:kinyozi,cut" --service "Shave:300:20"

Deposits: add --till HEADOFFICE:TILL (Buy Goods till; the head-office number is the Daraja shortcode) or --paybill NUMBER.
With neither, or --deposit 0, bookings are confirmed without a deposit. --owner is the owner's WhatsApp number (254...).

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
    ap.add_argument("--phone-number-id", required=True)
    ap.add_argument("--stylist", action="append", required=True)
    ap.add_argument("--service", action="append", required=True)
    ap.add_argument("--deposit", type=int, default=200)
    ap.add_argument("--till", help="HEADOFFICE:TILL")
    ap.add_argument("--paybill")
    ap.add_argument("--owner", help="owner's WhatsApp number, e.g. 2547...")
    a = ap.parse_args()
    pool = await db.make_pool(os.environ["DATABASE_URL"])
    await db.migrate(pool)
    salon = await pool.fetchval(
        "INSERT INTO salons (name, wa_phone_number_id, deposit_kes, owner_wa_id) VALUES ($1,$2,$3,$4) "
        "ON CONFLICT (wa_phone_number_id) DO UPDATE SET name=EXCLUDED.name, deposit_kes=EXCLUDED.deposit_kes, "
        "owner_wa_id=COALESCE(EXCLUDED.owner_wa_id, salons.owner_wa_id) RETURNING id::text",
        a.name, a.phone_number_id, a.deposit, a.owner)
    if a.till or a.paybill:
        kind, shortcode, party_b = ("till", *a.till.split(":")) if a.till else ("paybill", a.paybill, a.paybill)
        await pool.execute(
            "INSERT INTO salon_payment (salon_id, kind, shortcode, party_b) VALUES ($1::uuid,$2,$3,$4) "
            "ON CONFLICT (salon_id) DO UPDATE SET kind=EXCLUDED.kind, shortcode=EXCLUDED.shortcode, party_b=EXCLUDED.party_b",
            salon, kind, shortcode, party_b)
    staff = [await pool.fetchval("INSERT INTO staff (salon_id, name) VALUES ($1::uuid,$2) RETURNING id::text", salon, n) for n in a.stylist]
    for spec in a.service:
        parts = spec.split(":")
        name, price, mins = parts[0], int(parts[1]), int(parts[2])
        aliases = [x.strip() for x in parts[3].split(",")] if len(parts) > 3 else []
        sid = await pool.fetchval("INSERT INTO services (salon_id,name,aliases,duration_min,price_kes) VALUES ($1::uuid,$2,$3,$4,$5) RETURNING id::text",
                                  salon, name, aliases, mins, price)
        for st in staff:
            await pool.execute("INSERT INTO staff_services VALUES ($1::uuid,$2::uuid) ON CONFLICT DO NOTHING", st, sid)
    print(f"salon {a.name} ready with {len(staff)} stylist(s) and {len(a.service)} service(s)")
    await pool.close()
    return 0


if __name__ == "__main__":
    sys.exit(asyncio.run(main()))
