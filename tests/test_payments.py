"""M-Pesa deposits end to end: real app, real Daraja client, real Postgres; Safaricom and Meta are faked at the HTTP layer."""
import asyncio
import os

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


async def deliver(c, body: dict, secret: str = CB_SECRET):
    return await c.post(f"/mpesa/callback/{secret}", json=body)


@needs_db
def test_tapping_a_slot_sends_an_stk_prompt_to_the_chat_number_for_the_till():
    async def run():
        pool, out, salon = await build(DSN)
        async with client() as c:
            await to_pay(c, out, pool)
        assert len(out.stk) == 1
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
        assert await pool.fetchval("SELECT count(*) FROM mpesa_callbacks") == 2
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
    assert payments.parse_claim("0712345678") == (None, None, False)
    assert payments.parse_claim("send to till 5012345") == (None, None, False)
    assert payments.parse_claim("200") == (200, None, False)
    assert payments.parse_claim("200 300 QAB1CD2EF3") == (None, "QAB1CD2EF3", True)


async def manual_salon(pool, salon):
    await pool.execute("UPDATE salons SET owner_wa_id=$1", OWNER)
    await pool.execute("UPDATE salon_payment SET manual=true WHERE salon_id=$1::uuid", salon)


@needs_db
def test_manual_till_confirms_on_owner_tap():
    async def run():
        pool, out, salon = await build(DSN)
        await manual_salon(pool, salon)
        async with client() as c:
            await to_pay(c, out, pool)
        assert out.stk == []
        assert "till 5012345" in out.bodies(A)[-1]
        async with client() as c:
            await post(c, payload(A, "200 QAB1CD2EF3", mid="claim1"))
        p = await pool.fetchrow("SELECT id::text, status, method, amount_kes, mpesa_receipt FROM payments")
        assert (p["status"], p["method"], p["amount_kes"], p["mpesa_receipt"]) == ("pending", "manual", 200, "QAB1CD2EF3")
        assert await pool.fetchval("SELECT status FROM bookings") == "held"
        assert await pool.fetchval("SELECT hold_expires_at > now() + interval '20 minutes' FROM bookings")
        assert "waiting for Zuri Cuts to confirm" in out.bodies(A)[-1]
        req = [m for m in out.to(OWNER) if m["type"] == "interactive"][-1]
        yes = [b["reply"]["id"] for b in req["interactive"]["action"]["buttons"] if b["reply"]["id"].startswith("till:yes:")]
        assert len(yes) == 1 and yes[0].endswith(p["id"])
        async with client() as c:
            await post(c, payload(OWNER, reply_id=yes[0], mid="tap1"))
        assert await pool.fetchval("SELECT status FROM payments") == "paid"
        assert await pool.fetchval("SELECT status FROM bookings") == "confirmed"
        assert "You're all booked" in out.bodies(A)[-1]
        assert "Deposit KES 200 paid" in out.bodies(OWNER)[-1]
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
        assert "better claim" in out.bodies(OWNER)[-1]
        async with client() as c:
            await post(c, payload(A, "200 QAB1CD2EF3", mid="claim2"))
        assert "already used" in out.bodies(A)[-1]
        await pool.close()
    asyncio.run(run())


@needs_db
def test_an_unclear_claim_goes_to_review_not_to_the_owner_buttons():
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
        assert all(m["type"] == "text" for m in out.to(OWNER))
        assert "KES 150" in out.bodies(OWNER)[-1]
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
        assert await payments.reconcile(app.state.ctx) == 0
        assert await pool.fetchval("SELECT status FROM payments") == "pending"
        await pool.execute("UPDATE bookings SET hold_expires_at=now() - interval '1 minute'")
        await pool.execute("UPDATE payments SET created_at=now() - interval '10 minutes'")
        assert await expire_holds(pool) == 1
        assert await pool.fetchval("SELECT status FROM bookings") == "expired"
        assert await pool.fetchval("SELECT status FROM payments") == "failed"
        await pool.close()
    asyncio.run(run())


@needs_db
def test_cancelling_drops_a_waiting_manual_claim():
    async def run():
        pool, out, salon = await build(DSN)
        await manual_salon(pool, salon)
        async with client() as c:
            await to_pay(c, out, pool)
            await post(c, payload(A, "200 QAB1CD2EF3", mid="claim1"))
            out.script["cancel it"] = {"intent": "cancel"}
            await post(c, payload(A, "cancel it", mid="x1"))
        assert await pool.fetchval("SELECT status FROM bookings") == "cancelled"
        assert await pool.fetchval("SELECT status FROM payments") == "failed"
        await pool.close()
    asyncio.run(run())


@needs_db
def test_a_salon_without_a_till_confirms_bookings_without_a_deposit():
    async def run():
        pool, out, salon = await build(DSN, till=False)
        await pool.execute("UPDATE salons SET owner_wa_id=$1", OWNER)
        async with client() as c:
            await to_pay(c, out, pool)
        assert out.stk == [] and await pool.fetchval("SELECT status FROM bookings") == "confirmed"
        assert "booked" in out.bodies(A)[-1].lower() and "Fade" in out.bodies(A)[-1] and "No deposit taken" in out.bodies(OWNER)[-1]
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

        def handler(req):
            seen.append(req.url.host)
            if req.url.host == "graph.facebook.com":
                return _httpx.Response(200, json={"display_phone_number": "+254 700 000000", "verified_name": "Zuri Cuts"})
            if req.url.path.endswith("/chat/completions"):
                content = _json.loads(req.content)["messages"][-1]["content"]
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
