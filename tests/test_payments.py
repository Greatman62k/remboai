"""M-Pesa deposits end to end: real app, real Daraja client, real Postgres; Safaricom and Meta are faked at the HTTP layer."""
import asyncio
import os
from datetime import datetime, timedelta, timezone

import pytest

from app import payments
from app.bookings import expire_holds
from app.mpesa import Outcome, normalize_msisdn, parse_callback
from tests.harness import CB_SECRET, build, callback, client, open_day, payload, post

DSN = os.environ.get("TEST_DATABASE_URL")
needs_db = pytest.mark.skipif(not DSN, reason="needs TEST_DATABASE_URL")
A, B, OWNER = "254711000001", "254711000002", "254799000000"


def test_phone_numbers_are_normalised_and_other_text_is_not_a_number():
    for raw in ("0712 345 678", "+254712345678", "254712345678", "712345678", "0112-345-678"):
        assert normalize_msisdn(raw) in ("254712345678", "254112345678"), raw
    assert normalize_msisdn("fade kesho") is None and normalize_msisdn("12345") is None and normalize_msisdn("0612345678") is None


def test_manual_receipt_references_are_exact_and_cannot_be_mobile_numbers():
    assert payments.normalize_reference(" uk3l70e32k ") == "UK3L70E32K"
    assert payments.normalize_reference("254712345678") is None
    assert payments.normalize_reference("Transaction UK3L70E32K") is None
    assert payments.manual_provider({"kind": "phone", "phone_type": "airtel_money"}) == "airtel_money"
    assert payments.manual_provider({"kind": "phone", "phone_type": "pochi"}) == "mpesa"


def test_callback_parsing_success_and_failure():
    ok = parse_callback(callback("ws_CO_1", receipt="R1", amount=200))
    assert ok.ok and ok.receipt == "R1" and ok.amount == 200 and ok.phone == "254711000001"
    no = parse_callback(callback("ws_CO_2", code=1032))
    assert not no.ok and no.code == 1032 and no.receipt is None
    assert parse_callback({"nope": 1}) is None and parse_callback({"Body": {"stkCallback": {"ResultCode": "x"}}}) is None


async def to_pay(c, out, pool, who=A, mid="b"):
    """Customer asks for a fade and taps the first afternoon slot. Returns after the M-Pesa prompt went out."""
    out.script[f"fade {mid}"] = {"intent": "book", "service": "Fade", "date": open_day().isoformat(), "window": "afternoon"}
    await post(c, payload(who, f"fade {mid}", mid=f"{mid}1"))
    await post(c, payload(who, reply_id="slot:0", mid=f"{mid}2"))
    pending = await pool.fetchrow("SELECT id::text FROM bookings WHERE owner_check_status='pending' ORDER BY created_at DESC LIMIT 1")
    if pending:
        req = next(m for m in reversed(out.to(OWNER)) if m["type"] == "interactive" and
                   any(b["reply"]["id"].startswith("availability:yes:") for b in m["interactive"]["action"]["buttons"]))
        yes = next(b["reply"]["id"] for b in req["interactive"]["action"]["buttons"] if b["reply"]["id"].startswith("availability:yes:"))
        await post(c, payload(OWNER, reply_id=yes, mid=f"{mid}owner"))


async def deliver(c, body: dict, secret: str = CB_SECRET):
    return await c.post(f"/mpesa/callback/{secret}", json=body)


@needs_db
def test_tapping_a_slot_sends_an_stk_prompt_to_the_chat_number_for_the_till():
    async def run():
        pool, out, salon = await build(DSN)
        await pool.execute("UPDATE salons SET owner_wa_id=$1 WHERE id=$2::uuid", OWNER, salon)
        async with client() as c:
            await to_pay(c, out, pool)
        assert len(out.stk) == 1
        assert not any("availability:yes:" in str(m) for m in out.to(OWNER))
        stk = out.stk[0]
        assert stk["Amount"] == 200 and stk["PhoneNumber"] == A and stk["PartyA"] == A
        assert stk["BusinessShortCode"] == "174379" and stk["PartyB"] == "5012345" and stk["TransactionType"] == "CustomerBuyGoodsOnline"
        assert stk["CallBackURL"] == f"https://rembo.test/mpesa/callback/{CB_SECRET}"
        body = out.bodies(A)[-1]
        assert "Fade" in body and "200" in body and "254711000001" in body
        p = await pool.fetchrow("SELECT status, amount_kes, checkout_request_id FROM payments")
        assert (p["status"], p["amount_kes"], p["checkout_request_id"]) == ("pending", 200, "ws_CO_1")
        assert await pool.fetchval("SELECT status FROM bookings") == "held"
        await pool.close()
    asyncio.run(run())


