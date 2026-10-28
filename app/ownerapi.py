"""The owner's JSON API. The web page is a thin client of this, and a native (Kotlin) app would be another one:
sign in once with the single-use link from WhatsApp, then send the session token as `Authorization: Bearer <token>`
(the browser gets the same token as an HttpOnly cookie). Every query is scoped to the signed-in salon."""
from __future__ import annotations

import hashlib
import json
import re
import secrets
import uuid
from datetime import date, datetime, time, timedelta
from zoneinfo import ZoneInfo

from fastapi import APIRouter, Depends, HTTPException, Request, Response
from pydantic import BaseModel, Field, field_validator

from . import owner
from .availability import DAY_KEYS

router = APIRouter(prefix="/api/owner")
COOKIE = "rembo_session"
SESSION_DAYS = 14
HHMM = re.compile(r"^([01]\d|2[0-3]):[0-5]\d$")


# ---- sign-in ------------------------------------------------------------------------------------------------------

def _hash(token: str) -> str:
    return hashlib.sha256(token.encode()).hexdigest()


class Login(BaseModel):
    token: str = Field(min_length=20, max_length=200)


@router.post("/session")
async def sign_in(body: Login, request: Request, response: Response):
    """Exchange the single-use link token for a session. Returns the session token (for apps) and sets the cookie (for browsers)."""
    pool = request.app.state.pool
    h = _hash(body.token)
    salon_id = await pool.fetchval(
        "UPDATE owner_links SET used_at=now() WHERE token_hash=$1 AND used_at IS NULL AND expires_at > now() RETURNING salon_id::text", h)
    if salon_id is None:
        raise HTTPException(401, "This link has expired or was already used. Send \"link\" to the salon's WhatsApp number for a new one.")
    session = secrets.token_urlsafe(32)
    expires = datetime.now(ZoneInfo("UTC")) + timedelta(days=SESSION_DAYS)
    await pool.execute("INSERT INTO owner_sessions (token_hash, salon_id, expires_at) VALUES ($1,$2::uuid,$3)", _hash(session), salon_id, expires)
    secure = request.app.state.settings.public_base_url.startswith("https://")
    response.set_cookie(COOKIE, session, max_age=SESSION_DAYS * 86400, httponly=True, secure=secure, samesite="strict", path="/")
    return {"session_token": session, "expires_at": expires.isoformat()}


async def current_salon(request: Request) -> dict:
    auth = request.headers.get("authorization", "")
    token, via_cookie = (auth[7:], False) if auth.lower().startswith("bearer ") else (request.cookies.get(COOKIE, ""), True)
    if not token:
        raise HTTPException(401, "Not signed in")
    if via_cookie and request.method not in ("GET", "HEAD") and request.headers.get("x-rembo") != "1":
        raise HTTPException(403, "Missing X-Rembo header")  # a custom header cannot be sent cross-site: this is the CSRF guard
    row = await request.app.state.pool.fetchrow(
        "SELECT s.id::text, s.name, s.wa_phone_number_id, s.timezone, s.open_hours, s.deposit_kes, s.owner_wa_id "
        "FROM owner_sessions o JOIN salons s ON s.id=o.salon_id WHERE o.token_hash=$1 AND o.expires_at > now() AND s.active", _hash(token))
    if row is None:
        raise HTTPException(401, "Session expired")
    salon = dict(row)
    if isinstance(salon["open_hours"], str):
        salon["open_hours"] = json.loads(salon["open_hours"])
    return salon


Salon = Depends(current_salon)


@router.post("/signout")
async def sign_out(request: Request, response: Response, salon: dict = Salon):
    auth = request.headers.get("authorization", "")
    token = auth[7:] if auth.lower().startswith("bearer ") else request.cookies.get(COOKIE, "")
    await request.app.state.pool.execute("DELETE FROM owner_sessions WHERE token_hash=$1", _hash(token))
    response.delete_cookie(COOKIE, path="/")
    return {"ok": True}


def _uid(value: str) -> str:
    """Path ids must be UUIDs; anything else is simply 'not found'."""
    try:
        return str(uuid.UUID(value))
    except ValueError:
        raise HTTPException(404)


def _tz(salon: dict) -> ZoneInfo:
    return ZoneInfo(salon["timezone"])


# ---- the salon ----------------------------------------------------------------------------------------------------

