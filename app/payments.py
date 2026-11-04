"""Deposits. Code decides every money state; the model never sees a payment.

Lifecycle of one prompt:  request_deposit -> (callback | status query) -> settle -> announce.
settle() is idempotent: Safaricom may deliver a callback twice, and the sweeper may race it."""
from __future__ import annotations

import asyncio
import dataclasses
import json
import logging
import re
from dataclasses import dataclass
from datetime import datetime
from zoneinfo import ZoneInfo

import asyncpg

from . import mpesa
from .channels import outbound
from .bookings import expire_holds
from .messages import has, t
from .metrics import bookings_confirmed, payments_total
from .mpesa import MpesaError, Outcome
from .channels.whatsapp import mask

log = logging.getLogger("rembo.pay")

RETRY_AFTER_S = 45
EXTEND_HOLD = "4 minutes"
STALE_AFTER_MIN = 6


async def account(pool, salon_id: str) -> dict | None:
    row = await pool.fetchrow("SELECT kind, shortcode, party_b, account_ref, manual, phone_type FROM salon_payment WHERE salon_id=$1::uuid", salon_id)
    return dict(row) if row else None


# Manual claims are owner-verified, so accept references from M-PESA and Airtel Money. References
# may be alphanumeric or numeric; the owner must still verify every claim in the destination account.
_CODE = re.compile(r"\b(?=[A-Z0-9]*[A-Z])(?=[A-Z0-9]*\d)[A-Z0-9]{6,20}\b")
_NUMERIC_REFERENCE = re.compile(r"\b\d{8,20}\b")
_STRICT_ALPHA_REFERENCE = re.compile(r"(?=[A-Z0-9]*[A-Z])(?=[A-Z0-9]*\d)[A-Z0-9]{6,20}")
# Currency-prefixed amounts may be small (for example Ksh50.00); bare amounts must
# have at least three digits unless they directly precede a transaction reference.
# Boundaries prevent phone, till and transaction-reference digits being read as money.
_AMOUNT = re.compile(
    r"(?<![A-Z0-9])(?:KES|KSH)\s*(\d{1,6})(?!\d)|"
    r"(?<![A-Z0-9])(\d{3,6})(?![A-Z0-9])"
)
# Small deposits need explicit context so short date/time components in a pasted SMS are not treated as amounts.
_SMALL_AMOUNT = re.compile(
    r"(?<![A-Z0-9])(\d{1,2})\s+(?=[A-Z0-9]{6,20}\b)"
)

MANUAL_HOLD = "30 minutes"


def normalize_reference(value: object) -> str | None:
    """Normalize one exact M-PESA or Airtel transaction reference, never a phone number."""
    if not isinstance(value, str):
        return None
    candidate = value.strip().upper().rstrip(".,:;")
    valid = bool(_STRICT_ALPHA_REFERENCE.fullmatch(candidate) or _NUMERIC_REFERENCE.fullmatch(candidate))
    if not valid or mpesa.normalize_msisdn(candidate):
        return None
    return candidate


def manual_provider(acct: dict | None) -> str:
    """Provider for a configured manual destination."""
    if acct and acct.get("kind") == "phone" and acct.get("phone_type") == "airtel_money":
        return "airtel_money"
    return "mpesa"


def parse_claim(text: str) -> tuple[int | None, str | None, bool]:
    """Read an owner-verified manual claim: (amount, transaction reference, ambiguous)."""
    upper = (text or "").upper()
    reference = next((m for m in _CODE.finditer(upper)
                      if not re.fullmatch(r"(?:KES|KSH)\d{3,6}", m.group(0))), None)
    numeric_reference = _NUMERIC_REFERENCE.search(upper) if reference is None else None
    code = reference.group(0) if reference else numeric_reference.group(0) if numeric_reference else None
    if code and mpesa.normalize_msisdn(code):
        code = None
    found = {int(value) for m in _AMOUNT.finditer(upper) if (value := m.group(1) or m.group(2))}
    found.update(int(value) for m in _SMALL_AMOUNT.finditer(upper) if (value := m.group(1) or m.group(2)))
    found = sorted(found)
    if len(found) > 1:
        return None, code, True
    return (found[0] if found else None), code, False