@needs_db
def test_manual_booking_waits_five_minutes_for_owner_and_late_yes_reopens_the_customer_choice():
    async def run():
        from app.main import app
        from app.booking_flow import expire_owner_availability_checks
        pool, out, salon = await build(DSN)
        await manual_salon(pool, salon)
        out.script["fade waiting"] = {"intent": "book", "service": "Fade", "date": open_day().isoformat(), "window": "afternoon"}
        async with client() as c:
            await post(c, payload(A, "fade waiting", mid="w1"))
            await post(c, payload(A, reply_id="slot:0", mid="w2"))
            booking = await pool.fetchrow("SELECT id::text, status, owner_check_status, hold_expires_at FROM bookings")
            assert (booking["status"], booking["owner_check_status"]) == ("held", "pending")
            assert booking["hold_expires_at"] > datetime.now(timezone.utc) + timedelta(minutes=4)
            assert await pool.fetchval("SELECT count(*) FROM payments") == 0
            assert "up to five minutes" in out.bodies(A)[-1]
            request = next(m for m in reversed(out.to(OWNER)) if m["type"] == "interactive" and
                           any(b["reply"]["id"].startswith("availability:yes:") for b in m["interactive"]["action"]["buttons"]))
            late_yes = next(b["reply"]["id"] for b in request["interactive"]["action"]["buttons"]
                            if b["reply"]["id"].startswith("availability:yes:"))
            await pool.execute("UPDATE bookings SET owner_check_expires_at=now()-interval '1 second', "
                               "hold_expires_at=now()-interval '1 second' WHERE id=$1::uuid", booking["id"])
            assert await expire_owner_availability_checks(app.state.ctx) == 1
            assert await pool.fetchval("SELECT status FROM bookings") == "cancelled"
            assert "released the slot" in out.bodies(A)[-1]
            await post(c, payload(OWNER, reply_id=late_yes, mid="w3"))
            assert await pool.fetchval("SELECT owner_check_status FROM bookings") == "late_approved"
            resume = next(b["reply"]["id"] for m in reversed(out.to(A)) if m["type"] == "interactive"
                          for b in m["interactive"]["action"]["buttons"] if b["reply"]["id"].startswith("ownerresume:yes:"))
            assert "Fade" in out.bodies(A)[-1] and "five minutes" in out.bodies(A)[-1]
            await post(c, payload(A, reply_id=resume, mid="w4"))
        bookings = await pool.fetch("SELECT status, owner_check_status FROM bookings ORDER BY created_at")
        assert [(r["status"], r["owner_check_status"]) for r in bookings] == [("cancelled", "customer_accepted"), ("held", None)]
        assert "seven-digit till number: 5012345" in out.bodies(A)[-1]
        assert await pool.fetchval("SELECT count(*) FROM payments") == 0
        await pool.close()
    asyncio.run(run())


@needs_db
def test_a_late_owner_offer_expires_after_five_minutes_and_is_discarded():
    async def run():
        from app.main import app
        from app.booking_flow import expire_owner_availability_checks
        pool, out, salon = await build(DSN)
        await manual_salon(pool, salon)
        out.script["fade waiting"] = {"intent": "book", "service": "Fade", "date": open_day().isoformat(), "window": "afternoon"}
        async with client() as c:
            await post(c, payload(A, "fade waiting", mid="e1"))
            await post(c, payload(A, reply_id="slot:0", mid="e2"))
            request = next(m for m in reversed(out.to(OWNER)) if m["type"] == "interactive" and
                           any(b["reply"]["id"].startswith("availability:yes:") for b in m["interactive"]["action"]["buttons"]))
            late_yes = next(b["reply"]["id"] for b in request["interactive"]["action"]["buttons"]
                            if b["reply"]["id"].startswith("availability:yes:"))
            await pool.execute("UPDATE bookings SET owner_check_expires_at=now()-interval '1 second', "
                               "hold_expires_at=now()-interval '1 second' WHERE status='held'")
            await expire_owner_availability_checks(app.state.ctx)
            await post(c, payload(OWNER, reply_id=late_yes, mid="e3"))
            await pool.execute("UPDATE bookings SET customer_reply_expires_at=now()-interval '1 second' WHERE owner_check_status='late_approved'")
            assert await expire_owner_availability_checks(app.state.ctx) == 1
        assert await pool.fetchval("SELECT owner_check_status FROM bookings") == "customer_expired"
        state = await pool.fetchval("SELECT state FROM conversations WHERE wa_id=$1", A)
        if isinstance(state, str):
            import json
            state = json.loads(state)
        assert state.get("state") != "owner_late_offer"
        await pool.close()
    asyncio.run(run())


@needs_db
def test_successful_payment_confirms_the_booking_once_even_if_safaricom_repeats_the_callback():
    async def run():
        pool, out, salon = await build(DSN)
        await pool.execute("UPDATE salons SET owner_wa_id=$1", OWNER)
        async with client() as c:
            await to_pay(c, out, pool)
            assert (await deliver(c, callback("ws_CO_1"), secret="wrong")).status_code == 404
            assert await pool.fetchval("SELECT status FROM payments") == "pending"
            r = await deliver(c, callback("ws_CO_1"))
            assert r.json() == {"ResultCode": 0, "ResultDesc": "Accepted"}
            await deliver(c, callback("ws_CO_1"))
        b = await pool.fetchrow("SELECT status, hold_expires_at FROM bookings")
        assert b["status"] == "confirmed" and b["hold_expires_at"] is None
        p = await pool.fetchrow("SELECT status, mpesa_receipt, settled_via FROM payments")
        assert (p["status"], p["mpesa_receipt"], p["settled_via"]) == ("paid", "QAB1CD2EF3", "callback")
        mine = [x for x in out.bodies(A) if "200" in x and "QAB1CD2EF3" in x]
        assert len(mine) == 1 and "booked" in mine[0].lower() and "Fade" in mine[0]
        assert len(out.bodies(OWNER)) == 1 and out.bodies(OWNER)[0].startswith("New booking: Cust, Fade")
        assert await pool.fetchval("SELECT count(*) FROM mpesa_callbacks") == 1
        assert await pool.fetchval("SELECT state FROM conversations") in ("{}", {})
        await pool.close()
    asyncio.run(run())


