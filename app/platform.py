"""The platform owner's control hub: list, stats, off/on, delete and warn, for every salon.

Only the address in PLATFORM_OWNER_ID may use it; everyone else never reaches here
(the router checks first). Replies go plain, since the platform owner has no salon."""
from __future__ import annotations

import logging
import re
from datetime import datetime
from zoneinfo import ZoneInfo

from .channels import outbound
from .db import clear_salon_cache
from .messages import t
from . import lifecycle

log = logging.getLogger("rembo.platform")

SHOPS = re.compile(r"^\s*(shops|businesses|salons|orodha)\s*$", re.I)
STATS = re.compile(r"^\s*(stats|takwimu)\s*$", re.I)
OFF = re.compile(r"^\s*off\s+(.+?)\s*$", re.I)
ON = re.compile(r"^\s*on\s+(.+?)\s*$", re.I)
DEL = re.compile(r"^\s*del(?:ete)?\s+(.+?)(?:\s+(YES|NDIYO))?\s*$", re.I)
WARN = re.compile(r"^\s*warn\s+(.+?)\s*$", re.I)
HELP = re.compile(r"^\s*(help|msaada|platform)\s*$", re.I)

HELP_TEXT = ("Platform hub: shops (list businesses), stats (totals), off <name> / on <name>, "
             "del <name> (asks first, del <name> YES deletes everything), warn <name> (send the 7-day notice now).")


def is_platform(settings, sender: str) -> bool:
    want = (getattr(settings, "platform_owner_id", "") or "").strip()
    return bool(want) and sender.strip() == want


def _like_escape(ref: str) -> str:
    """Escape LIKE wildcards so a typed name only ever matches itself literally."""
    return ref.replace("\\", "\\\\").replace("%", "\\%").replace("_", "\\_")


async def _find(pool, ref: str) -> list[dict]:
    ref = (ref or "").strip()
    rows = await pool.fetch("SELECT id::text, name, slug, active FROM salons WHERE slug=$1", ref.lower())
    if rows:
        return [dict(rows[0])]
    rows = await pool.fetch("SELECT id::text, name, slug, active FROM salons WHERE name ILIKE $1 ESCAPE '\\' ORDER BY name LIMIT 6",
                            f"%{_like_escape(ref)}%")
    return [dict(r) for r in rows]


def _age_line(last, now) -> str:
    if last is None:
        return "no bookings yet"
    days = int((now - last).total_seconds() // 86400)
    return "today" if days <= 0 else ("yesterday" if days == 1 else f"{days}d ago")


async def handle(ctx, sender: str, text: str) -> bool:
    """Run a platform command. True means dealt with (even 'I don't know that one')."""
    pool = ctx.pool
    say = lambda body: outbound.send_plain(ctx, sender, body)  # noqa: E731

    if SHOPS.match(text or ""):
        rows = await pool.fetch(
            "SELECT s.id::text, s.name, s.slug, s.active, count(b.id) AS bookings, max(b.created_at) AS last "
            "FROM salons s LEFT JOIN bookings b ON b.salon_id=s.id GROUP BY s.id ORDER BY s.created_at, s.id")
        now = datetime.now(ZoneInfo("UTC"))
        lines = [f"{'OFF ' if not r['active'] else ''}{r['name']} ({r['slug'] or 'no-link'}): "
                 f"{r['bookings']} bookings, last {_age_line(r['last'], now)}" for r in rows]
        await say("Businesses:\n" + "\n".join(lines) if lines else "No businesses yet.")
        return True
    if STATS.match(text or ""):
        row = await pool.fetchrow("SELECT (SELECT count(*) FROM salons) AS salons, (SELECT count(*) FROM salons WHERE active) AS active, "
                                  "(SELECT count(*) FROM bookings) AS bookings, (SELECT count(*) FROM clients) AS clients, "
                                  "(SELECT count(*) FROM payments WHERE status='paid') AS paid")
        await say(f"Salons: {row['salons']} ({row['active']} active). Bookings: {row['bookings']}. Clients: {row['clients']}. Paid deposits: {row['paid']}.")
        return True
    if HELP.match(text or ""):
        await say(HELP_TEXT)
        return True
    m = OFF.match(text or "") or ON.match(text or "")
    if m:
        found = await _find(pool, m.group(1))
        if len(found) != 1:
            await say("Not sure which business you mean." if not found else
                      "Several match: " + ", ".join(r["name"] for r in found) + ". Use the exact name.")
            return True
        active = text.strip().lower().startswith("on")
        await pool.execute("UPDATE salons SET active=$2 WHERE id=$1::uuid", found[0]["id"], active)
        clear_salon_cache()
        await say(f"{found[0]['name']} is now {'open' if active else 'closed'}.")
        return True
    m = DEL.match(text or "")
    if m:
        found = await _find(pool, m.group(1))
        if len(found) != 1:
            await say("Not sure which business you mean." if not found else
                      "Several match: " + ", ".join(r["name"] for r in found) + ". Use the exact name.")
            return True
        if (m.group(2) or "").upper() not in ("YES", "NDIYO"):
            await say(f"Delete {found[0]['name']} and everything in it (services, bookings, clients)? Reply: del {found[0]['name']} YES")
            return True
        counts = await lifecycle.delete_salon_and_notify(ctx, found[0]["id"])
        await say(f"Deleted {found[0]['name']}: {counts['bookings']} bookings, {counts['payments']} payments. "
                  f"{counts['notified']} customer(s) told.")
        return True
    m = WARN.match(text or "")
    if m:
        found = await _find(pool, m.group(1))
        if len(found) != 1 or not found[0]["active"]:
            await say("Not sure which active business you mean.")
            return True
        row = await pool.fetchrow("SELECT id::text, name, wa_phone_number_id, wa_access_token, owner_wa_id FROM salons WHERE id=$1::uuid", found[0]["id"])
        salon = dict(row)
        if not salon["owner_wa_id"]:
            await say(f"{salon['name']} has no owner contact to warn.")
            return True
        lang = await lifecycle.owner_lang(pool, salon["id"], salon["owner_wa_id"])
        await outbound.send_text(ctx, salon, salon["owner_wa_id"], t(lang, "owner_warn1", salon=salon["name"]))
        await pool.execute("UPDATE salons SET inactivity_warned_at=now() WHERE id=$1::uuid", salon["id"])
        await say(f"Warning sent to the owner of {salon['name']}.")
        return True
    return False
