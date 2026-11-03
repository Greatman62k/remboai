"""The platform administrators' control hub: list, stats, off/on, delete and warn, for every business.

Only identities configured in PLATFORM_OWNER_ID or PLATFORM_OWNER_IDS may use it;
everyone else never reaches here (the router checks first)."""
from __future__ import annotations

import json
import logging
import re
from datetime import datetime
from zoneinfo import ZoneInfo

from .channels import outbound
from .db import clear_salon_cache
from .messages import ensure_help_hint, t
from . import smalltalk
from . import lifecycle

log = logging.getLogger("rembo.platform")

SHOPS = re.compile(
    r"^\s*(?:shops|businesses|salons|orodha|list(?:\s+(?:businesses|shops|salons))?|"
    r"show\s+(?:the\s+)?(?:list|businesses|shops|salons)|return\s+(?:the\s+)?(?:list|businesses|shops|salons))\s*[?.!]*$", re.I)
STATS = re.compile(r"^\s*(stats|status|takwimu)\s*[?.!]*$", re.I)
OFF = re.compile(r"^\s*off\s+(.+?)\s*$", re.I)
ON = re.compile(r"^\s*on\s+(.+?)\s*$", re.I)
DEL = re.compile(r"^\s*del(?:ete)?\s+(.+?)(?:\s+(YES|NDIYO))?\s*$", re.I)
WARN = re.compile(r"^\s*warn\s+(.+?)\s*$", re.I)
HELP = re.compile(r"^\s*(help|msaada|platform)\s*$", re.I)
ROLE = re.compile(r"^\s*(?:what\s+is\s+my\s+role|who\s+am\s+i|role\s+yangu|mimi\s+ni\s+nani|niko\s+role\s+gani)\s*[?.!]*$", re.I)
OWNER_ROUTE = re.compile(r"^\s*/owner(?:@\w+)?(?:\s+(.+?))?\s*$", re.I | re.S)
_SUGGESTION_OFFER = re.compile(
    r"^\s*(?:can|could|may)\s+i\s+(?:recommend|suggest|propose)"
    r"(?:\s+(?:something|a\s+recommendation|a\s+suggestion|an\s+idea|a\s+feature|a\s+change))?\s*[?.!]*$", re.I)
_SAFE_READ_ONLY_ALIASES = {
    "shops": ("shops", "businesses", "salons", "orodha"),
    "stats": ("stats", "status", "takwimu"),
    "help": ("help", "msaada", "platform"),
}


def _one_typo_apart(left: str, right: str) -> bool:
    """Match one insertion, deletion, substitution, or adjacent transposition."""
    if left == right:
        return True
    if abs(len(left) - len(right)) > 1:
        return False
    if len(left) == len(right):
        mismatches = [i for i, (a, b) in enumerate(zip(left, right)) if a != b]
        if len(mismatches) == 1:
            return True
        return (len(mismatches) == 2 and mismatches[1] == mismatches[0] + 1
                and left[mismatches[0]] == right[mismatches[1]]
                and left[mismatches[1]] == right[mismatches[0]])
    short, long = (left, right) if len(left) < len(right) else (right, left)
    i = j = skipped = 0
    while i < len(short) and j < len(long):
        if short[i] == long[j]:
            i += 1
            j += 1
        else:
            skipped += 1
            j += 1
            if skipped > 1:
                return False
    return True


def _correct_read_only_command(text: str) -> str | None:
    """Correct a one-character typo only when it resolves to one safe read command."""
    match = re.fullmatch(r"\s*([a-z]+)\s*[?.!]*\s*", text or "", re.I)
    if not match:
        return None
    word = match.group(1).casefold()
    if len(word) < 4:
        return None
    possible = {command for command, aliases in _SAFE_READ_ONLY_ALIASES.items()
                if any(_one_typo_apart(word, alias) for alias in aliases)}
    return next(iter(possible)) if len(possible) == 1 else None

HELP_TEXT = (
    "Platform administrator help\n"
    "1. List businesses: shops. Example: shops.\n"
    "2. See platform totals: stats.\n"
    "3. Pause or reopen a business: off Brian's Kinyozi or on Brian's Kinyozi.\n"
    "4. Send an inactivity notice: warn Brian's Kinyozi.\n"
    "5. Delete only after checking: del Brian's Kinyozi, then confirm with del Brian's Kinyozi YES.\n\n"
    "Register your own barbershop or salon with /start register. This account will own it. "
    "For owner tasks, try /owner today, or /owner brian-kinyozi | today if you own several businesses.\n"
    "Type HELP anytime and ask me what a command does."
)