@needs_db
def test_a_cancelled_prompt_keeps_the_hold_and_pay_tries_again_then_succeeds():
    async def run():
        pool, out, salon = await build(DSN)
        async with client() as c:
            await to_pay(c, out, pool)
            await deliver(c, callback("ws_CO_1", code=1032))
            assert "cancelled" in out.bodies(A)[-1].lower()
            assert await pool.fetchval("SELECT status FROM bookings") == "held"
            await post(c, payload(A, "pay", mid="r1"))
            assert len(out.stk) == 2 and "sent an M-Pesa request" in out.bodies(A)[-1]
            await deliver(c, callback("ws_CO_2", receipt="R2"))
        assert await pool.fetchval("SELECT status FROM bookings") == "confirmed"
        assert sorted(await pool.fetch("SELECT status FROM payments ORDER BY created_at"), key=lambda r: r["status"])[0]["status"] == "failed"
        assert len(out.calls) == 1
        await pool.close()
    asyncio.run(run())


@needs_db
def test_sending_a_different_mpesa_number_prompts_that_number_and_a_rapid_repeat_waits():
    async def run():
        pool, out, salon = await build(DSN)
        async with client() as c:
            await to_pay(c, out, pool)
            await post(c, payload(A, "0722 333 444", mid="n1"))
            assert out.stk[-1]["PhoneNumber"] == "254722333444" and len(out.stk) == 2
            await post(c, payload(A, "0722 333 444", mid="n2"))
            assert len(out.stk) == 2 and "still on its way" in out.bodies(A)[-1]
        await pool.close()
    asyncio.run(run())


@needs_db
def test_a_late_payment_after_the_hold_expired_still_gets_the_slot_if_it_is_free():
    async def run():
        pool, out, salon = await build(DSN)
        async with client() as c:
            await to_pay(c, out, pool)
            await pool.execute("UPDATE bookings SET status='expired'")
            await deliver(c, callback("ws_CO_1"))
        assert await pool.fetchval("SELECT status FROM bookings") == "confirmed"
        assert await pool.fetchval("SELECT status FROM payments") == "paid"
        await pool.close()
    asyncio.run(run())


@needs_db
def test_a_late_payment_for_a_slot_someone_else_took_is_parked_for_refund_not_lost():
    async def run():
        pool, out, salon = await build(DSN)
        await pool.execute("UPDATE salons SET owner_wa_id=$1", OWNER)
        async with client() as c:
            await to_pay(c, out, pool)
            await pool.execute("UPDATE bookings SET status='expired'")
            await to_pay(c, out, pool, who=B, mid="c")
            assert await pool.fetchval("SELECT count(*) FROM bookings WHERE status='held'") == 1
            await deliver(c, callback("ws_CO_1", receipt="LATE1"))
        p = await pool.fetchrow("SELECT status, mpesa_receipt FROM payments WHERE checkout_request_id='ws_CO_1'")
        assert (p["status"], p["mpesa_receipt"]) == ("needs_refund", "LATE1")
        assert "taken before it arrived" in out.bodies(A)[-1] and "will refund you" in out.bodies(A)[-1]
        assert "Refund needed: KES 200 from 254711000001 (M-Pesa LATE1)" in out.bodies(OWNER)[-1]
        await pool.close()
    asyncio.run(run())


@needs_db
def test_two_successful_payments_for_one_booking_refund_the_second():
    async def run():
        pool, out, salon = await build(DSN)
        async with client() as c:
            await to_pay(c, out, pool)
            await post(c, payload(A, "0722 333 444", mid="n1"))
            await deliver(c, callback("ws_CO_1", receipt="FIRST"))
            await deliver(c, callback("ws_CO_2", receipt="SECOND"))
        rows = {r["mpesa_receipt"]: r["status"] for r in await pool.fetch("SELECT mpesa_receipt, status FROM payments")}
        assert rows == {"FIRST": "paid", "SECOND": "needs_refund"}
        await pool.close()
    asyncio.run(run())


@needs_db
def test_under_payment_is_not_treated_as_a_deposit():
    async def run():
        pool, out, salon = await build(DSN)
        async with client() as c:
            await to_pay(c, out, pool)
            await deliver(c, callback("ws_CO_1", amount=50))
        assert await pool.fetchval("SELECT status FROM payments") == "needs_refund"
        assert await pool.fetchval("SELECT status FROM bookings") == "held"
        await pool.close()
    asyncio.run(run())


@needs_db
def test_missed_callbacks_are_settled_by_asking_daraja():
    async def run():
        pool, out, salon = await build(DSN)
        async with client() as c:
            await to_pay(c, out, pool)
            ctx = c._transport.app.state.ctx
            assert await payments.reconcile(ctx) == 0
            await pool.execute("UPDATE payments SET created_at = now() - interval '2 minutes'")
            assert await payments.reconcile(ctx) == 0
            assert await pool.fetchval("SELECT status FROM payments") == "pending"
            out.queries["ws_CO_1"] = {"ResultCode": "0", "ResultDesc": "The service request is processed successfully."}
            assert await payments.reconcile(ctx) == 1
            assert await payments.reconcile(ctx) == 0
        p = await pool.fetchrow("SELECT status, settled_via, mpesa_receipt FROM payments")
        assert (p["status"], p["settled_via"], p["mpesa_receipt"]) == ("paid", "query", None)
        assert await pool.fetchval("SELECT status FROM bookings") == "confirmed"
        assert "200" in out.bodies(A)[-1] and "booked" in out.bodies(A)[-1].lower()
        await pool.close()
    asyncio.run(run())


