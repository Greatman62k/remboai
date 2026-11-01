"""The platform administrators' control hub: list, stats, off/on, delete and warn, for every salon.

Only identities configured in PLATFORM_OWNER_ID or PLATFORM_OWNER_IDS may use it;
everyone else never reaches here (the router checks first)."""
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

SHOPS = re.compile(
    r"^\s*(?:shops|businesses|salons|orodha|list(?:\s+(?:businesses|shops|salons))?|"
    r"show\s+(?:the\s+)?(?:list|businesses|shops|salons)|return\s+(?:the\s+)?(?:list|businesses|shops|salons))\s*[?.!]*$", re.I)
STATS = re.compile(r"^\s*(stats|takwimu)\s*$", re.I)
OFF = re.compile(r"^\s*off\s+(.+?)\s*$", re.I)
ON = re.compile(r"^\s*on\s+(.+?)\s*$", re.I)
DEL = re.compile(r"^\s*del(?:ete)?\s+(.+?)(?:\s+(YES|NDIYO))?\s*$", re.I)
WARN = re.compile(r"^\s*warn\s+(.+?)\s*$", re.I)
HELP = re.compile(r"^\s*(help|msaada|platform)\s*$", re.I)
ROLE = re.compile(r"^\s*(?:what\s+is\s+my\s+role|who\s+am\s+i|role\s+yangu|mimi\s+ni\s+nani|niko\s+role\s+gani)\s*[?.!]*$", re.I)
OWNER_ROUTE = re.compile(r"^\s*/owner(?:@\w+)?(?:\s+(.+?))?\s*$", re.I | re.S)

HELP_TEXT = (
    "Platform administrator commands\n"
    "1. shops: list businesses.\n"
    "2. stats: view platform totals.\n"
    "3. off <business> / on <business>: close or reopen a business.\n"
    "4. warn <business>: send its owner the 7-day notice now.\n"
    "5. del <business>: review the business before deletion. Confirm with "
    "del <business> YES.\n\n"
    "Business ownership\n"
    "1. Register your salon: /start register. This account becomes its owner.\n"
    "2. Register for someone else: have that owner use their own account.\n"
    "3. Use owner commands: /owner <command>. For multiple salons, use "
    "/owner <salon-slug> | <command>.\n"
    "4. Test as a customer: use a separate account."
)

ROLE_TEXT = (
    "You are authenticated as a RemboAI platform administrator.\n\n"
    "Your access\n"
    "1. View businesses: shops. View platform totals: stats.\n"
    "2. Close or reopen a business: off <business> or on <business>.\n"
    "3. Send an owner notice: warn <business>.\n"
    "4. Delete a business: del <business>, then confirm with "
    "del <business> YES.\n\n"
    "Business ownership\n"
    "1. Register your own salon: /start register. This account becomes its owner.\n"
    "2. For someone else's salon, have that owner register from their own account.\n"
    "3. Use salon-owner commands: /owner <command>. For multiple salons, use "
    "/owner <salon-slug> | <command>.\n"
    "4. Test as a customer: use a separate account."
)


def configured_ids(settings) -> set[str]:
    """Support the original single admin ID and a comma-separated backup admin list."""
    out: set[str] = set()
    for attr in ("platform_owner_id", "platform_owner_ids"):
        raw = getattr(settings, attr, "") or ""
        values = raw if isinstance(raw, (list, tuple, set)) else str(raw).split(",")
        out.update(str(value).strip() for value in values if str(value).strip())
    return out


def is_platform(settings, sender: str) -> bool:
    return sender.strip() in configured_ids(settings)


def owner_request(text: str) -> tuple[str | None, str] | None:
    """Parse the platform owner's explicit, salon-scoped owner command route."""
    match = OWNER_ROUTE.fullmatch(text or "")
    if not match:
        return None
    payload = (match.group(1) or "").strip()
    if not payload:
        return None, ""
    if "|" in payload:
        salon, command = payload.split("|", 1)
        salon, command = salon.strip(), command.strip()
        return (salon, command) if salon and command else (None, "")
    return None, payload


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

    if ROLE.match(text or ""):
        await say(ROLE_TEXT)
        return True
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
