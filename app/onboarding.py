from __future__ import annotations

import hashlib
import hmac
import html
import re
import secrets
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
from typing import Any
from urllib.parse import quote
import asyncpg
from .db import clear_salon_cache
from .service_categories import CATEGORIES, CATEGORY_CODES

_TOKEN_RE = re.compile(r"^[A-Za-z0-9_-]{32,128}$")
_PHONE_RE = re.compile(r"^[1-9][0-9]{6,14}$")
_PHONE_INPUT_RE = re.compile(r"^\+?[0-9 ()-]+$")
_TIME_RE = re.compile(r"^([01][0-9]|2[0-3]):[0-5][0-9]$")
_MAX_STAFF = 30
_MAX_SERVICES = 40

class OnboardingError(ValueError):
    pass

class InviteUnavailable(OnboardingError):
    pass

class InviteConflict(OnboardingError):
    pass

@dataclass(frozen=True)
class SetupInvite:
    phone_number_id: str
    wa_business_number: str
    expires_at: datetime

@dataclass(frozen=True)
class ServiceInput:
    category: str
    name: str
    duration_min: int
    price_kes: int
    aliases: list[str]

@dataclass(frozen=True)
class SalonSetup:
    name: str
    owner_wa_id: str
    deposit_kes: int
    open_hours: dict[str, list[str] | None]
    staff: list[str]
    services: list[ServiceInput]

async def verify_onboarding_schema(pool) -> None:
    columns = {row["column_name"] for row in await pool.fetch("SELECT column_name FROM information_schema.columns WHERE table_schema='public' AND table_name='salons'")}
    has_invites = await pool.fetchval("SELECT to_regclass('public.salon_setup_invites') IS NOT NULL")
    service_columns = {row["column_name"] for row in await pool.fetch("SELECT column_name FROM information_schema.columns WHERE table_schema='public' AND table_name='services'")}
    if not {"wa_business_number", "owner_wa_id"}.issubset(columns) or not has_invites or "category" not in service_columns:
        raise RuntimeError("database migrations 011 must be applied before starting RemboAI")

def _token_hash(token: str) -> str:
    return hashlib.sha256(token.encode("ascii")).hexdigest()

def csrf_for_token(token: str) -> str:
    return hmac.new(token.encode("ascii"), b"remboai/salon-setup/csrf/v1", hashlib.sha256).hexdigest()

def _valid_token(token: str) -> bool:
    return bool(_TOKEN_RE.fullmatch(token or ""))

def _phone_number_id(value: str) -> str:
    value = (value or "").strip()
    if not value or len(value) > 128 or any(c.isspace() for c in value):
        raise OnboardingError("WhatsApp phone-number ID is invalid.")
    return value

def normalize_phone(value: str, label: str) -> str:
    raw = (value or "").strip()
    if not _PHONE_INPUT_RE.fullmatch(raw):
        raise OnboardingError(f"{label} must be a full international WhatsApp number.")
    number = re.sub(r"[ ()-]", "", raw).lstrip("+")
    if not _PHONE_RE.fullmatch(number):
        raise OnboardingError(f"{label} must be a full international WhatsApp number.")
    return number

def normalize_kenyan_mobile(value: str, label: str) -> str:
    local = re.sub(r"[ ()-]", "", (value or "").strip())
    if not re.fullmatch(r"[17][0-9]{8}", local):
        raise OnboardingError(f"{label} must start with 7 or 1 and have nine digits after +254.")
    return "254" + local

def _text(value: str, label: str, *, minimum: int, maximum: int) -> str:
    value = " ".join((value or "").split())
    if not minimum <= len(value) <= maximum:
        raise OnboardingError(f"{label} must be between {minimum} and {maximum} characters.")
    return value

def _format_kenyan_number(number: str) -> str:
    return f"+{number}" if number.startswith("254") else number

def click_to_chat_link(number: str, salon_name: str = "") -> str:
    text = f"Hello {salon_name}, I'd like to book" if salon_name else "Hello, I'd like to book"
    return f"https://wa.me/{number}?text={quote(text)}"