@needs_db
def test_a_prompt_nobody_answers_is_written_off_but_a_late_success_still_wins():
    async def run():
        pool, out, salon = await build(DSN)
        async with client() as c:
            await to_pay(c, out, pool)
            ctx = c._transport.app.state.ctx
            await pool.execute("UPDATE payments SET created_at = now() - interval '10 minutes'")
            assert await payments.reconcile(ctx) == 1
            assert await pool.fetchval("SELECT status FROM payments") == "failed"
            assert "request timed out" in out.bodies(A)[-1]
            await deliver(c, callback("ws_CO_1", receipt="LATEOK"))
        assert await pool.fetchval("SELECT status FROM payments") == "paid"
        assert await pool.fetchval("SELECT status FROM bookings") == "confirmed"
        await pool.close()
    asyncio.run(run())


@needs_db
def test_a_hold_with_an_open_prompt_is_not_expired_under_the_customers_fingers():
    async def run():
        pool, out, salon = await build(DSN)
        async with client() as c:
            await to_pay(c, out, pool)
        await pool.execute("UPDATE bookings SET hold_expires_at = now() - interval '1 minute'")
        assert await expire_holds(pool) == 0
        await pool.execute("UPDATE payments SET status='failed'")
        assert await expire_holds(pool) == 1
        await pool.close()
    asyncio.run(run())


@needs_db
def test_daraja_down_keeps_the_slot_and_says_how_to_retry():
    async def run():
        pool, out, salon = await build(DSN)
        out.stk_fails = True
        async with client() as c:
            await to_pay(c, out, pool)
            assert "could not send" in out.bodies(A)[-1].lower() or "couldn't send" in out.bodies(A)[-1].lower() and 'reply "pay"' in out.bodies(A)[-1]
            assert await pool.fetchval("SELECT status FROM bookings") == "held"
            assert await pool.fetchval("SELECT status FROM payments") == "failed"
            out.stk_fails = False
            await post(c, payload(A, "pay", mid="r1"))
            assert len(out.stk) == 1 and "sent an M-Pesa request" in out.bodies(A)[-1]
        await pool.close()
    asyncio.run(run())


def test_claim_parsing_reads_amount_and_code_but_trusts_nothing():
    assert payments.parse_claim("200 QAB1CD2EF3") == (200, "QAB1CD2EF3", False)
    sms = "Confirmed. Ksh200.00 sent to Zuri Cuts on 12/2/26 at 3:00 PM. M-PESA Ref: qab1cd2ef3."
    assert payments.parse_claim(sms) == (200, "QAB1CD2EF3", False)
    assert payments.parse_claim("KSH200 sent. M-PESA Ref: QAB1CD2EF3") == (200, "QAB1CD2EF3", False)
    assert payments.parse_claim("0712345678") == (None, None, False)
    assert payments.parse_claim("200 0712345678") == (200, None, False)
    assert payments.parse_claim("send to till 5012345") == (None, None, False)
    assert payments.parse_claim("200") == (200, None, False)
    assert payments.parse_claim("200 300 QAB1CD2EF3") == (None, "QAB1CD2EF3", True)
    assert payments.parse_claim("200 Airtel Money") == (200, None, False)


def test_manual_payment_instructions_match_the_selected_provider_and_destination():
    till = {"kind": "till", "party_b": "5012345", "manual": True}
    pochi = {"kind": "phone", "party_b": "254712345678", "manual": True, "phone_type": "pochi"}
    send = {"kind": "phone", "party_b": "254712345678", "manual": True, "phone_type": "send_money"}
    airtel = {"kind": "phone", "party_b": "254733222111", "manual": True, "phone_type": "airtel_money"}
    assert payments.destination_label(till) == "M-PESA manual till 5012345"
    assert "seven-digit till number: 5012345" in payments.manual_instructions("en", 200, till, "Cuts")
    assert "Pochi la Biashara" in payments.manual_instructions("en", 200, pochi, "Cuts")
    assert "Send Money" in payments.manual_instructions("en", 200, send, "Cuts")
    airtel_text = payments.manual_instructions("en", 200, airtel, "Cuts")
    assert "Airtel Money number: 0733 222 111" in airtel_text
    assert "M-PESA" not in airtel_text


@needs_db
def test_manual_airtel_payment_uses_provider_specific_reference_and_confirmation():
    async def run():
        pool, out, salon = await build(DSN)
        await pool.execute(
            "UPDATE salon_payment SET kind='phone', shortcode=NULL, party_b='254712345678', manual=true, phone_type='airtel_money' "
            "WHERE salon_id=$1::uuid", salon)
        await pool.execute("UPDATE salons SET owner_wa_id=$1 WHERE id=$2::uuid", OWNER, salon)
        async with client() as c:
            await to_pay(c, out, pool, mid="airtel")
            assert out.stk == []
            assert "Airtel Money number: 0712 345 678" in out.bodies(A)[-1]
            await post(c, payload(A, "200 AM12345678", mid="airtel-claim"))
            payment = await pool.fetchrow("SELECT id::text, provider, status FROM payments")
            assert (payment["provider"], payment["status"]) == ("airtel_money", "pending")
            tap = [b["reply"]["id"] for m in out.to(OWNER) if m["type"] == "interactive"
                   for b in m["interactive"]["action"]["buttons"] if b["reply"]["id"].startswith("till:yes:")][0]
            await post(c, payload(OWNER, reply_id=tap, mid="airtel-tap"))
        payment = await pool.fetchrow("SELECT provider, status FROM payments")
        assert (payment["provider"], payment["status"]) == ("airtel_money", "paid")
        assert "Airtel Money payment" in out.bodies(A)[-1]
        assert "Airtel Money" in out.bodies(OWNER)[-1]
        await pool.close()
    asyncio.run(run())