def display_msisdn(number: str) -> str:
    """Format a normalized Kenyan mobile number for payment instructions."""
    digits = re.sub(r"\D", "", number or "")
    if len(digits) == 12 and digits.startswith("254"):
        local = "0" + digits[3:]
        return f"{local[:4]} {local[4:7]} {local[7:]}"
    return number


def destination_label(acct: dict | None, lang: str = "en") -> str:
    """Return the precise payment route and destination, without Daraja shortcode details."""
    if not acct:
        return "no payment destination configured" if lang != "sw" else "hakuna sehemu ya malipo iliyowekwa"
    kind, party = acct.get("kind"), str(acct.get("party_b") or "")
    if kind == "phone":
        party = display_msisdn(party)
        phone_type = acct.get("phone_type")
        if phone_type == "pochi":
            return f"M-PESA Pochi la Biashara {party}" if lang != "sw" else f"Pochi la Biashara ya M-PESA {party}"
        if phone_type == "send_money":
            return f"M-PESA Send Money {party}" if lang != "sw" else f"Tuma Pesa ya M-PESA {party}"
        if phone_type == "airtel_money":
            return f"Airtel Money {party}"
        return f"mobile money {party}"
    if acct.get("manual") and kind == "till":
        return f"M-PESA manual till {party}" if lang != "sw" else f"till ya M-PESA {party}"
    if kind in ("till", "paybill"):
        return f"M-PESA {kind} {party}" if lang != "sw" else f"{kind} ya M-PESA {party}"
    return party


def manual_instructions(lang: str, amount: int, acct: dict, salon: str) -> str:
    """Build code-written instructions for the salon's selected manual payment route."""
    if acct.get("kind") == "till":
        key = "manual_pay_till"
        destination = str(acct.get("party_b") or "")
    else:
        key = {"pochi": "manual_pay_pochi", "send_money": "manual_pay_send_money",
               "airtel_money": "manual_pay_airtel"}.get(acct.get("phone_type"), "manual_pay_send_money")
        destination = display_msisdn(str(acct.get("party_b") or ""))
    return t(lang, key, amount=amount, destination=destination, salon=salon)


def deposit_note(lang: str, amount: int, acct: dict) -> str:
    return t(lang, "deposit_note", amount=amount, destination=destination_label(acct, lang))


def deposit_amount(salon: dict, service: dict) -> int:
    """The deposit is never more than the service costs."""
    return max(0, min(int(salon["deposit_kes"]), int(service["price_kes"])))


async def request_deposit(ctx, salon: dict, booking_id: str, phone: str, amount: int, message_id: str | None = None) -> str:
    """Send an M-Pesa prompt for a held booking. Returns 'sent', 'wait' (one is already on its way) or 'failed'."""
    acct = await account(ctx.pool, salon["id"])
    if acct is None or ctx.daraja is None or not ctx.daraja.configured:
        log.error("deposit requested but M-Pesa is not configured for salon %s", salon["id"])
        return "failed"
    if message_id:
        prior = await ctx.pool.fetchrow("SELECT id::text, checkout_request_id FROM payments WHERE source_message_id=$1 AND booking_id=$2::uuid ORDER BY created_at LIMIT 1", message_id, booking_id)
        if prior is not None:
            if prior["checkout_request_id"]:
                return "sent"
            await ctx.pool.execute("UPDATE payments SET status='failed', result_desc='interrupted before Daraja answered', settled_at=now() WHERE id=$1::uuid AND status='pending'", prior["id"])
            return "failed"
    recent = await ctx.pool.fetchval(
        "SELECT 1 FROM payments WHERE booking_id=$1::uuid AND status='pending' AND phone=$2 "
        "AND created_at > now() - make_interval(secs => $3)", booking_id, phone, RETRY_AFTER_S)
    if recent:
        return "wait"
    pay_id = await ctx.pool.fetchval(
        "INSERT INTO payments (booking_id, salon_id, phone, amount_kes, provider, source_message_id) VALUES ($1::uuid,$2::uuid,$3,$4,'mpesa',$5) RETURNING id::text",
        booking_id, salon["id"], phone, amount, message_id)
    try:
        acc = await ctx.daraja.stk_push(acct, amount=amount, phone=phone, reference=acct["account_ref"], description="Deposit")
    except MpesaError as e:
        log.error("STK push failed for %s: %s", mask(phone), e)
        await ctx.pool.execute("UPDATE payments SET status='failed', result_desc=$2, settled_at=now() WHERE id=$1::uuid", pay_id, str(e)[:300])
        return "failed"
    await ctx.pool.execute("UPDATE payments SET checkout_request_id=$2, merchant_request_id=$3 WHERE id=$1::uuid",
                           pay_id, acc.checkout_request_id, acc.merchant_request_id)
    await ctx.pool.execute(f"UPDATE bookings SET hold_expires_at = GREATEST(hold_expires_at, now() + interval '{EXTEND_HOLD}') "
                           "WHERE id=$1::uuid AND status='held'", booking_id)
    log.info("STK prompt sent to %s for KES %s", mask(phone), amount)
    return "sent"