HELP_TEXT_SW = (
    "Msaada wa msimamizi wa RemboAI\n"
    "1. Orodhesha biashara: shops. Mfano: shops.\n"
    "2. Angalia jumla za jukwaa: stats.\n"
    "3. Sitisha au washa biashara: off Brian's Kinyozi au on Brian's Kinyozi.\n"
    "4. Tuma taarifa ya kutofanya kazi: warn Brian's Kinyozi.\n"
    "5. Kagua kabla ya kufuta: del Brian's Kinyozi, kisha thibitisha kwa del Brian's Kinyozi YES.\n\n"
    "Sajili kinyozi au saluni yako kwa /start register. Akaunti hii itakuwa mmiliki. "
    "Kwa kazi za mmiliki, jaribu /owner today, au /owner brian-kinyozi | today ukiwa na biashara kadhaa.\n"
    "Tuma HELP au MSAADA wakati wowote, kisha niulize maana ya amri."
)


def help_text(lang: str = "en") -> str:
    return HELP_TEXT_SW if lang == "sw" else HELP_TEXT

ROLE_TEXT = (
    "You are authenticated as a RemboAI platform administrator. This role is tied to this account and remains if you register a business. "
    "The same account can be both platform administrator and business owner.\n\n"
    "Platform tasks\n"
    "1. View businesses: shops. View platform totals: stats.\n"
    "2. Close or reopen a business: off <business> or on <business>.\n"
    "3. Send an owner notice: warn <business>.\n"
    "4. Delete a business: del Brian's Kinyozi, then confirm with del Brian's Kinyozi YES.\n\n"
    "For business-owner actions, use /owner today. To test customer booking, use a separate account. "
    "Type HELP anytime and ask me to explain a command."
)

ROLE_TEXT_SW = (
    "Umetambuliwa kama msimamizi wa jukwaa la RemboAI. Nafasi hii imefungwa kwenye akaunti hii na itaendelea hata ukisajili biashara. "
    "Akaunti moja inaweza kuwa msimamizi wa jukwaa na mmiliki wa biashara.\n\n"
    "Kazi za jukwaa\n"
    "1. Orodha ya biashara: shops. Jumla za jukwaa: stats.\n"
    "2. Sitisha au washa biashara: off Brian's Kinyozi au on Brian's Kinyozi.\n"
    "3. Tuma taarifa kwa mmiliki: warn Brian's Kinyozi.\n"
    "4. Futa biashara: del Brian's Kinyozi, kisha del Brian's Kinyozi YES kuthibitisha.\n\n"
    "Kwa kazi za mmiliki, tumia /owner today. Kujaribu kuweka miadi kama mteja, tumia akaunti tofauti. "
    "Tuma MSAADA wakati wowote uniulize kuhusu amri."
)


def role_text(lang: str = "en") -> str:
    return ROLE_TEXT_SW if lang == "sw" else ROLE_TEXT


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


def is_mutation_command(text: str) -> bool:
    """Recognize explicit platform commands that change business state or notify an owner."""
    return bool(OFF.fullmatch(text or "") or ON.fullmatch(text or "")
                or DEL.fullmatch(text or "") or WARN.fullmatch(text or ""))


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


async def _find_exact(pool, ref: str) -> list[dict]:
    """Resolve a mutation target by its full slug or complete name only."""
    ref = (ref or "").strip()
    if not ref:
        return []
    rows = await pool.fetch("SELECT id::text, name, slug, active FROM salons WHERE slug=$1", ref.lower())
    if rows:
        return [dict(r) for r in rows]
    rows = await pool.fetch(
        "SELECT id::text, name, slug, active FROM salons WHERE lower(name)=lower($1) ORDER BY slug LIMIT 6", ref)
    return [dict(r) for r in rows]