@needs_db
def test_deposit_booking_is_blocked_when_no_destination_is_configured():
    async def run():
        pool, out, salon = await build(DSN, till=False)
        async with client() as c:
            await to_pay(c, out, pool, mid="no-destination")
        assert "has not set a payment destination" in out.bodies(A)[-1]
        assert out.stk == []
        assert await pool.fetchval("SELECT count(*) FROM bookings") == 0
        await pool.close()
    asyncio.run(run())


async def manual_salon(pool, salon):
    await pool.execute("UPDATE salons SET owner_wa_id=$1", OWNER)
    await pool.execute("UPDATE salon_payment SET manual=true, shortcode=NULL WHERE salon_id=$1::uuid", salon)


@needs_db
def test_manual_till_confirms_on_owner_tap():
    async def run():
        pool, out, salon = await build(DSN)
        await manual_salon(pool, salon)
        async with client() as c:
            await to_pay(c, out, pool)
        assert out.stk == []
        customer_prompt = out.bodies(A)[-1]
        assert "seven-digit till number: 5012345" in customer_prompt
        assert "which M-Pesa number should we use" not in customer_prompt
        buttons = [item["reply"] for item in out.to(A)[-1]["interactive"]["action"]["buttons"]]
        assert {item["title"] for item in buttons} == {"I've paid", "Cancel booking"}
        paid_id = next(item["id"] for item in buttons if item["id"].startswith("manualpay:paid:"))
        async with client() as c:
            await post(c, payload(A, reply_id=paid_id, mid="paid-button"))
        assert "screenshot" in out.bodies(A)[-1].lower() and "transaction reference" in out.bodies(A)[-1].lower()
        async with client() as c:
            await post(c, payload(A, "200 QAB1CD2EF3", mid="claim1"))
        p = await pool.fetchrow("SELECT id::text, status, method, amount_kes, mpesa_receipt FROM payments")
        assert (p["status"], p["method"], p["amount_kes"], p["mpesa_receipt"]) == ("pending", "manual", 200, "QAB1CD2EF3")
        assert await pool.fetchval("SELECT status FROM bookings") == "held"
        assert await pool.fetchval("SELECT hold_expires_at > now() + interval '20 minutes' FROM bookings")
        assert "waiting for Zuri Cuts to verify" in out.bodies(A)[-1]
        req = [m for m in out.to(OWNER) if m["type"] == "interactive"][-1]
        yes = [b["reply"]["id"] for b in req["interactive"]["action"]["buttons"] if b["reply"]["id"].startswith("till:yes:")]
        assert len(yes) == 1 and yes[0].endswith(p["id"])
        async with client() as c:
            await post(c, payload(OWNER, reply_id=yes[0], mid="tap1"))
        assert await pool.fetchval("SELECT status FROM payments") == "paid"
        assert await pool.fetchval("SELECT status FROM bookings") == "confirmed"
        assert "You're booked" in out.bodies(A)[-1]
        assert "Deposit KES 200 received via M-Pesa" in out.bodies(OWNER)[-1]
        n_before = len(out.bodies(A))
        async with client() as c:
            await post(c, payload(OWNER, reply_id=yes[0], mid="tap2"))
        assert await pool.fetchval("SELECT count(*) FROM payments WHERE status='paid'") == 1
        assert "already confirmed" in out.bodies(OWNER)[-1]
        assert len(out.bodies(A)) == n_before
        await pool.close()
    asyncio.run(run())


@needs_db
def test_owner_no_leaves_the_hold_open_for_a_better_claim():
    async def run():
        pool, out, salon = await build(DSN)
        await manual_salon(pool, salon)
        async with client() as c:
            await to_pay(c, out, pool)
            await post(c, payload(A, "200 QAB1CD2EF3", mid="claim1"))
        req = [m for m in out.to(OWNER) if m["type"] == "interactive"][-1]
        no = [b["reply"]["id"] for b in req["interactive"]["action"]["buttons"] if b["reply"]["id"].startswith("till:no:")][0]
        async with client() as c:
            await post(c, payload(OWNER, reply_id=no, mid="tap1"))
        assert await pool.fetchval("SELECT status FROM payments") == "failed"
        assert await pool.fetchval("SELECT status FROM bookings") == "held"
        assert "could not confirm" in out.bodies(A)[-1]
        assert "could not verify this payment claim" in out.bodies(OWNER)[-1]
        async with client() as c:
            await post(c, payload(A, "200 QAB1CD2EF3", mid="claim2"))
        assert "already used" in out.bodies(A)[-1]
        await pool.close()
    asyncio.run(run())