@dataclass(slots=True)
class Settlement:
    outcome: str
    why: str
    payment_id: str
    booking_id: str
    amount: int
    phone: str
    receipt: str | None
    code: int | None
    salon_id: str
    salon_name: str
    pid: str
    owner_wa_id: str | None
    timezone: str
    client_wa_id: str
    client_name: str | None
    lang: str
    service: str
    stylist: str
    starts_at: datetime
    wa_token: str | None = None
    settled_via: str = "callback"
    provider: str = "mpesa"


_DETAIL = (
    "SELECT s.id::text AS salon_id, s.name AS salon_name, s.wa_phone_number_id AS pid, s.wa_access_token AS wa_token, s.owner_wa_id, s.timezone, "
    "c.wa_id AS client_wa_id, c.name AS client_name, c.lang, sv.name AS service, st.name AS stylist, b.starts_at "
    "FROM bookings b JOIN salons s ON s.id=b.salon_id JOIN clients c ON c.id=b.client_id "
    "JOIN services sv ON sv.id=b.service_id JOIN staff st ON st.id=b.staff_id WHERE b.id=$1::uuid")


_REVIEW_REASONS = {"slot_taken": "slot_unavailable", "amount": "amount_mismatch", "overpaid": "amount_mismatch",
                    "cancelled": "payment_unresolved", "duplicate": "payment_unresolved"}


async def park_for_cancellation(pool, booking_id: str, reason: str = "cancelled") -> int:
    """Park every paid prompt for a cancelled booking as needs_refund so it shows up in the refund queue.
    The cancelling message already told the customer and the owner, so parked rows are marked announced."""
    async with pool.acquire() as con:
        async with con.transaction():
            rows = await con.fetch(
                "UPDATE payments SET status='needs_refund', refund_reason=$2, announced_at=now(), updated_at=now() "
                "WHERE booking_id=$1::uuid AND status='paid' RETURNING id, amount_kes, mpesa_receipt", booking_id, reason)
            for r in rows:
                await con.execute(
                    "INSERT INTO payment_reviews (payment_id, reason, status, expected_amount, received_amount, mpesa_receipt) "
                    "VALUES ($1,'payment_unresolved','refund_due',$2,$2,$3) ON CONFLICT (payment_id) DO NOTHING",
                    r["id"], r["amount_kes"], r["mpesa_receipt"])
            return len(rows)