def _age_line(last, now) -> str:
    if last is None:
        return "no bookings yet"
    days = int((now - last).total_seconds() // 86400)
    return "today" if days <= 0 else ("yesterday" if days == 1 else f"{days}d ago")


async def handle(ctx, sender: str, text: str) -> bool:
    """Run a platform command. True means dealt with (even 'I don't know that one')."""
    pool = ctx.pool
    original_text = text or ""
    lang = smalltalk.language(text, "en")

    async def say(body: str) -> None:
        row = await pool.fetchrow(
            "SELECT history FROM platform_agent_sessions WHERE sender=$1 AND updated_at > now() - interval '30 minutes'",
            sender)
        history = row["history"] if row else []
        if isinstance(history, str):
            try:
                history = json.loads(history)
            except ValueError:
                history = []
        history = history if isinstance(history, list) else []
        first_reply = not any(isinstance(item, dict) and item.get("role") == "assistant" for item in history)
        if first_reply:
            body = ensure_help_hint(body, lang)
        saved = [*history[-8:], {"role": "user", "content": original_text[:500]},
                 {"role": "assistant", "content": body[:700]}][-10:]
        await pool.execute(
            "INSERT INTO platform_agent_sessions (sender, history, updated_at) VALUES ($1,$2::jsonb,now()) "
            "ON CONFLICT (sender) DO UPDATE SET history=EXCLUDED.history, updated_at=now()",
            sender, json.dumps(saved, ensure_ascii=False))
        await outbound.send_plain(ctx, sender, body)

    if _SUGGESTION_OFFER.fullmatch(text or ""):
        await say("Of course. What would you like to recommend?")
        return True
    text = _correct_read_only_command(text) or text

    if ROLE.match(text or ""):
        await say(role_text(lang))
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
        row = await pool.fetchrow("SELECT (SELECT count(*) FROM salons) AS businesses, (SELECT count(*) FROM salons WHERE active) AS active, "
                                  "(SELECT count(*) FROM bookings) AS bookings, (SELECT count(*) FROM clients) AS clients, "
                                  "(SELECT count(*) FROM payments WHERE status='paid') AS paid")
        await say(f"Businesses: {row['businesses']} ({row['active']} active). Bookings: {row['bookings']}. Clients: {row['clients']}. Paid deposits: {row['paid']}.")
        return True
    if HELP.match(text or ""):
        await say(help_text(lang))
        return True
    m = OFF.match(text or "") or ON.match(text or "")
    if m:
        found = await _find_exact(pool, m.group(1))
        if not found:
            await say("No exact business match. Send the full business name or its slug.")
            return True
        if len(found) != 1:
            choices = ", ".join(f"{r['name']} ({r['slug']})" for r in found)
            await say("Several businesses have that exact name: " + choices + ". Use a business slug from shops.")
            return True
        active = text.strip().lower().startswith("on")
        await pool.execute("UPDATE salons SET active=$2 WHERE id=$1::uuid", found[0]["id"], active)
        clear_salon_cache()
        await say(f"{found[0]['name']} is now {'open' if active else 'closed'}.")
        return True
    m = DEL.match(text or "")
    if m:
        await pool.execute("DELETE FROM platform_delete_confirmations WHERE requested_at <= now() - interval '5 minutes'")
        found = await _find_exact(pool, m.group(1))
        if not found:
            await pool.execute("DELETE FROM platform_delete_confirmations WHERE sender=$1", sender)
            await say("No exact business match. Send the full business name or its slug.")
            return True
        if len(found) != 1:
            await pool.execute("DELETE FROM platform_delete_confirmations WHERE sender=$1", sender)
            choices = ", ".join(f"{r['name']} ({r['slug']})" for r in found)
            await say("Several businesses have that exact name: " + choices + ". Use a business slug from shops.")
            return True
        if (m.group(2) or "").upper() not in ("YES", "NDIYO"):
            await pool.execute(
                "INSERT INTO platform_delete_confirmations (sender, salon_id, requested_at) VALUES ($1,$2::uuid,now()) "
                "ON CONFLICT (sender) DO UPDATE SET salon_id=EXCLUDED.salon_id, requested_at=now()",
                sender, found[0]["id"])
            await say(f"Review: {found[0]['name']} and everything in it (services, bookings, clients) will be deleted. "
                      f"To confirm within 5 minutes, reply: del {found[0]['name']} YES")
            return True
        confirmation = await pool.fetchrow(
            "DELETE FROM platform_delete_confirmations WHERE sender=$1 AND salon_id=$2::uuid "
            "AND requested_at > now() - interval '5 minutes' RETURNING salon_id", sender, found[0]["id"])
        if not confirmation:
            await pool.execute("DELETE FROM platform_delete_confirmations WHERE sender=$1", sender)
            await say("No matching deletion is awaiting confirmation. First send del with the full business name or slug, "
                      "review the business shown, then confirm with the same exact name within 5 minutes.")
            return True
        counts = await lifecycle.delete_salon_and_notify(ctx, found[0]["id"])
        await say(f"Deleted {found[0]['name']}: {counts['bookings']} bookings, {counts['payments']} payments. "
                  f"{counts['notified']} customer(s) told.")
        return True
    m = WARN.match(text or "")
    if m:
        found = await _find_exact(pool, m.group(1))
        if not found:
            await say("No exact business match. Send the full business name or its slug.")
            return True
        if len(found) != 1:
            choices = ", ".join(f"{r['name']} ({r['slug']})" for r in found)
            await say("Several businesses have that exact name: " + choices + ". Use a business slug from shops.")
            return True
        if not found[0]["active"]:
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