@needs_db
def test_amount_mismatch_claim_stays_in_the_owner_inbox_until_the_owner_resolves_it():
    async def run():
        pool, out, salon = await build(DSN)
        await manual_salon(pool, salon)
        async with client() as c:
            await to_pay(c, out, pool)
            await post(c, payload(A, "150 QAB1CD2EF3", mid="claim1"))
        assert await pool.fetchval("SELECT status FROM payments") == "review"
        review = await pool.fetchrow("SELECT reason, status, expected_amount, received_amount FROM payment_reviews")
        assert (review["reason"], review["status"], review["expected_amount"], review["received_amount"]) == ("amount_mismatch", "pending_review", 200, 150)
        assert await pool.fetchval("SELECT status FROM bookings") == "held"
        assert "human eye" in out.bodies(A)[-1]
        assert "KES 150" in out.bodies(OWNER)[-1]
        async with client() as c:
            await post(c, payload(OWNER, "pending payments", mid="queue-review"))
        req = [m for m in out.to(OWNER) if m["type"] == "interactive"][-1]
        yes = [b["reply"]["id"] for b in req["interactive"]["action"]["buttons"] if b["reply"]["id"].startswith("till:yes:")]
        assert len(yes) == 1
        async with client() as c:
            await post(c, payload(OWNER, reply_id=yes[0], mid="approve-review"))
        assert await pool.fetchval("SELECT status FROM payments") == "paid"
        assert await pool.fetchval("SELECT amount_kes FROM payments") == 150
        assert await pool.fetchval("SELECT status FROM bookings") == "confirmed"
        assert await pool.fetchval("SELECT status FROM payment_reviews") == "closed_no_refund"
        await pool.close()
    asyncio.run(run())


@needs_db
def test_manual_claims_wait_for_the_owner_not_the_sweeper():
    async def run():
        from app.main import app
        pool, out, salon = await build(DSN)
        await manual_salon(pool, salon)
        async with client() as c:
            await to_pay(c, out, pool)
            await post(c, payload(A, "200 QAB1CD2EF3", mid="claim1"))
        assert "Please wait and do not pay again" in out.bodies(A)[-1]
        assert await payments.reconcile(app.state.ctx) == 0
        assert await pool.fetchval("SELECT status FROM payments") == "pending"
        await pool.execute("UPDATE bookings SET hold_expires_at=now() - interval '1 minute'")
        await pool.execute("UPDATE payments SET created_at=now() - interval '10 minutes'")
        assert await expire_holds(pool) == 1
        assert await pool.fetchval("SELECT status FROM bookings") == "expired"
        assert await pool.fetchval("SELECT status FROM payments") == "pending"
        assert await payments.notify_expired_manual_claims(app.state.ctx) == 1
        assert "claim is still open" in out.bodies(A)[-1]
        assert await payments.notify_expired_manual_claims(app.state.ctx) == 0
        pay_id = await pool.fetchval("SELECT id::text FROM payments")
        result, _ = await payments.confirm_manual_claim(pool, pay_id)
        assert result == "confirmed"
        assert await pool.fetchval("SELECT status FROM payments") == "paid"
        assert await pool.fetchval("SELECT status FROM bookings") == "confirmed"
        await pool.close()
    asyncio.run(run())


@needs_db
def test_a_late_owner_confirmation_never_rebooks_a_slot_taken_after_the_hold_expired():
    async def run():
        pool, out, salon = await build(DSN)
        await manual_salon(pool, salon)
        async with client() as c:
            await to_pay(c, out, pool)
            await post(c, payload(A, "200 QAB1CD2EF3", mid="claim1"))
        await pool.execute("UPDATE bookings SET hold_expires_at=now() - interval '1 minute'")
        await pool.execute("UPDATE payments SET created_at=now() - interval '10 minutes'")
        assert await expire_holds(pool) == 1
        await pool.execute(
            "INSERT INTO bookings (salon_id, staff_id, service_id, client_id, starts_at, ends_at, status) "
            "SELECT salon_id, staff_id, service_id, client_id, starts_at, ends_at, 'confirmed' FROM bookings WHERE status='expired'")
        pay_id = await pool.fetchval("SELECT id::text FROM payments")
        result, _ = await payments.confirm_manual_claim(pool, pay_id)
        assert result == "unavailable"
        assert await pool.fetchval("SELECT status FROM payments WHERE id=$1::uuid", pay_id) == "needs_refund"
        review = await pool.fetchrow("SELECT reason, status FROM payment_reviews WHERE payment_id=$1::uuid", pay_id)
        assert (review["reason"], review["status"]) == ("slot_unavailable", "refund_due")
        assert await pool.fetchval("SELECT count(*) FROM bookings WHERE status='confirmed'") == 1
        await pool.close()
    asyncio.run(run())


@needs_db
def test_cancelling_a_booking_keeps_its_manual_claim_until_the_owner_resolves_it():
    async def run():
        pool, out, salon = await build(DSN)
        await manual_salon(pool, salon)
        async with client() as c:
            await to_pay(c, out, pool)
            await post(c, payload(A, "200 QAB1CD2EF3", mid="claim1"))
            waiting = out.to(A)[-1]
            cancel_id = next(item["reply"]["id"] for item in waiting["interactive"]["action"]["buttons"]
                             if item["reply"]["id"].startswith("manualpay:cancel:"))
            await post(c, payload(A, reply_id=cancel_id, mid="cancel-button"))
        assert await pool.fetchval("SELECT status FROM bookings") == "cancelled"
        assert await pool.fetchval("SELECT status FROM payments") == "pending"
        assert "still has your payment claim" in out.bodies(A)[-1]
        pay_id = await pool.fetchval("SELECT id::text FROM payments")
        result, _ = await payments.confirm_manual_claim(pool, pay_id)
        assert result == "unavailable"
        assert await pool.fetchval("SELECT status FROM payments") == "needs_refund"
        review = await pool.fetchrow("SELECT reason, status FROM payment_reviews WHERE payment_id=$1::uuid", pay_id)
        assert (review["reason"], review["status"]) == ("payment_unresolved", "refund_due")
        await pool.close()
    asyncio.run(run())