async def settle(pool, o: Outcome, via: str) -> tuple[str, Settlement | None]:
    """Apply one outcome. Returns ('settled', Settlement) the first time, ('seen', None) if it was already applied,
    ('unknown', None) if we have no such payment. Money rules:
      - a success always wins, even over a prompt we had written off as failed;
      - a payment for a booking that cannot be honoured is parked as needs_refund, never dropped;
      - two payments for one booking: the second is needs_refund."""
    async with pool.acquire() as con:
        async with con.transaction():
            p = await con.fetchrow("SELECT * FROM payments WHERE checkout_request_id=$1 FOR UPDATE", o.checkout_request_id)
            if p is None:
                return "unknown", None
            if p["status"] in ("paid", "needs_refund") or (not o.ok and p["status"] != "pending"):
                if not o.ok and p["status"] == "failed" and (o.code != p["result_code"] or (o.desc or "")[:300] != (p["result_desc"] or "")):
                    await con.execute("UPDATE payments SET result_code=$2, result_desc=$3 WHERE id=$1", p["id"], o.code, o.desc[:300])
                return "seen", None
            outcome, why = "failed", ""
            if o.ok:
                bid = str(p["booking_id"])
                booking = await con.fetchrow("SELECT status FROM bookings WHERE id=$1::uuid FOR UPDATE", bid)
                other = await con.fetchval("SELECT 1 FROM payments WHERE booking_id=$1::uuid AND status='paid' AND id<>$2", bid, p["id"])
                if other:
                    outcome, why = "needs_refund", "duplicate"
                elif o.amount is not None and o.amount < p["amount_kes"]:
                    outcome, why = "needs_refund", "amount"
                elif o.amount is not None and o.amount > p["amount_kes"]:
                    outcome, why = "needs_refund", "overpaid"
                elif booking is None or booking["status"] == "cancelled":
                    outcome, why = "needs_refund", "cancelled"
                elif booking["status"] == "confirmed":
                    outcome = "paid"
                else:
                    try:
                        async with con.transaction():
                            await con.execute("UPDATE bookings SET status='confirmed', hold_expires_at=NULL WHERE id=$1::uuid", bid)
                        outcome = "paid"
                    except asyncpg.ExclusionViolationError:
                        outcome, why = "needs_refund", "slot_taken"
            status = "paid" if outcome == "paid" else outcome
            try:
                async with con.transaction():
                    await con.execute(
                        "UPDATE payments SET status=$2, result_code=$3, result_desc=$4, mpesa_receipt=COALESCE($5, mpesa_receipt), "
                        "settled_via=$6, settled_at=now(), refund_reason=$7, announced_at=NULL WHERE id=$1", p["id"], status, o.code, o.desc[:300], o.receipt, via, why or None)
            except asyncpg.UniqueViolationError:
                log.warning("concurrent duplicate payment for booking %s; parking as needs_refund", p["booking_id"])
                outcome, why = "needs_refund", "duplicate"
                status = "needs_refund"
                await con.execute(
                    "UPDATE payments SET status=$2, result_code=$3, result_desc=$4, mpesa_receipt=COALESCE($5, mpesa_receipt), "
                    "settled_via=$6, settled_at=now(), refund_reason=$7, announced_at=NULL WHERE id=$1", p["id"], status, o.code, o.desc[:300], o.receipt, via, why or None)
            if outcome == "needs_refund":
                await con.execute(
                    "INSERT INTO payment_reviews (payment_id, reason, status, expected_amount, received_amount, mpesa_receipt) "
                    "VALUES ($1,$2,'refund_due',$3,$4,$5) ON CONFLICT (payment_id) DO NOTHING",
                    p["id"], _REVIEW_REASONS.get(why, "payment_unresolved"), p["amount_kes"], o.amount, o.receipt or p["mpesa_receipt"])
            d = await con.fetchrow(_DETAIL, str(p["booking_id"]))
            if d is None:
                log.error("settled payment %s but its booking details are gone; parked without announcement", p["id"])
                return "settled", None
    return "settled", Settlement(outcome, why, str(p["id"]), str(p["booking_id"]), p["amount_kes"], p["phone"], o.receipt or p["mpesa_receipt"],
                                 o.code, provider=p["provider"] or "mpesa", **dict(d))


def _when(s: Settlement) -> str:
    return s.starts_at.astimezone(ZoneInfo(s.timezone)).strftime("%a %d %b, %H:%M")


async def _send(ctx, s: Settlement, to: str, body: str) -> None:
    await outbound.send_text(ctx, {"id": s.salon_id, "wa_phone_number_id": s.pid, "wa_access_token": s.wa_token}, to, body)


async def announce(ctx, s: Settlement) -> None:
    """Tell the customer and the owner, once. Sends are remembered under the payment's id, so announcing again after a failure part way
    repeats nothing that already went out; the payment is marked announced only when all of it has."""
    async with outbound.message_scope(f"payment:{s.payment_id}"):
        await _announce(ctx, s)
    await ctx.pool.execute("UPDATE payments SET announced_at=now() WHERE id=$1::uuid", s.payment_id)


