"""The owner's web/app API: sign-in, isolation between salons, and each thing the owner can change."""
import asyncio
import os

import pytest

from app import owner
from tests.harness import build, callback, client, open_day, payload, post
from tests.test_payments import A, B, OWNER, deliver, to_pay

DSN = os.environ.get("TEST_DATABASE_URL")
needs_db = pytest.mark.skipif(not DSN, reason="needs TEST_DATABASE_URL")
H = {"X-Rembo": "1"}


async def sign_in(c, salon_id: str) -> dict:
    """What the page does: take the single-use link token, trade it for a session, then use it as a bearer token."""
    ctx = c._transport.app.state.ctx
    token = await owner.create_link(ctx, salon_id)
    r = await c.post("/api/owner/session", json={"token": token})
    assert r.status_code == 200, r.text
    return {"Authorization": "Bearer " + r.json()["session_token"]}


async def second_salon(pool) -> str:
    sid = await pool.fetchval("INSERT INTO salons (name, wa_phone_number_id) VALUES ('Other Cuts','PID2') RETURNING id::text")
    await pool.execute("INSERT INTO staff (salon_id, name) VALUES ($1::uuid,'Zed')", sid)
    return sid


@needs_db
def test_the_whatsapp_link_signs_the_owner_in_once_and_only_the_owner_gets_one():
    async def run():
        pool, out, salon = await build(DSN)
        await pool.execute("UPDATE salons SET owner_wa_id=$1", OWNER)
        async with client() as c:
            await post(c, payload(B, "link", mid="b1"))                                    # a customer cannot get a sign-in link
            assert not any("/owner/login/" in x for x in out.bodies(B))
            await post(c, payload(OWNER, "link", mid="o1"))
            url = out.bodies(OWNER)[-1].split("\n")[1]
            assert url.startswith("https://rembo.test/owner/login/")
            token = url.rsplit("/", 1)[1]
            assert await pool.fetchval("SELECT count(*) FROM owner_links WHERE token_hash=$1", token) == 0   # only the hash is stored
            r = await c.post("/api/owner/session", json={"token": token})
            assert r.status_code == 200 and "httponly" in r.headers["set-cookie"].lower() and "samesite=strict" in r.headers["set-cookie"].lower()
            assert (await c.post("/api/owner/session", json={"token": token})).status_code == 401            # single use
            expired = await owner.create_link(c._transport.app.state.ctx, salon, minutes=-1)
            assert (await c.post("/api/owner/session", json={"token": expired})).status_code == 401
            await post(c, payload(OWNER, "kiungo", mid="o2"))
            assert "Ukurasa wa saluni" in out.bodies(OWNER)[-1]
        await pool.close()
    asyncio.run(run())


@needs_db
def test_requests_need_a_session_and_cookie_sessions_need_the_csrf_header():
    async def run():
        pool, out, salon = await build(DSN)
        async with client() as c:
            assert (await c.get("/api/owner/me")).status_code == 401
            assert (await c.get("/api/owner/me", headers={"Authorization": "Bearer nope"})).status_code == 401
            bearer = await sign_in(c, salon)
            me = (await c.get("/api/owner/me", headers=bearer)).json()
            assert me["name"] == "Zuri Cuts" and me["payment"]["party_b"] == "5012345" and me["open_hours"]["sun"] is None
            cookie = {"Cookie": "rembo_session=" + bearer["Authorization"][7:]}
            assert (await c.get("/api/owner/me", headers=cookie)).status_code == 200
            assert (await c.patch("/api/owner/salon", json={"deposit_kes": 100}, headers=cookie)).status_code == 403        # forged cross-site form
            assert (await c.patch("/api/owner/salon", json={"deposit_kes": 100}, headers=cookie | H)).status_code == 200
            assert (await c.patch("/api/owner/salon", json={"deposit_kes": 150}, headers=bearer)).status_code == 200       # an app sends a bearer token
            assert await pool.fetchval("SELECT deposit_kes FROM salons") == 150
            assert (await c.post("/api/owner/signout", headers=bearer)).status_code == 200
            assert (await c.get("/api/owner/me", headers=bearer)).status_code == 401
        await pool.close()
    asyncio.run(run())