async def create_setup_invite(pool, wa_phone_number_id: str, wa_business_number: str, hours: int = 168) -> tuple[str, SetupInvite]:
    pid = _phone_number_id(wa_phone_number_id)
    business = normalize_phone(wa_business_number, "Salon WhatsApp number")
    if hours < 1 or hours > 30*24:
        raise OnboardingError("Invite expiry must be between 1 hour and 30 days.")
    token = secrets.token_urlsafe(32)
    while not _valid_token(token):
        token = secrets.token_urlsafe(32)
    token_hash = _token_hash(token)
    expires_at = datetime.now(timezone.utc) + timedelta(hours=hours)
    try:
        async with pool.acquire() as conn, conn.transaction():
            await conn.execute("SELECT pg_advisory_xact_lock(hashtext($1::text))", pid)
            exists = await conn.fetchval("SELECT 1 FROM salons WHERE wa_phone_number_id=$1", pid)
            if exists:
                raise InviteConflict("A salon is already connected to that WhatsApp number.")
            exists_invite = await conn.fetchval("SELECT 1 FROM salon_setup_invites WHERE wa_phone_number_id=$1", pid)
            if exists_invite:
                raise InviteConflict("An invite already exists for that WhatsApp number.")
            conflict_business = await conn.fetchval("SELECT 1 FROM salons WHERE wa_business_number=$1 UNION ALL SELECT 1 FROM salon_setup_invites WHERE wa_business_number=$1 LIMIT 1", business)
            if conflict_business:
                raise InviteConflict("That WhatsApp business number is already in use.")
            await conn.execute("INSERT INTO salon_setup_invites (token_hash, wa_phone_number_id, wa_business_number, expires_at) VALUES ($1,$2,$3,$4)", token_hash, pid, business, expires_at)
    except asyncpg.UniqueViolationError as e:
        raise InviteConflict("That WhatsApp number is already in use.") from e
    return token, SetupInvite(pid, business, expires_at)

async def get_setup_invite(pool, token: str) -> SetupInvite | None:
    if not _valid_token(token):
        return None
    row = await pool.fetchrow("SELECT wa_phone_number_id, wa_business_number, expires_at FROM salon_setup_invites WHERE token_hash=$1 AND expires_at > now()", _token_hash(token))
    if row is None:
        return None
    return SetupInvite(row["wa_phone_number_id"], row["wa_business_number"], row["expires_at"])

def setup_headers() -> dict[str, str]:
    return {"Content-Security-Policy": "default-src 'none'; style-src 'unsafe-inline'; form-action 'self'; base-uri 'none'; frame-ancestors 'none'", "X-Frame-Options": "DENY", "X-Content-Type-Options": "nosniff", "Cache-Control": "no-store"}

def setup_unavailable_page() -> str:
    return "<html><head><meta charset='utf-8'><title>Setup unavailable</title></head><body><h1>This setup link is unavailable</h1><p>It may have expired or already been used.</p></body></html>"

def _page(title: str, body: str) -> str:
    safe_title = html.escape(title)
    return f"<html><head><meta charset='utf-8'><meta name='viewport' content='width=device-width'><title>{safe_title}</title><style>body{{font-family:sans-serif;max-width:600px;margin:2em auto;padding:1em}}label{{display:block;margin:1em 0}}input,select{{width:100%;padding:8px;margin-top:4px}}button{{background:#075e54;color:#fff;padding:12px 24px;border:none;cursor:pointer}} .hint{{color:#666;font-size:0.9em}} .error{{color:#c00;background:#fee;padding:8px}} .grid{{display:grid;grid-template-columns:1fr 1fr;gap:1em}} .notice{{background:#f0f0f0;padding:12px;border-left:4px solid #075e54}}</style></head><body>{body}</body></html>"