async def announce_missed(ctx, checkout_request_id: str) -> bool:
    """A payment that is settled but was never (fully) announced: the announcing failed last time. Returns True if it announced."""
    p = await ctx.pool.fetchrow("SELECT * FROM payments WHERE checkout_request_id=$1 AND status IN ('paid','needs_refund','failed') AND announced_at IS NULL", checkout_request_id)
    if p is None:
        return False
    d = await ctx.pool.fetchrow(_DETAIL, str(p["booking_id"]))
    outcome = "paid" if p["status"] == "paid" else p["status"]
    s = Settlement(outcome, p["refund_reason"] or "", str(p["id"]), str(p["booking_id"]), p["amount_kes"], p["phone"], p["mpesa_receipt"], p["result_code"],
                   provider=p["provider"] or "mpesa", **dict(d))
    log.warning("payment %s was settled but never announced: announcing it now", s.payment_id)
    await announce(ctx, s)
    return True


async def _announce(ctx, s: Settlement) -> None:
    lang, when = s.lang, _when(s)
    provider_label = "Airtel Money" if s.provider == "airtel_money" else "M-Pesa"
    ref = f" ({provider_label} reference {s.receipt})" if s.receipt and s.provider == "airtel_money" else (f" (M-Pesa {s.receipt})" if s.receipt else "")
    if s.outcome == "paid":
        try:
            from .reminders import schedule_reminders
            await schedule_reminders(ctx.pool, s.booking_id, template_24h=ctx.settings.reminder_template_24h, template_2h=ctx.settings.reminder_template_2h)
        except Exception:
            log.exception("could not schedule reminders for booking %s", s.booking_id)
        bookings_confirmed.inc()
        try:
            payments_total.labels("paid", getattr(s, "settled_via", "callback") or "callback").inc()
        except Exception:
            pass
        await _send(ctx, s, s.client_wa_id, t(lang, "paid", provider=provider_label, amount=s.amount, ref=ref, service=s.service, when=when, stylist=s.stylist, salon=s.salon_name))
        if s.owner_wa_id:
            await _send(ctx, s, s.owner_wa_id, t("en", "owner_booked", client=s.client_name or mask(s.client_wa_id), service=s.service,
                                                    when=when, stylist=s.stylist, amount=s.amount, provider=provider_label, ref=ref))
    elif s.outcome == "needs_refund":
        log.error("REFUND NEEDED KES %s from %s receipt=%s why=%s", s.amount, mask(s.phone), s.receipt, s.why)
        await _send(ctx, s, s.client_wa_id, t(lang, "refund_notice", amount=s.amount, ref=ref, why=t(lang, f"refund_{s.why}"), salon=s.salon_name))
        if s.owner_wa_id:
            await _send(ctx, s, s.owner_wa_id, t("en", "owner_refund", amount=s.amount, phone=s.phone, ref=ref, when=when, why=t("en", f"refund_{s.why}")))
    else:
        why = t(lang, f"why_{s.code}") if has(lang, f"why_{s.code}") else t(lang, "why_other")
        await _send(ctx, s, s.client_wa_id, t(lang, "pay_failed", why=why))
        return
    await ctx.pool.execute(
        "UPDATE conversations SET state='{}', updated_at=now() WHERE salon_id=$1::uuid AND wa_id=$2 AND state->>'booking_id'=$3",
        s.salon_id, s.client_wa_id, s.booking_id)


async def _account_for_checkout(pool, checkout_id: str):
    row = await pool.fetchrow("SELECT p.salon_id FROM payments p WHERE p.checkout_request_id=$1", checkout_id)
    if row is None:
        return None
    return await pool.fetchrow("SELECT kind, shortcode, party_b, account_ref FROM salon_payment WHERE salon_id=$1::uuid", row["salon_id"])


async def settle_with_verification(ctx, cb) -> str | None:
    if getattr(getattr(ctx, "settings", None), "mpesa_verify_callbacks", False) and cb.raw is not None and "Body" in cb.raw:
        acct = await _account_for_checkout(ctx.pool, cb.checkout_request_id)
        if acct is not None:
            verified = await ctx.daraja.stk_query(dict(acct), cb.checkout_request_id)
            if verified is None:
                log.warning("callback for %s not verifiable yet; left pending for the sweeper", cb.checkout_request_id)
                return "unverified"
            if verified.code == cb.code:
                cb = dataclasses.replace(verified, raw=cb.raw, receipt=cb.receipt, amount=cb.amount, phone=cb.phone)
            elif cb.code == 0 and cb.amount is not None:
                log.error("callback says %s but Daraja says %s for %s; trusting the callback's success", cb.code, verified.code, cb.checkout_request_id)
            else:
                log.error("callback says %s but Daraja says %s for %s; trusting Daraja", cb.code, verified.code, cb.checkout_request_id)
                cb = dataclasses.replace(verified, raw=cb.raw, phone=cb.phone)
    status, settlement = await settle(ctx.pool, cb, "callback")
    if status == "settled" and settlement:
        await announce(ctx, settlement)
    elif status == "seen":
        await announce_missed(ctx, cb.checkout_request_id)
    return status