@needs_db
def test_bookings_listing_and_cancelling_through_the_api_tells_the_customer():
    async def run():
        pool, out, salon = await build(DSN)
        day = open_day().isoformat()
        async with client() as c:
            await to_pay(c, out, pool)
            await deliver(c, callback("ws_CO_1"))
            hdr = await sign_in(c, salon)
            r = (await c.get("/api/owner/bookings", params={"day": day}, headers=hdr)).json()
            b = r["bookings"][0]
            assert (b["time"], b["service"], b["phone"], b["paid_kes"]) == ("12:00", "Fade", "+254711000001", 200)
            assert (await c.get("/api/owner/summary", headers=hdr)).json()["deposits_today_kes"] == 200
            assert (await c.post("/api/owner/bookings/not-a-uuid/cancel", headers=hdr)).status_code == 404
            done = (await c.post(f"/api/owner/bookings/{b['id']}/cancel", headers=hdr)).json()
            assert done["refund_due_kes"] == 200 and done["refund_to"] == "+254711000001"
            assert "had to cancel your booking" in out.bodies(A)[-1]
            assert (await c.post(f"/api/owner/bookings/{b['id']}/cancel", headers=hdr)).status_code == 404
            assert (await c.get("/api/owner/bookings", params={"day": day}, headers=hdr)).json()["bookings"] == []
        await pool.close()
    asyncio.run(run())


@needs_db
def test_one_salon_can_never_see_or_change_another_salons_data():
    async def run():
        pool, out, salon = await build(DSN)
        other = await second_salon(pool)
        async with client() as c:
            await to_pay(c, out, pool)
            await deliver(c, callback("ws_CO_1"))
            mine, theirs = await sign_in(c, salon), await sign_in(c, other)
            booking = await pool.fetchval("SELECT id::text FROM bookings")
            service = await pool.fetchval("SELECT id::text FROM services WHERE name='Fade'")
            staff = await pool.fetchval("SELECT id::text FROM staff WHERE name='Brian'")
            payment = await pool.fetchval("SELECT id::text FROM payments")
            await pool.execute("UPDATE payments SET status='needs_refund'")
            await c.post("/api/owner/time_off", json={"date": open_day().isoformat()}, headers=mine)
            tid = await pool.fetchval("SELECT id::text FROM time_off")
            assert (await c.post(f"/api/owner/bookings/{booking}/cancel", headers=theirs)).status_code == 404
            assert (await c.patch(f"/api/owner/services/{service}", json={"price_kes": 1}, headers=theirs)).status_code == 404
            assert (await c.patch(f"/api/owner/staff/{staff}", json={"active": False}, headers=theirs)).status_code == 404
            assert (await c.delete(f"/api/owner/time_off/{tid}", headers=theirs)).status_code == 404
            assert (await c.post(f"/api/owner/refunds/{payment}/done", headers=theirs)).status_code == 404
            for path in ("services", "staff", "time_off", "refunds"):
                body = (await c.get(f"/api/owner/{path}", headers=theirs)).json()
                assert "Fade" not in str(body) and "Brian" not in str(body) and payment not in str(body)
            assert (await c.get("/api/owner/bookings", params={"day": open_day().isoformat()}, headers=theirs)).json()["bookings"] == []
            assert await pool.fetchval("SELECT price_kes FROM services WHERE id=$1::uuid", service) == 500
            assert await pool.fetchval("SELECT status FROM bookings") == "confirmed"
        await pool.close()
    asyncio.run(run())


@needs_db
def test_services_and_stylists_can_be_managed_and_customers_see_the_change():
    async def run():
        pool, out, salon = await build(DSN)
        async with client() as c:
            hdr = await sign_in(c, salon)
            r = await c.post("/api/owner/services", json={"name": "Dreadlock retwist", "price_kes": 1500, "duration_min": 90, "aliases": [" Locs ", ""]}, headers=hdr)
            assert r.status_code == 201 and r.json()["aliases"] == ["locs"]
            sid = r.json()["id"]
            assert await pool.fetchval("SELECT count(*) FROM staff_services WHERE service_id=$1::uuid", sid) == 1       # every stylist offers it
            for bad in ({"name": "", "price_kes": 1, "duration_min": 30}, {"name": "x", "price_kes": -5, "duration_min": 30}, {"name": "x", "price_kes": 1, "duration_min": 1}):
                assert (await c.post("/api/owner/services", json=bad, headers=hdr)).status_code == 422
            out.script["prices?"] = {"intent": "price_list"}
            await post(c, payload(A, "prices?", mid="p1"))
            assert "Dreadlock retwist (KES 1500)" in out.bodies(A)[-1]
            assert (await c.patch(f"/api/owner/services/{sid}", json={"price_kes": 1800}, headers=hdr)).json()["price_kes"] == 1800
            assert (await c.patch(f"/api/owner/services/{sid}", json={}, headers=hdr)).status_code == 422
            await c.patch(f"/api/owner/services/{sid}", json={"active": False}, headers=hdr)                             # hidden from customers
            await post(c, payload(A, "prices?", mid="p2"))
            assert "Dreadlock" not in out.bodies(A)[-1]
            m = (await c.post("/api/owner/staff", json={"name": "Wanjiru"}, headers=hdr)).json()
            assert await pool.fetchval("SELECT count(*) FROM staff_services WHERE staff_id=$1::uuid", m["id"]) == 2    # her services = the active ones
            assert (await c.patch(f"/api/owner/staff/{m['id']}", json={"active": False}, headers=hdr)).json()["active"] is False
        await pool.close()
    asyncio.run(run())