@needs_db
def test_a_deposit_booking_is_blocked_when_the_salon_has_no_payment_destination():
    async def run():
        pool, out, salon = await build(DSN, till=False)
        await pool.execute("UPDATE salons SET owner_wa_id=$1", OWNER)
        async with client() as c:
            await to_pay(c, out, pool)
        assert out.stk == [] and await pool.fetchval("SELECT status FROM bookings") is None
        assert "has not set a payment destination" in out.bodies(A)[-1]
        assert out.bodies(OWNER) == []
        await pool.close()
    asyncio.run(run())


@needs_db
def test_a_foreign_whatsapp_number_is_asked_for_an_mpesa_number_and_cancelling_a_paid_booking_is_flagged():
    async def run():
        pool, out, salon = await build(DSN)
        await pool.execute("UPDATE salons SET owner_wa_id=$1", OWNER)
        FOREIGN = "447700900123"
        async with client() as c:
            await to_pay(c, out, pool, who=FOREIGN)
            assert out.stk == [] and "m-pesa number" in out.bodies(FOREIGN)[-1].lower()
            await post(c, payload(FOREIGN, "0711 000 001", mid="n1"))
            assert out.stk[0]["PhoneNumber"] == "254711000001"
            await deliver(c, callback("ws_CO_1"))
            out.script["cancel it"] = {"intent": "cancel"}
            await post(c, payload(FOREIGN, "cancel it", mid="x1"))
            assert "handles deposit refunds" in out.bodies(FOREIGN)[-1]
            assert "Refund needed: KES 200" in out.bodies(OWNER)[-1] and "customer cancelled" in out.bodies(OWNER)[-1]
        await pool.close()
    asyncio.run(run())


@needs_db
def test_a_sender_flooding_the_number_is_cut_off_so_model_credits_are_safe():
    async def run():
        pool, out, salon = await build(DSN, rate_limit_per_min=5)
        async with client() as c:
            for i in range(12):
                await post(c, payload(A, "prices?", mid=f"f{i}"))
            await post(c, payload(B, "prices?", mid="other"))
        assert len(out.to(A)) == 6 and len(out.to(B)) == 1
        assert "very quickly" in out.bodies(A)[-1] and "haraka" in out.bodies(A)[-1]
        await pool.close()
    asyncio.run(run())


@needs_db
def test_doctor_reports_what_works_and_says_what_to_fix():
    import json as _json
    import httpx as _httpx
    from app.doctor import run_checks
    from tests.harness import app as _app

    async def run():
        pool, out, salon = await build(DSN, till=False)
        s = _app.state.settings
        seen = []
        payment_answer = {
            "is_payment_confirmation": True, "provider": "mpesa", "currency": "KES", "amount": 10,
            "reference": "RMB2AI7K9Q", "date": "2026-11-03", "time": "10:47",
            "recipient": "REMBOAI DEMO SALON", "confidence": 0.99,
        }

        def handler(req):
            seen.append(req.url.host)
            if req.url.host == "graph.facebook.com":
                return _httpx.Response(200, json={"display_phone_number": "+254 700 000000", "verified_name": "Zuri Cuts"})
            if req.url.path.endswith("/chat/completions"):
                body = _json.loads(req.content)
                content = body["messages"][-1]["content"]
                prompt = body["messages"][0]["content"].lower()
                if "payment screenshot" in prompt or "payment_screenshot" in prompt:
                    ans = payment_answer
                else:
                    ans = {"color": "red"} if isinstance(content, list) else {"intent": "book", "service": "Fade", "date": None, "window": "afternoon"}
                return _httpx.Response(200, json={"choices": [{"message": {"content": _json.dumps(ans)}}]})
            if req.url.path == "/oauth/v1/generate":
                return _httpx.Response(200, json={"access_token": "AT", "expires_in": "3599"})
            return _httpx.Response(200, json={"ok": True})
        async with _httpx.AsyncClient(transport=_httpx.MockTransport(handler)) as http:
            checks = {c.name: c for c in await run_checks(s, http, pool)}
        assert checks["configuration"].ok and checks["database"].ok
        assert checks["salon 'Zuri Cuts' WhatsApp"].ok and "Zuri Cuts" in checks["salon 'Zuri Cuts' WhatsApp"].detail
        assert checks["model muse-spark-1.3"].ok and "intent=book service=Fade" in checks["model muse-spark-1.3"].detail
        assert not checks["salon 'Zuri Cuts' deposits"].ok and checks["salon 'Zuri Cuts' deposits"].warn
        assert checks["photo matching muse-spark-1.3"].ok and "red" in checks["photo matching muse-spark-1.3"].detail
        assert checks["payment screenshot vision muse-spark-1.3"].ok
        payment_answer["amount"] = 11
        async with _httpx.AsyncClient(transport=_httpx.MockTransport(handler)) as http:
            bad_checks = {c.name: c for c in await run_checks(s, http, pool)}
        assert not bad_checks["payment screenshot vision muse-spark-1.3"].ok
        assert checks["M-Pesa"].ok and checks["M-Pesa mode"].warn and checks["model failover"].warn
        await pool.close()
    asyncio.run(run())