async def _with_callback_details(pool, out: Outcome) -> Outcome:
    """A status query says how a prompt ended but carries no receipt number and no amount; the callback is the only place that has them.
    If the callback was received (stored verbatim before it was queued) and agrees with Daraja about the result, take both from it."""
    body = await pool.fetchval("SELECT body FROM mpesa_callbacks WHERE checkout_request_id=$1 ORDER BY id DESC LIMIT 1", out.checkout_request_id)
    if isinstance(body, str):
        body = json.loads(body)
    cb = mpesa.parse_callback(body) if isinstance(body, dict) else None
    if cb is None or cb.code != out.code:
        return out
    return dataclasses.replace(out, receipt=cb.receipt or out.receipt, amount=cb.amount if cb.amount is not None else out.amount, phone=cb.phone or out.phone)


async def reconcile(ctx) -> int:
    """Ask Daraja about prompts that never called back (or whose callback could not be verified). Returns how many it settled."""
    if ctx.daraja is None or not ctx.daraja.configured:
        return 0
    rows = await ctx.pool.fetch(
        "SELECT p.checkout_request_id, p.created_at < now() - make_interval(mins => $1) AS stale, sp.kind, sp.shortcode, sp.party_b, sp.account_ref, "
        "EXISTS (SELECT 1 FROM mpesa_callbacks cb WHERE cb.checkout_request_id=p.checkout_request_id AND cb.body #>> '{Body,stkCallback,ResultCode}' = '0') AS success_on_file "
        "FROM payments p JOIN salon_payment sp ON sp.salon_id=p.salon_id "
        "WHERE p.status='pending' AND p.checkout_request_id IS NOT NULL AND p.created_at < now() - interval '40 seconds' "
        "AND (p.created_at > now() - interval '2 days' OR EXISTS (SELECT 1 FROM mpesa_callbacks cb WHERE cb.checkout_request_id=p.checkout_request_id "
        "AND cb.body #>> '{Body,stkCallback,ResultCode}' = '0')) ORDER BY p.created_at LIMIT 50", STALE_AFTER_MIN)
    settled = 0
    for r in rows:
        out = await ctx.daraja.stk_query(dict(r), r["checkout_request_id"])
        if out is None and r["stale"] and not r["success_on_file"]:
            out = Outcome(r["checkout_request_id"], mpesa.CODE_TIMEOUT, "no answer from M-Pesa")
        if out is None:
            continue
        out = await _with_callback_details(ctx.pool, out)
        status, s = await settle(ctx.pool, out, "query")
        if s:
            settled += 1
            await announce(ctx, s)
        elif status == "seen":
            await announce_missed(ctx, out.checkout_request_id)
    for r in await ctx.pool.fetch("SELECT checkout_request_id FROM payments WHERE status IN ('paid','needs_refund','failed') AND announced_at IS NULL "
                                  "AND checkout_request_id IS NOT NULL AND settled_at < now() - interval '1 minute' LIMIT 20"):
        await announce_missed(ctx, r["checkout_request_id"])
    await ctx.pool.execute("UPDATE payments SET status='failed', result_desc='never sent', settled_at=now() "
                           "WHERE status='pending' AND checkout_request_id IS NULL AND method='stk' AND created_at < now() - interval '2 minutes'")
    return settled


async def record_manual_claim(pool, booking_id: str, salon_id: str, phone: str, amount: int, code: str, ref: str,
                              provider: str = "mpesa") -> str:
    """File a manual transfer claim. Only the owner's tap confirms it; customer text is never proof."""
    return await pool.fetchval(
        "INSERT INTO payments (booking_id, salon_id, phone, amount_kes, method, provider, mpesa_receipt, claim_ref) "
        "VALUES ($1::uuid,$2::uuid,$3,$4,'manual',$5,$6,$7) RETURNING id::text",
        booking_id, salon_id, phone, amount, provider, code, ref[:500])