class SalonPatch(BaseModel):
    name: str | None = Field(None, min_length=1, max_length=80)
    deposit_kes: int | None = Field(None, ge=0, le=5000)
    open_hours: dict[str, list[str] | None] | None = None

    @field_validator("open_hours")
    @classmethod
    def _hours(cls, v):
        if v is None:
            return v
        if set(v) - set(DAY_KEYS):
            raise ValueError("days must be mon..sun")
        for day, rng in v.items():
            if rng is not None and not (len(rng) == 2 and all(HHMM.match(x) for x in rng) and rng[0] < rng[1]):
                raise ValueError(f"{day}: use [\"08:00\", \"19:00\"] with opening before closing, or null for closed")
        return v


@router.get("/me")
async def me(request: Request, salon: dict = Salon):
    pool = request.app.state.pool
    acct = await pool.fetchrow("SELECT kind, shortcode, party_b FROM salon_payment WHERE salon_id=$1::uuid", salon["id"])
    hours = {k: salon["open_hours"].get(k) for k in DAY_KEYS}
    return {"name": salon["name"], "timezone": salon["timezone"], "deposit_kes": salon["deposit_kes"], "open_hours": hours,
            "whatsapp_number_id": salon["wa_phone_number_id"], "payment": dict(acct) if acct else None}


@router.patch("/salon")
async def patch_salon(body: SalonPatch, request: Request, salon: dict = Salon):
    pool = request.app.state.pool
    if body.name is not None:
        await pool.execute("UPDATE salons SET name=$2 WHERE id=$1::uuid", salon["id"], body.name.strip())
    if body.deposit_kes is not None:
        await pool.execute("UPDATE salons SET deposit_kes=$2 WHERE id=$1::uuid", salon["id"], body.deposit_kes)
    if body.open_hours is not None:
        merged = {**salon["open_hours"], **body.open_hours}
        await pool.execute("UPDATE salons SET open_hours=$2::jsonb WHERE id=$1::uuid", salon["id"], json.dumps(merged))
    return {"ok": True}


# ---- bookings -----------------------------------------------------------------------------------------------------

@router.get("/bookings")
async def bookings(request: Request, day: date | None = None, salon: dict = Salon):
    tz = _tz(salon)
    day = day or datetime.now(tz).date()
    rows = await request.app.state.pool.fetch(
        "SELECT b.id::text, b.starts_at, b.ends_at, b.status, sv.name AS service, st.name AS stylist, c.name AS client, c.wa_id AS phone, "
        "(SELECT sum(amount_kes) FROM payments p WHERE p.booking_id=b.id AND p.status='paid') AS paid_kes "
        "FROM bookings b JOIN services sv ON sv.id=b.service_id JOIN staff st ON st.id=b.staff_id JOIN clients c ON c.id=b.client_id "
        "WHERE b.salon_id=$1::uuid AND b.status IN ('confirmed','held') AND b.starts_at >= $2 AND b.starts_at < $3 "
        "AND (b.status='confirmed' OR b.hold_expires_at > now()) ORDER BY b.starts_at",
        salon["id"], datetime.combine(day, time(0), tz), datetime.combine(day + timedelta(days=1), time(0), tz))
    return {"date": day.isoformat(), "bookings": [
        {**dict(r), "starts_at": r["starts_at"].astimezone(tz).isoformat(), "ends_at": r["ends_at"].astimezone(tz).isoformat(),
         "time": r["starts_at"].astimezone(tz).strftime("%H:%M"), "phone": "+" + r["phone"], "paid_kes": r["paid_kes"] or 0} for r in rows]}


@router.post("/bookings/{booking_id}/cancel")
async def cancel(booking_id: str, request: Request, salon: dict = Salon):
    row = await owner.cancel_booking(request.app.state.ctx, salon, _uid(booking_id), _tz(salon))
    if row is None:
        raise HTTPException(404, "No such open booking")
    return {"cancelled": True, "customer_told": True, "refund_due_kes": row["paid"] or 0, "refund_to": "+" + row["phone"] if row["paid"] else None}


# ---- time off -----------------------------------------------------------------------------------------------------

class TimeOffIn(BaseModel):
    date: date
    start: str | None = Field(None, pattern=r"^([01]\d|2[0-3]):[0-5]\d$")
    end: str | None = Field(None, pattern=r"^([01]\d|2[0-3]):[0-5]\d$")
    note: str | None = Field(None, max_length=100)