@needs_db
def test_hours_deposit_and_time_off_change_what_customers_are_offered():
    async def run():
        pool, out, salon = await build(DSN)
        day = open_day()
        out.script["fade"] = {"intent": "book", "service": "Fade", "date": day.isoformat(), "window": "afternoon"}
        async with client() as c:
            hdr = await sign_in(c, salon)
            assert (await c.patch("/api/owner/salon", json={"open_hours": {"mon": ["19:00", "08:00"]}}, headers=hdr)).status_code == 422
            assert (await c.patch("/api/owner/salon", json={"open_hours": {"xyz": None}}, headers=hdr)).status_code == 422
            assert (await c.patch("/api/owner/salon", json={"deposit_kes": 99999}, headers=hdr)).status_code == 422
            wd = ["mon", "tue", "wed", "thu", "fri", "sat", "sun"][day.weekday()]
            assert (await c.patch("/api/owner/salon", json={"open_hours": {wd: ["14:00", "16:00"]}}, headers=hdr)).status_code == 200
            await post(c, payload(A, "fade", mid="a1"))
            assert "14:00" in out.bodies(A)[-1] and "12:00" not in out.bodies(A)[-1]                                    # new hours are in force
            r = await c.post("/api/owner/time_off", json={"date": day.isoformat(), "start": "14:00", "end": "15:00", "note": "errand"}, headers=hdr)
            assert r.status_code == 201 and r.json()["existing_bookings_in_that_time"] == []
            assert (await c.post("/api/owner/time_off", json={"date": day.isoformat(), "start": "14:00"}, headers=hdr)).status_code == 422
            assert (await c.post("/api/owner/time_off", json={"date": day.isoformat(), "start": "16:00", "end": "15:00"}, headers=hdr)).status_code == 422
            listed = (await c.get("/api/owner/time_off", headers=hdr)).json()
            assert len(listed) == 1 and listed[0]["note"] == "errand"
            await post(c, payload(B, "fade", mid="b1"))
            assert "15:00" in out.bodies(B)[-1] and "14:00" not in out.bodies(B)[-1].split("\n", 1)[1]
            assert (await c.delete(f"/api/owner/time_off/{r.json()['id']}", headers=hdr)).status_code == 200
            await c.patch("/api/owner/salon", json={"deposit_kes": 0}, headers=hdr)
            await post(c, payload(B, reply_id="slot:0", mid="b2"))                                                      # no deposit: confirmed at once
            assert await pool.fetchval("SELECT status FROM bookings") == "confirmed"
        await pool.close()
    asyncio.run(run())


@needs_db
def test_refunds_are_listed_with_their_reason_until_the_owner_marks_them_done():
    async def run():
        pool, out, salon = await build(DSN)
        async with client() as c:
            await to_pay(c, out, pool)
            await pool.execute("UPDATE bookings SET status='cancelled'")
            await deliver(c, callback("ws_CO_1", receipt="RR1"))                                                       # paid for a cancelled booking
            hdr = await sign_in(c, salon)
            assert (await c.get("/api/owner/summary", headers=hdr)).json()["refunds_waiting"] == 1
            [r] = (await c.get("/api/owner/refunds", headers=hdr)).json()
            assert (r["amount_kes"], r["phone"], r["receipt"], r["reason"]) == (200, "+254711000001", "RR1", "cancelled")
            assert (await c.post(f"/api/owner/refunds/{r['id']}/done", headers=hdr)).status_code == 200
            assert (await c.get("/api/owner/refunds", headers=hdr)).json() == []
            assert (await c.post(f"/api/owner/refunds/{r['id']}/done", headers=hdr)).status_code == 404
        await pool.close()
    asyncio.run(run())


@needs_db
def test_the_owner_page_is_served_with_strict_headers_and_nothing_else_is_exposed():
    async def run():
        pool, out, salon = await build(DSN)
        async with client() as c:
            for path in ("/owner", "/owner/login/abcdefghijklmnopqrstuvwxyz"):
                r = await c.get(path)
                assert r.status_code == 200 and "<title>Salon page</title>" in r.text
                assert "default-src 'none'" in r.headers["content-security-policy"] and r.headers["cache-control"] == "no-store"
                assert r.headers["referrer-policy"] == "no-referrer" and r.headers["x-frame-options"] == "DENY"
            assert (await c.get("/owner/static/owner.js")).status_code == 200 and (await c.get("/owner/static/owner.css")).status_code == 200
            assert (await c.get("/owner/static/../main.py")).status_code == 404 and (await c.get("/owner/static/secret.txt")).status_code == 404
            assert "innerHTML" not in (await c.get("/owner/static/owner.js")).text        # customer-supplied names are only ever inserted as text
        await pool.close()
    asyncio.run(run())