async def confirm_manual_claim(pool, pay_id: str) -> tuple[str, str | None]:
    """The owner's Yes tap. Idempotent: ('confirmed', booking_id) once, ('already', booking_id) after,
    ('stale', None) when the claim or the hold is no longer open."""
    async with pool.acquire() as con:
        async with con.transaction():
            p = await con.fetchrow(
                "SELECT p.booking_id, p.status, p.method, p.amount_kes, p.mpesa_receipt, "
                "r.status AS review_status, r.expected_amount, r.received_amount "
                "FROM payments p LEFT JOIN payment_reviews r ON r.payment_id=p.id "
                "WHERE p.id=$1::uuid FOR UPDATE OF p", pay_id)
            if p is None:
                return "stale", None
            bid = str(p["booking_id"])
            if p["status"] == "paid":
                return "already", bid
            if (p["status"] not in ("pending", "review") or p["method"] != "manual"
                    or (p["status"] == "review" and p["review_status"] != "pending_review")):
                return "stale", bid
            received_amount = p["received_amount"] or p["amount_kes"]
            b = await con.fetchrow(
                "SELECT status, salon_id::text AS salon_id, staff_id::text AS staff_id, starts_at, ends_at "
                "FROM bookings WHERE id=$1::uuid FOR UPDATE", bid)
            if b is None:
                await con.execute("UPDATE payments SET status='failed', result_desc='booking closed before confirm', settled_at=now() WHERE id=$1::uuid", pay_id)
                return "stale", bid
            if b["status"] not in ("held", "expired"):
                reason = "owner verified manual payment after the booking was closed"
                await con.execute(
                    "UPDATE payments SET status='needs_refund', amount_kes=$3, settled_via='manual', settled_at=now(), "
                    "refund_reason=$2, result_desc='owner verified payment; booking is closed' WHERE id=$1::uuid",
                    pay_id, reason, received_amount)
                await con.execute(
                    "INSERT INTO payment_reviews (payment_id, reason, status, expected_amount, received_amount, mpesa_receipt) "
                    "VALUES ($1,'payment_unresolved','refund_due',$2,$3,$4) "
                    "ON CONFLICT (payment_id) DO UPDATE SET status='refund_due', "
                    "received_amount=EXCLUDED.received_amount, mpesa_receipt=EXCLUDED.mpesa_receipt, updated_at=now()",
                    pay_id, p["expected_amount"] or p["amount_kes"], received_amount, p["mpesa_receipt"])
                return "unavailable", bid
            if b["status"] == "expired":
                await con.execute("SELECT pg_advisory_xact_lock(hashtext($1))", f"hold:{b['staff_id']}")
                blocked = await con.fetchval(
                    "SELECT 1 FROM bookings WHERE staff_id=$1::uuid AND id<>$2::uuid "
                    "AND status IN ('held','confirmed') AND (status='confirmed' OR hold_expires_at>now()) "
                    "AND starts_at<$4 AND ends_at>$3 LIMIT 1",
                    b["staff_id"], bid, b["starts_at"], b["ends_at"])
                away = await con.fetchval(
                    "SELECT 1 FROM time_off WHERE salon_id=$1::uuid AND (staff_id IS NULL OR staff_id=$2::uuid) "
                    "AND starts_at<$4 AND ends_at>$3 LIMIT 1",
                    b["salon_id"], b["staff_id"], b["starts_at"], b["ends_at"])
                if b["starts_at"] <= datetime.now(b["starts_at"].tzinfo) or blocked or away:
                    await con.execute(
                        "UPDATE payments SET status='needs_refund', amount_kes=$2, settled_via='manual', settled_at=now(), "
                        "refund_reason='owner verified payment after the slot became unavailable', "
                        "result_desc='owner verified payment; slot unavailable' WHERE id=$1::uuid", pay_id, received_amount)
                    await con.execute(
                        "INSERT INTO payment_reviews (payment_id, reason, status, expected_amount, received_amount, mpesa_receipt) "
                        "VALUES ($1,'slot_unavailable','refund_due',$2,$3,$4) "
                        "ON CONFLICT (payment_id) DO UPDATE SET reason='slot_unavailable', status='refund_due', "
                        "received_amount=EXCLUDED.received_amount, mpesa_receipt=EXCLUDED.mpesa_receipt, updated_at=now()",
                        pay_id, p["expected_amount"] or p["amount_kes"], received_amount, p["mpesa_receipt"])
                    return "unavailable", bid
                await con.execute("UPDATE bookings SET status='held', hold_expires_at=now() + interval '1 minute' WHERE id=$1::uuid", bid)
            await con.execute("UPDATE payments SET status='paid', amount_kes=$2, settled_via='manual', settled_at=now(), announced_at=NULL, "
                              "result_desc=NULL, refund_reason=NULL WHERE id=$1::uuid", pay_id, received_amount)
            await con.execute("UPDATE bookings SET status='confirmed', hold_expires_at=NULL WHERE id=$1::uuid", bid)
            await con.execute("UPDATE payment_reviews SET status='closed_no_refund', resolution_note='owner verified transfer', "
                              "resolved_at=now(), updated_at=now() WHERE payment_id=$1::uuid AND status='pending_review'", pay_id)
            return "confirmed", bid


