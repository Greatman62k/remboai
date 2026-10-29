"""Deposits. Code decides every money state; the model never sees a payment.

Lifecycle of one prompt:  request_deposit -> (callback | status query) -> settle -> announce.
settle() is idempotent: Safaricom may deliver a callback twice, and the sweeper may race it."""
from __future__ import annotations

import asyncio
import json
import logging
from dataclasses import dataclass
from datetime import datetime
from zoneinfo import ZoneInfo

import asyncpg

from . import mpesa
from .bookings import expire_holds
from .messages import has, t
from .mpesa import MpesaError, Outcome
from .whatsapp import mask

log = logging.getLogger("rembo.pay")

RETRY_AFTER_S = 45          # do not stack a second prompt on a phone that is still showing the first
EXTEND_HOLD = "4 minutes"   # keep the slot while the customer is entering their PIN
STALE_AFTER_MIN = 6         # a prompt with no answer after this long is written off (a late success still wins)


async def account(pool, salon_id: str) -> dict | None:
    row = await pool.fetchrow("SELECT kind, shortcode, party_b, account_ref FROM salon_payment WHERE salon_id=$1::uuid", salon_id)
    return dict(row) if row else None


def deposit_amount(salon: dict, service: dict) -> int:
    """The deposit is never more than the service costs."""
    return max(0, min(int(salon["deposit_kes"]), int(service["price_kes"])))


async def request_deposit(ctx, salon: dict, booking_id: str, phone: str, amount: int) -> str:
    """Send an M-Pesa prompt for a held booking. Returns 'sent', 'wait' (one is already on its way) or 'failed'."""
    acct = await account(ctx.pool, salon["id"])
    if acct is None or ctx.daraja is None or not ctx.daraja.configured:
        log.error("deposit requested but M-Pesa is not configured for salon %s", salon["id"])
        return "failed"
    recent = await ctx.pool.fetchval(
        "SELECT 1 FROM payments WHERE booking_id=$1::uuid AND status='pending' AND phone=$2 "
        "AND created_at > now() - make_interval(secs => $3)", booking_id, phone, RETRY_AFTER_S)
    if recent:
        return "wait"
    # The row exists before Daraja is called, so a callback can never arrive for something we have no record of.
    pay_id = await ctx.pool.fetchval(
        "INSERT INTO payments (booking_id, salon_id, phone, amount_kes) VALUES ($1::uuid,$2::uuid,$3,$4) RETURNING id::text",
        booking_id, salon["id"], phone, amount)
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
    outcome: str                 # paid | failed | needs_refund
    why: str                     # for needs_refund: slot_taken | cancelled | duplicate | amount
    payment_id: str
    booking_id: str
    amount: int
    phone: str
    receipt: str | None
    code: int | None
    # who and what, for the messages
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


_DETAIL = (
    "SELECT s.id::text AS salon_id, s.name AS salon_name, s.wa_phone_number_id AS pid, s.owner_wa_id, s.timezone, "
    "c.wa_id AS client_wa_id, c.name AS client_name, c.lang, sv.name AS service, st.name AS stylist, b.starts_at "
    "FROM bookings b JOIN salons s ON s.id=b.salon_id JOIN clients c ON c.id=b.client_id "
    "JOIN services sv ON sv.id=b.service_id JOIN staff st ON st.id=b.staff_id WHERE b.id=$1::uuid")


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
                elif booking["status"] == "cancelled":
                    outcome, why = "needs_refund", "cancelled"
                elif booking["status"] == "confirmed":
                    outcome = "paid"
                else:  # held, or expired while the customer was paying: take the slot back if nobody else has it
                    try:
                        async with con.transaction():
                            await con.execute("UPDATE bookings SET status='confirmed', hold_expires_at=NULL WHERE id=$1::uuid", bid)
                        outcome = "paid"
                    except asyncpg.ExclusionViolationError:
                        outcome, why = "needs_refund", "slot_taken"
            status = "paid" if outcome == "paid" else outcome
            await con.execute(
                "UPDATE payments SET status=$2, result_code=$3, result_desc=$4, mpesa_receipt=COALESCE($5, mpesa_receipt), "
                "settled_via=$6, settled_at=now(), refund_reason=$7 WHERE id=$1", p["id"], status, o.code, o.desc[:300], o.receipt, via, why or None)
            d = await con.fetchrow(_DETAIL, str(p["booking_id"]))
    return "settled", Settlement(outcome, why, str(p["id"]), str(p["booking_id"]), p["amount_kes"], p["phone"], o.receipt or p["mpesa_receipt"],
                                 o.code, **dict(d))


def _when(s: Settlement) -> str:
    return s.starts_at.astimezone(ZoneInfo(s.timezone)).strftime("%a %d %b, %H:%M")


async def _send(ctx, pid: str, to: str, body: str) -> None:
    await ctx.wa.send_text(pid, ctx.settings.wa_access_token, to, body)