@router.get("/time_off")
async def time_off(request: Request, salon: dict = Salon):
    tz = _tz(salon)
    rows = await request.app.state.pool.fetch(
        "SELECT id::text, starts_at, ends_at, note FROM time_off WHERE salon_id=$1::uuid AND ends_at > now() ORDER BY starts_at", salon["id"])
    return [{"id": r["id"], "starts_at": r["starts_at"].astimezone(tz).isoformat(), "ends_at": r["ends_at"].astimezone(tz).isoformat(), "note": r["note"]} for r in rows]


@router.post("/time_off", status_code=201)
async def add_time_off(body: TimeOffIn, request: Request, salon: dict = Salon):
    tz = _tz(salon)
    if (body.start is None) != (body.end is None):
        raise HTTPException(422, "Give both start and end, or neither for the whole day")
    start = datetime.combine(body.date, time.fromisoformat(body.start) if body.start else time(0), tz)
    end = datetime.combine(body.date, time.fromisoformat(body.end), tz) if body.end else datetime.combine(body.date + timedelta(days=1), time(0), tz)
    if end <= start:
        raise HTTPException(422, "End must be after start")
    pool = request.app.state.pool
    tid = await pool.fetchval("INSERT INTO time_off (salon_id, starts_at, ends_at, note) VALUES ($1::uuid,$2,$3,$4) RETURNING id::text",
                              salon["id"], start, end, body.note)
    clash = await pool.fetch(
        "SELECT b.id::text, b.starts_at, sv.name AS service, c.name AS client FROM bookings b JOIN services sv ON sv.id=b.service_id "
        "JOIN clients c ON c.id=b.client_id WHERE b.salon_id=$1::uuid AND b.status IN ('confirmed','held') AND b.starts_at < $3 AND b.ends_at > $2 "
        "ORDER BY b.starts_at", salon["id"], start, end)
    return {"id": tid, "existing_bookings_in_that_time": [{"id": r["id"], "time": r["starts_at"].astimezone(tz).strftime("%H:%M"),
                                                             "service": r["service"], "client": r["client"]} for r in clash]}


@router.delete("/time_off/{time_off_id}")
async def remove_time_off(time_off_id: str, request: Request, salon: dict = Salon):
    n = await request.app.state.pool.execute("DELETE FROM time_off WHERE id=$1::uuid AND salon_id=$2::uuid", _uid(time_off_id), salon["id"])
    if n.endswith(" 0"):
        raise HTTPException(404)
    return {"ok": True}


# ---- services and stylists ----------------------------------------------------------------------------------------

class ServiceIn(BaseModel):
    name: str = Field(min_length=1, max_length=60)
    price_kes: int = Field(ge=0, le=200_000)
    duration_min: int = Field(ge=5, le=600)
    aliases: list[str] = Field(default_factory=list, max_length=10)

    @field_validator("aliases")
    @classmethod
    def _aliases(cls, v):
        return [a.strip().lower()[:30] for a in v if a.strip()][:10]


class ServicePatch(BaseModel):
    name: str | None = Field(None, min_length=1, max_length=60)
    price_kes: int | None = Field(None, ge=0, le=200_000)
    duration_min: int | None = Field(None, ge=5, le=600)
    aliases: list[str] | None = Field(None, max_length=10)
    active: bool | None = None


def _service_row(r) -> dict:
    return {"id": r["id"], "name": r["name"], "price_kes": r["price_kes"], "duration_min": r["duration_min"], "aliases": list(r["aliases"]), "active": r["active"]}


@router.get("/services")
async def services(request: Request, salon: dict = Salon):
    rows = await request.app.state.pool.fetch(
        "SELECT id::text, name, price_kes, duration_min, aliases, active FROM services WHERE salon_id=$1::uuid ORDER BY active DESC, name", salon["id"])
    return [_service_row(r) for r in rows]


@router.post("/services", status_code=201)
async def add_service(body: ServiceIn, request: Request, salon: dict = Salon):
    pool = request.app.state.pool
    async with pool.acquire() as con, con.transaction():
        row = await con.fetchrow(
            "INSERT INTO services (salon_id, name, aliases, duration_min, price_kes) VALUES ($1::uuid,$2,$3,$4,$5) "
            "RETURNING id::text, name, price_kes, duration_min, aliases, active", salon["id"], body.name.strip(), body.aliases, body.duration_min, body.price_kes)
        await con.execute("INSERT INTO staff_services (staff_id, service_id) SELECT id, $2::uuid FROM staff WHERE salon_id=$1::uuid AND active",
                          salon["id"], row["id"])
    return _service_row(row)