def setup_form(token: str, wa_business_number: str, error: str | None = None, values: dict | None = None) -> str:
    values = values or {}
    field = lambda n, d="": html.escape(values.get(n, d), quote=True)
    err = f"<div class='error'>{html.escape(error)}</div>" if error else ""
    csrf = csrf_for_token(token)
    wa_num = html.escape(_format_kenyan_number(wa_business_number), quote=True)
    team_slots = [0,1]
    service_slots = [0,1]
    team_row = lambda i: f"<div><input name='staff_{i}' placeholder='Stylist name' value=\"{field(f'staff_{i}')}\"></div>"
    cat_opts = "".join(f"<option value='{c}'>{html.escape(l)}</option>" for c,l in CATEGORIES)
    service_card = lambda i: f"<div style='border:1px solid #ddd;padding:12px;margin:8px 0'><input name='service_name_{i}' placeholder='Service name' value=\"{field(f'service_name_{i}')}\"><select name='service_category_{i}'>{cat_opts}</select><div class='grid'><input name='service_duration_{i}' type='number' placeholder='Duration min' value=\"{field(f'service_duration_{i}')}\"><input name='service_price_{i}' type='number' placeholder='Price KES' value=\"{field(f'service_price_{i}')}\"></div></div>"
    checked = " checked" if values.get("sunday_open") else ""
    return _page("Salon setup", f"{err}<h1>Set up your salon</h1><p class='hint'>This link can be used once and expires soon.</p><form method='post'><input type='hidden' name='csrf' value='{html.escape(csrf, quote=True)}'><section><label>Salon name<input name='name' maxlength='120' required placeholder='Amina Beauty Lounge' value='{field('name')}'></label><label>Deposit (KES)<input name='deposit_kes' type='number' min='1' max='1000000' required value='{field('deposit_kes','200')}'></label><div class='grid'><label>Open<input name='opening_time' type='time' required value='{field('opening_time','09:00')}'></label><label>Close<input name='closing_time' type='time' required value='{field('closing_time','18:00')}'></label></div><label><input type='checkbox' name='sunday_open'{checked} style='width:auto;margin-right:8px'>Open Sunday</label></section><section><h2>WhatsApp numbers</h2><label>Your WhatsApp (+254)<input name='owner_wa_local' type='tel' required pattern='[17][0-9]{{8}}' maxlength='9' placeholder='712345678' value='{field('owner_wa_local')}'></label><div class='notice'><strong>Salon WhatsApp</strong><input readonly value='{wa_num}'><p class='hint'>Customers message this verified number. Cannot be changed here.</p></div></section><section><h2>Team</h2><div id='team'>{''.join(team_row(i) for i in team_slots)}</div><p class='hint'>Add team members</p></section><section><h2>Services</h2><div>{''.join(service_card(i) for i in service_slots)}</div></section><button type='submit'>Activate</button></form>")

def setup_complete_page(salon: dict[str, Any]) -> str:
    link = click_to_chat_link(salon["wa_business_number"], salon["name"])
    safe_link = html.escape(link, quote=True)
    return _page("Salon setup complete", f"<h1>Your booking assistant is ready</h1><p>Share this booking link:</p><p><a href='{safe_link}'>{safe_link}</a></p><p class='hint'>Customers go straight to WhatsApp with a booking message.</p>")

