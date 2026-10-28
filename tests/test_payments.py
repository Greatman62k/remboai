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
        assert "Holding Fade" in body and "KES 200 deposit to 254711000001" in body
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
            assert await pool.fetchval("SELECT status FROM payments") == "pending"          # a forged callback changes nothing
            r = await deliver(c, callback("ws_CO_1"))
            assert r.json() == {"ResultCode": 0, "ResultDesc": "Accepted"}
            await deliver(c, callback("ws_CO_1"))                                          # Safaricom retries
        b = await pool.fetchrow("SELECT status, hold_expires_at FROM bookings")
        assert b["status"] == "confirmed" and b["hold_expires_at"] is None
        p = await pool.fetchrow("SELECT status, mpesa_receipt, settled_via FROM payments")
        assert (p["status"], p["mpesa_receipt"], p["settled_via"]) == ("paid", "QAB1CD2EF3", "callback")
        mine = [x for x in out.bodies(A) if x.startswith("Payment of KES 200 received (M-Pesa QAB1CD2EF3)")]
        assert len(mine) == 1 and "You're booked: Fade" in mine[0]
        assert len(out.bodies(OWNER)) == 1 and out.bodies(OWNER)[0].startswith("New booking: Cust, Fade")
        assert await pool.fetchval("SELECT count(*) FROM mpesa_callbacks") == 2              # both kept for audit
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
            assert "you cancelled the request" in out.bodies(A)[-1]
            assert await pool.fetchval("SELECT status FROM bookings") == "held"
            await post(c, payload(A, "pay", mid="r1"))                                   # no model call needed
            assert len(out.stk) == 2 and "sent an M-Pesa request" in out.bodies(A)[-1]
            await deliver(c, callback("ws_CO_2", receipt="R2"))
        assert await pool.fetchval("SELECT status FROM bookings") == "confirmed"
        assert sorted(await pool.fetch("SELECT status FROM payments ORDER BY created_at"), key=lambda r: r["status"])[0]["status"] == "failed"
        assert len(out.calls) == 1                                                       # only the first booking message used the model
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
            await post(c, payload(A, "0722 333 444", mid="n2"))                          # prompt still on its way: do not stack another
            assert len(out.stk) == 2 and "still on its way" in out.bodies(A)[-1]
        await pool.close()
    asyncio.run(run())


@needs_db
def test_a_late_payment_after_the_hold_expired_still_gets_the_slot_if_it_is_free():
    async def run():
        pool, out, salon = await build(DSN)
        async with client() as c:
            await to_pay(c, out, pool)
            await pool.execute("UPDATE bookings SET status='expired'")                   # hold lapsed while the customer was away
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
            await to_pay(c, out, pool, who=B, mid="c")                                   # B is offered and takes the same slot
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
            assert await payments.reconcile(ctx) == 0                                    # too fresh to chase
            await pool.execute("UPDATE payments SET created_at = now() - interval '2 minutes'")
            assert await payments.reconcile(ctx) == 0                                    # Daraja: still being processed
            assert await pool.fetchval("SELECT status FROM payments") == "pending"
            out.queries["ws_CO_1"] = {"ResultCode": "0", "ResultDesc": "The service request is processed successfully."}
            assert await payments.reconcile(ctx) == 1
            assert await payments.reconcile(ctx) == 0                                    # already settled
        p = await pool.fetchrow("SELECT status, settled_via, mpesa_receipt FROM payments")
        assert (p["status"], p["settled_via"], p["mpesa_receipt"]) == ("paid", "query", None)
        assert await pool.fetchval("SELECT status FROM bookings") == "confirmed"
        assert out.bodies(A)[-1].startswith("Payment of KES 200 received. You're booked")  # no receipt known from a query
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
            await deliver(c, callback("ws_CO_1", receipt="LATEOK"))                      # the callback finally arrives
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
            assert "couldn't send the M-Pesa request" in out.bodies(A)[-1] and 'reply "pay"' in out.bodies(A)[-1]
            assert await pool.fetchval("SELECT status FROM bookings") == "held"
            assert await pool.fetchval("SELECT status FROM payments") == "failed"
            out.stk_fails = False
            await post(c, payload(A, "pay", mid="r1"))
            assert len(out.stk) == 1 and "sent an M-Pesa request" in out.bodies(A)[-1]
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
        assert out.bodies(A)[-1].startswith("You're booked: Fade") and "No deposit taken" in out.bodies(OWNER)[-1]
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
            assert out.stk == [] and "Which M-Pesa number" in out.bodies(FOREIGN)[-1]
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
            await post(c, payload(B, "prices?", mid="other"))                            # somebody else is unaffected
        assert len(out.to(A)) == 5 and len(out.to(B)) == 1                                # 5 a minute are answered, the rest ignored
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
        assert not checks["salon 'Zuri Cuts' deposits"].ok and checks["salon 'Zuri Cuts' deposits"].warn      # deposit set, no till
        assert checks["photo matching muse-spark-1.3"].ok and "red" in checks["photo matching muse-spark-1.3"].detail
        assert checks["M-Pesa"].ok and checks["M-Pesa mode"].warn and checks["model failover"].warn
        await pool.close()
    asyncio.run(run())