async def decline_manual_claim(pool, pay_id: str) -> tuple[str, str | None]:
    """The owner's No tap. The booking stays held so the customer can send a better claim."""
    async with pool.acquire() as con:
        async with con.transaction():
            p = await con.fetchrow("SELECT booking_id, status, method FROM payments WHERE id=$1::uuid FOR UPDATE", pay_id)
            if p is None:
                return "stale", None
            bid = str(p["booking_id"])
            if p["status"] == "paid":
                return "already", bid
            if p["status"] not in ("pending", "review") or p["method"] != "manual":
                return "stale", bid
            await con.execute("UPDATE payments SET status='failed', result_desc='declined by salon', settled_at=now() WHERE id=$1::uuid", pay_id)
            await con.execute("UPDATE payment_reviews SET status='closed_no_refund', resolution_note='owner could not verify transfer', "
                              "resolved_at=now(), updated_at=now() WHERE payment_id=$1::uuid AND status='pending_review'", pay_id)
            return "declined", bid


async def sweep(ctx) -> dict:
    from .owner import daily_summaries
    from .reminders import ask_about_visits
    from .booking_flow import expire_owner_availability_checks
    owner_checks_expired = await expire_owner_availability_checks(ctx)
    expired = await expire_holds(ctx.pool)
    await notify_expired_manual_claims(ctx)
    return {"owner_checks_expired": owner_checks_expired, "expired": expired, "reconciled": await reconcile(ctx), "summaries": await daily_summaries(ctx),
            "visit_checks": await ask_about_visits(ctx.pool, ctx)}


async def notify_expired_manual_claims(ctx) -> int:
    """Tell customers their 30-minute hold elapsed while retaining the owner's open claim."""
    rows = await ctx.pool.fetch(
        "SELECT p.id::text AS payment_id, "
        "c.wa_id, c.lang, s.id::text AS salon_id, s.name AS salon_name, s.wa_phone_number_id, s.wa_access_token "
        "FROM payments p JOIN bookings b ON b.id=p.booking_id JOIN clients c ON c.id=b.client_id "
        "JOIN salons s ON s.id=p.salon_id "
        "WHERE p.method='manual' AND p.status IN ('pending','review') AND b.status='expired' "
        "AND p.customer_expired_notice_at IS NULL ORDER BY p.created_at")
    sent = 0
    for row in rows:
        salon = {"id": row["salon_id"], "name": row["salon_name"], "wa_phone_number_id": row["wa_phone_number_id"],
                 "wa_access_token": row["wa_access_token"]}
        ok = await outbound.send_text(
            ctx, salon, row["wa_id"],
            t(row["lang"] or "en", "till_hold_expired", salon=row["salon_name"]))
        if ok:
            await ctx.pool.execute("UPDATE payments SET customer_expired_notice_at=now() WHERE id=$1::uuid AND customer_expired_notice_at IS NULL",
                                   row["payment_id"])
            sent += 1
    return sent


async def run_sweeper(ctx, every: float = 30.0) -> None:
    while True:
        try:
            done = await sweep(ctx)
            if any(done.values()):
                log.info("sweep %s", done)
        except asyncio.CancelledError:
            raise
        except Exception:
            log.exception("sweep failed")
        await asyncio.sleep(every)