async def announce(ctx, s: Settlement) -> None:
    """Tell the customer, tell the owner, and clear the customer's conversation so they can book again."""
    lang, when = s.lang, _when(s)
    ref = f" (M-Pesa {s.receipt})" if s.receipt else ""
    if s.outcome == "paid":
        await _send(ctx, s.pid, s.client_wa_id, t(lang, "paid", amount=s.amount, ref=ref, service=s.service, when=when, stylist=s.stylist, salon=s.salon_name))
        if s.owner_wa_id:
            await _send(ctx, s.pid, s.owner_wa_id, t("en", "owner_booked", client=s.client_name or mask(s.client_wa_id), service=s.service,
                                                    when=when, stylist=s.stylist, amount=s.amount, ref=ref))
    elif s.outcome == "needs_refund":
        log.error("REFUND NEEDED KES %s from %s receipt=%s why=%s", s.amount, mask(s.phone), s.receipt, s.why)
        await _send(ctx, s.pid, s.client_wa_id, t(lang, "refund_notice", amount=s.amount, ref=ref, why=t(lang, f"refund_{s.why}"), salon=s.salon_name))
        if s.owner_wa_id:
            await _send(ctx, s.pid, s.owner_wa_id, t("en", "owner_refund", amount=s.amount, phone=s.phone, ref=ref, when=when, why=t("en", f"refund_{s.why}")))
    else:
        why = t(lang, f"why_{s.code}") if has(lang, f"why_{s.code}") else t(lang, "why_other")
        await _send(ctx, s.pid, s.client_wa_id, t(lang, "pay_failed", why=why))
        return  # the slot is still held: the customer keeps their conversation state to retry
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
                log.warning("callback for %s not verifiable yet; left pending for sweeper", cb.checkout_request_id)
                return "unknown"
            if verified.code != cb.code:
                log.error("callback says %s but Daraja says %s for %s; trusting Daraja", cb.code, verified.code, cb.checkout_request_id)
            orig = cb
            verified_raw = __import__("dataclasses").replace(verified, raw=orig.raw)
            if orig.code == verified.code:
                verified_raw = __import__("dataclasses").replace(verified_raw, receipt=orig.receipt, amount=orig.amount)
            verified_raw = __import__("dataclasses").replace(verified_raw, phone=orig.phone)
            cb = verified_raw
    status, settlement = await settle(ctx.pool, cb, "callback")
    if status == "settled" and settlement:
        await announce(ctx, settlement)
    return status


async def process_callback(ctx, payload: dict) -> None:
    out = mpesa.parse_callback(payload)
    await ctx.pool.execute("INSERT INTO mpesa_callbacks (checkout_request_id, body) VALUES ($1, $2::jsonb)", out.checkout_request_id if out else None, json.dumps(payload))
    if out is None:
        log.warning("M-Pesa callback with an unexpected shape")
        return
    status = await settle_with_verification(ctx, out)
    if status == "unknown":
        await asyncio.sleep(2)
        status = await settle_with_verification(ctx, out)
    if status == "unknown":
        log.error("M-Pesa callback for a payment we do not know: %s code=%s receipt=%s", out.checkout_request_id, out.code, out.receipt)


async def reconcile(ctx) -> int:
    """Ask Daraja about prompts that never called back. Returns how many it settled."""
    if ctx.daraja is None or not ctx.daraja.configured:
        return 0
    rows = await ctx.pool.fetch(
        "SELECT p.checkout_request_id, p.created_at < now() - make_interval(mins => $1) AS stale, sp.kind, sp.shortcode, sp.party_b, sp.account_ref "
        "FROM payments p JOIN salon_payment sp ON sp.salon_id=p.salon_id "
        "WHERE p.status='pending' AND p.checkout_request_id IS NOT NULL AND p.created_at < now() - interval '40 seconds' "
        "AND p.created_at > now() - interval '2 days' ORDER BY p.created_at LIMIT 50", STALE_AFTER_MIN)
    settled = 0
    for r in rows:
        out = await ctx.daraja.stk_query(dict(r), r["checkout_request_id"])
        if out is None and r["stale"]:
            out = Outcome(r["checkout_request_id"], mpesa.CODE_TIMEOUT, "no answer from M-Pesa")
        if out is None:
            continue
        status, s = await settle(ctx.pool, out, "query")
        if s:
            settled += 1
            await announce(ctx, s)
    # A row that never got a checkout id means we died between insert and Daraja's answer: nothing was sent.
    await ctx.pool.execute("UPDATE payments SET status='failed', result_desc='never sent', settled_at=now() "
                           "WHERE status='pending' AND checkout_request_id IS NULL AND created_at < now() - interval '2 minutes'")
    return settled


async def sweep(ctx) -> dict:
    from .owner import daily_summaries  # imported here so the payment code does not depend on the owner tools
    return {"expired": await expire_holds(ctx.pool), "reconciled": await reconcile(ctx), "summaries": await daily_summaries(ctx)}


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