def parse_setup_form(form: dict[str, str], wa_business_number: str) -> SalonSetup:
    name = _text(form.get("name",""), "Salon name", minimum=2, maximum=120)
    deposit = int(form.get("deposit_kes","200") or 200)
    if not 1 <= deposit <= 1000000:
        raise OnboardingError("Deposit must be between 1 and 1,000,000.")
    opening = form.get("opening_time","09:00")
    closing = form.get("closing_time","18:00")
    if not _TIME_RE.fullmatch(opening) or not _TIME_RE.fullmatch(closing):
        raise OnboardingError("Opening and closing times must be HH:MM.")
    open_h, open_m = map(int, opening.split(":"))
    close_h, close_m = map(int, closing.split(":"))
    if (open_h, open_m) >= (close_h, close_m):
        raise OnboardingError("Opening time must be before closing time.")
    sunday_open = bool(form.get("sunday_open"))
    days = ["mon","tue","wed","thu","fri","sat","sun"]
    open_hours = {}
    for d in days:
        if d == "sun" and not sunday_open:
            open_hours[d] = None
        else:
            open_hours[d] = [opening, closing]
    owner_local = form.get("owner_wa_local","")
    owner_wa_id = normalize_kenyan_mobile(owner_local, "Your WhatsApp number")
    staff = []
    for i in range(_MAX_STAFF):
        n = _text(form.get(f"staff_{i}",""), f"Staff {i}", minimum=1, maximum=60) if form.get(f"staff_{i}") and form.get(f"staff_{i}").strip() else None
        if n:
            if len(staff) >= 30:
                raise OnboardingError("Too many team members.")
            staff.append(n)
    if not staff:
        raise OnboardingError("Add at least one team member.")
    services = []
    for i in range(_MAX_SERVICES):
        sname = form.get(f"service_name_{i}","").strip()
        if not sname:
            continue
        sname = _text(sname, f"Service {i}", minimum=2, maximum=80)
        cat = form.get(f"service_category_{i}","other")
        if cat not in CATEGORY_CODES:
            cat = "other"
        try:
            dur = int(form.get(f"service_duration_{i}","60"))
            price = int(form.get(f"service_price_{i}","500"))
        except ValueError:
            raise OnboardingError("Duration and price must be numbers.")
        if not 5 <= dur <= 600:
            raise OnboardingError("Duration must be 5-600 minutes.")
        if not 0 <= price <= 1000000:
            raise OnboardingError("Price must be 0-1,000,000.")
        services.append(ServiceInput(cat, sname, dur, price, []))
    if not services:
        raise OnboardingError("Add at least one service.")
    return SalonSetup(name, owner_wa_id, deposit, open_hours, staff, services)

async def complete_setup_invite(pool, token: str, setup: SalonSetup) -> dict[str, Any]:
    if not _valid_token(token):
        raise InviteUnavailable("This setup link is unavailable.")
    token_hash = _token_hash(token)
    async with pool.acquire() as conn, conn.transaction():
        invite = await conn.fetchrow("SELECT wa_phone_number_id, wa_business_number FROM salon_setup_invites WHERE token_hash=$1 AND expires_at > now() FOR UPDATE", token_hash)
        if invite is None:
            raise InviteUnavailable("This setup link is unavailable.")
        pid = invite["wa_phone_number_id"]
        business = invite["wa_business_number"]
        exists = await conn.fetchval("SELECT 1 FROM salons WHERE wa_phone_number_id=$1", pid)
        if exists:
            raise InviteConflict("A salon is already connected to that WhatsApp number.")
        salon_id = await conn.fetchval("INSERT INTO salons (name, wa_phone_number_id, wa_business_number, owner_wa_id, deposit_kes, open_hours, timezone) VALUES ($1,$2,$3,$4,$5,$6::jsonb,'Africa/Nairobi') RETURNING id::text", setup.name, pid, business, setup.owner_wa_id, setup.deposit_kes, __import__("json").dumps(setup.open_hours))
        # staff
        staff_ids = []
        for sname in setup.staff:
            sid = await conn.fetchval("INSERT INTO staff (salon_id, name) VALUES ($1::uuid,$2) RETURNING id::text", salon_id, sname)
            staff_ids.append(sid)
        # services
        for svc in setup.services:
            sid = await conn.fetchval("INSERT INTO services (salon_id, name, aliases, duration_min, price_kes, category) VALUES ($1::uuid,$2,'{}',$3,$4,$5) RETURNING id::text", salon_id, svc.name, svc.duration_min, svc.price_kes, svc.category)
            for staff_id in staff_ids:
                await conn.execute("INSERT INTO staff_services (staff_id, service_id) VALUES ($1::uuid,$2::uuid) ON CONFLICT DO NOTHING", staff_id, sid)
        # default payment account if not exists? leave empty
        await conn.execute("DELETE FROM salon_setup_invites WHERE token_hash=$1", token_hash)
        row = await conn.fetchrow("SELECT id::text, name, wa_phone_number_id, wa_business_number FROM salons WHERE id=$1::uuid", salon_id)
        clear_salon_cache(pid)
        return dict(row)