@needs_db
def test_concurrent_duplicate_payments_park_the_loser_for_refund():
    async def run():
        from app.payments import settle
        pool, out, salon = await build(DSN)
        async with client() as c:
            await to_pay(c, out, pool)
            booking = await pool.fetchval("SELECT id::text FROM bookings")
            await pool.execute(
                "INSERT INTO payments (booking_id, salon_id, phone, amount_kes, checkout_request_id, source_message_id) "
                "VALUES ($1::uuid, $2::uuid, $3, 200, 'ws_CO_2', 'x')",
                booking, salon, A)
            first = parse_callback(callback("ws_CO_1", receipt="FIRST"))
            second = parse_callback(callback("ws_CO_2", receipt="SECOND"))
            got = await asyncio.gather(settle(pool, first, "callback"), settle(pool, second, "callback"))
            assert sorted(s for s, _ in got) == ["settled", "settled"]
        rows = {r["mpesa_receipt"]: (r["status"], r["refund_reason"]) for r in await pool.fetch("SELECT mpesa_receipt, status, refund_reason FROM payments")}
        assert rows == {"FIRST": ("paid", None), "SECOND": ("needs_refund", "duplicate")}
        review = await pool.fetchrow("SELECT reason, payment_reviews.status, expected_amount, received_amount FROM payment_reviews JOIN payments ON payments.id=payment_reviews.payment_id WHERE payments.mpesa_receipt='SECOND'")
        assert (review["reason"], review["status"], review["expected_amount"], review["received_amount"]) == ("payment_unresolved", "refund_due", 200, 200)
        await pool.close()
    asyncio.run(run())


@needs_db
def test_overpayment_is_parked_for_refund_not_kept():
    async def run():
        pool, out, salon = await build(DSN)
        await pool.execute("UPDATE salons SET owner_wa_id=$1", OWNER)
        async with client() as c:
            await to_pay(c, out, pool)
            await deliver(c, callback("ws_CO_1", receipt="BIG", amount=500))
        p = await pool.fetchrow("SELECT status, refund_reason FROM payments")
        assert (p["status"], p["refund_reason"]) == ("needs_refund", "overpaid")
        assert "more than the deposit" in out.bodies(A)[-1] and "will refund you" in out.bodies(A)[-1]
        assert "Refund needed: KES 200" in out.bodies(OWNER)[-1] and "more than the deposit" in out.bodies(OWNER)[-1]
        assert await pool.fetchval("SELECT reason FROM payment_reviews") == "amount_mismatch"
        await pool.close()
    asyncio.run(run())


@needs_db
def test_customer_cancel_parks_a_paid_deposit_for_refund():
    async def run():
        pool, out, salon = await build(DSN)
        await pool.execute("UPDATE salons SET owner_wa_id=$1", OWNER)
        async with client() as c:
            await to_pay(c, out, pool)
            await deliver(c, callback("ws_CO_1"))
            assert await pool.fetchval("SELECT status FROM payments") == "paid"
            out.script["cancel it"] = {"intent": "cancel"}
            await post(c, payload(A, "cancel it", mid="c1"))
            assert "cancelled" in out.bodies(A)[-1].lower()
        p = await pool.fetchrow("SELECT status, refund_reason, announced_at FROM payments")
        assert (p["status"], p["refund_reason"]) == ("needs_refund", "cancelled") and p["announced_at"] is not None
        assert await pool.fetchval("SELECT status FROM payment_reviews") == "refund_due"
        assert await pool.fetchval("SELECT count(*) FROM reminder_jobs WHERE status='pending'") == 0
        await pool.close()
    asyncio.run(run())


@needs_db
def test_callback_success_with_details_survives_a_disagreeing_status_query():
    async def run():
        pool, out, salon = await build(DSN, mpesa_verify_callbacks=True)
        async with client() as c:
            await to_pay(c, out, pool)
            out.queries["ws_CO_1"] = {"ResultCode": 1032, "ResultDesc": "Request cancelled by user"}
            await deliver(c, callback("ws_CO_1", receipt="REAL", amount=200))
        p = await pool.fetchrow("SELECT status, mpesa_receipt FROM payments")
        assert (p["status"], p["mpesa_receipt"]) == ("paid", "REAL")
        assert await pool.fetchval("SELECT status FROM bookings") == "confirmed"
        await pool.close()
    asyncio.run(run())


@needs_db
def test_a_repeat_failure_refreshes_the_recorded_reason_instead_of_keeping_stale_data():
    async def run():
        from app.payments import settle
        pool, out, salon = await build(DSN)
        async with client() as c:
            await to_pay(c, out, pool)
            await deliver(c, callback("ws_CO_1", code=1037))
        status, _ = await settle(pool, parse_callback(callback("ws_CO_1", code=1)), "query")
        assert status == "seen"
        p = await pool.fetchrow("SELECT status, result_code FROM payments")
        assert (p["status"], p["result_code"]) == ("failed", 1)
        await pool.close()
    asyncio.run(run())


@needs_db
def test_query_success_settles_when_the_callback_said_it_failed():
    async def run():
        pool, out, salon = await build(DSN, mpesa_verify_callbacks=True)
        async with client() as c:
            await to_pay(c, out, pool)
            out.queries["ws_CO_1"] = {"ResultCode": 0, "ResultDesc": "The service request is processed successfully."}
            await deliver(c, callback("ws_CO_1", code=1032))
        assert await pool.fetchval("SELECT status FROM payments") == "paid"
        assert await pool.fetchval("SELECT status FROM bookings") == "confirmed"
        await pool.close()
    asyncio.run(run())


@needs_db
def test_doctor_reports_a_malformed_public_url_instead_of_crashing():
    async def run():
        import httpx
        from app.doctor import run_checks
        from tests.harness import app as _app
        pool, out, salon = await build(DSN, public_base_url="not-a-url", asr_url="")
        async with httpx.AsyncClient(transport=httpx.MockTransport(out.handler)) as http:
            checks = {c.name: c for c in await run_checks(_app.state.settings, http, pool)}
        assert not checks["public URL"].ok and "https://your-domain" in checks["public URL"].detail
        await pool.close()
    asyncio.run(run())