@router.patch("/services/{service_id}")
async def patch_service(service_id: str, body: ServicePatch, request: Request, salon: dict = Salon):
    fields = {k: v for k, v in body.model_dump().items() if v is not None}
    if "aliases" in fields:
        fields["aliases"] = [a.strip().lower()[:30] for a in fields["aliases"] if a.strip()]
    if "name" in fields:
        fields["name"] = fields["name"].strip()
    if not fields:
        raise HTTPException(422, "Nothing to change")
    sets = ", ".join(f"{k}=${i + 3}" for i, k in enumerate(fields))   # column names come from the model above, never from the request
    row = await request.app.state.pool.fetchrow(
        f"UPDATE services SET {sets} WHERE id=$1::uuid AND salon_id=$2::uuid RETURNING id::text, name, price_kes, duration_min, aliases, active",
        _uid(service_id), salon["id"], *fields.values())
    if row is None:
        raise HTTPException(404)
    return _service_row(row)


class StaffIn(BaseModel):
    name: str = Field(min_length=1, max_length=60)


class StaffPatch(BaseModel):
    active: bool


@router.get("/staff")
async def staff(request: Request, salon: dict = Salon):
    rows = await request.app.state.pool.fetch("SELECT id::text, name, active FROM staff WHERE salon_id=$1::uuid ORDER BY active DESC, name", salon["id"])
    return [dict(r) for r in rows]


@router.post("/staff", status_code=201)
async def add_staff(body: StaffIn, request: Request, salon: dict = Salon):
    pool = request.app.state.pool
    async with pool.acquire() as con, con.transaction():
        row = await con.fetchrow("INSERT INTO staff (salon_id, name) VALUES ($1::uuid,$2) RETURNING id::text, name, active", salon["id"], body.name.strip())
        await con.execute("INSERT INTO staff_services (staff_id, service_id) SELECT $2::uuid, id FROM services WHERE salon_id=$1::uuid AND active",
                          salon["id"], row["id"])
    return dict(row)


@router.patch("/staff/{staff_id}")
async def patch_staff(staff_id: str, body: StaffPatch, request: Request, salon: dict = Salon):
    row = await request.app.state.pool.fetchrow("UPDATE staff SET active=$3 WHERE id=$1::uuid AND salon_id=$2::uuid RETURNING id::text, name, active",
                                                _uid(staff_id), salon["id"], body.active)
    if row is None:
        raise HTTPException(404)
    return dict(row)


# ---- money --------------------------------------------------------------------------------------------------------

@router.get("/summary")
async def summary(request: Request, salon: dict = Salon):
    tz = _tz(salon)
    start = datetime.combine(datetime.now(tz).date(), time(0), tz)
    pool = request.app.state.pool
    money, count = await pool.fetchrow(
        "SELECT COALESCE(sum(amount_kes),0), count(*) FROM payments WHERE salon_id=$1::uuid AND status='paid' AND settled_at >= $2 AND settled_at < $3",
        salon["id"], start, start + timedelta(days=1))
    owed = await pool.fetchval("SELECT count(*) FROM payments WHERE salon_id=$1::uuid AND status='needs_refund'", salon["id"])
    return {"deposits_today_kes": int(money), "deposits_today_count": count, "refunds_waiting": owed}


@router.get("/refunds")
async def refunds(request: Request, salon: dict = Salon):
    tz = _tz(salon)
    rows = await request.app.state.pool.fetch(
        "SELECT p.id::text, p.amount_kes, p.phone, p.mpesa_receipt, p.refund_reason, p.settled_at, b.starts_at FROM payments p "
        "JOIN bookings b ON b.id=p.booking_id WHERE p.salon_id=$1::uuid AND p.status='needs_refund' ORDER BY p.settled_at", salon["id"])
    return [{"id": r["id"], "amount_kes": r["amount_kes"], "phone": "+" + r["phone"], "receipt": r["mpesa_receipt"], "reason": r["refund_reason"],
             "booking_time": r["starts_at"].astimezone(tz).isoformat()} for r in rows]


@router.post("/refunds/{payment_id}/done")
async def refund_done(payment_id: str, request: Request, salon: dict = Salon):
    """The owner has sent the money back from the till; stop reminding him."""
    n = await request.app.state.pool.execute("UPDATE payments SET status='refunded' WHERE id=$1::uuid AND salon_id=$2::uuid AND status='needs_refund'",
                                              _uid(payment_id), salon["id"])
    if n.endswith(" 0"):
        raise HTTPException(404)
    return {"ok": True}
