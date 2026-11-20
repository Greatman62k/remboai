"""Many businesses on one shared bot: registering at the same moment, businesses with similar names, and telling them apart."""
import asyncio
import os
from datetime import datetime
from zoneinfo import ZoneInfo

import pytest

from app import binding, conversation, onboarding_chat as ob, owner
from tests.harness import PID, build, client, open_day, payload, post

DSN = os.environ.get("TEST_DATABASE_URL")
needs_db = pytest.mark.skipif(not DSN, reason="needs TEST_DATABASE_URL")
NBO = ZoneInfo("Africa/Nairobi")


# -- names: what counts as the same business name, and how a business is shown to customers -------------------------------------------
def test_names_that_customers_would_confuse_are_similar():
    from app.names import similar
    for a, b in [("Zuri Cuts", "ZURI CUTS"), ("Zuri Cuts", "Zuri-Cuts"), ("Zuri Cuts", "Zuri Cuts Barbershop"), ("Zuri's Cuts", "Zuri Cuts"),
                 ("Zuri Cuts", "Zuri Cutz"), ("Mama Lucy Salon", "Mama Lucy Beauty"), ("Kinyozi Baraka", "Baraka Barber Shop"), ("Zuri Cuts", "Zuuri Cuts")]:
        assert similar(a, b), (a, b)


def test_different_businesses_are_not_similar():
    from app.names import similar
    for a, b in [("Zuri Cuts", "Amani Cuts"), ("Zuri Cuts", "Zuri Cuts Westlands"), ("Mama Lucy Salon", "Mama Rose Salon"), ("Amani Barbershop", "Amina Barbershop"),
                 ("Fresh Fade", "Zuri Cuts")]:
        assert not similar(a, b), (a, b)


def test_a_business_is_shown_with_its_area():
    from app.names import clean_area, display
    assert display("Zuri Cuts", "Westlands") == "Zuri Cuts (Westlands)"
    assert display("Zuri Cuts", None) == display("Zuri Cuts", "  ") == "Zuri Cuts"
    assert clean_area("  Westlands ") == "Westlands" and clean_area("Kimathi Street, CBD") == "Kimathi Street, CBD"
    for bad in ("", "x", "a" * 41, "call 0712345678", "https://evil.example", "www.evil.com", "@someone", "yes"):
        assert clean_area(bad) is None, bad


# -- invite slugs: many similar names at the same instant ------------------------------------------------------------------------------
@needs_db
def test_many_businesses_with_the_same_name_all_get_a_unique_slug_at_once():
    async def run():
        pool, out, first = await build(DSN)
        ids = [first] + [await pool.fetchval("INSERT INTO salons (name, wa_phone_number_id) VALUES ('Zuri Cuts', $1) RETURNING id::text", PID) for _ in range(23)]
        results = await asyncio.gather(*[binding.ensure_slug(pool, sid, "Zuri Cuts") for sid in ids], return_exceptions=True)
        assert not [r for r in results if isinstance(r, Exception)], f"{sum(isinstance(r, Exception) for r in results)} of 24 failed: {results[0]!r}"
        assert len(set(results)) == 24 and all(r.startswith("zuri-cuts") for r in results)
        again = await asyncio.gather(*[binding.ensure_slug(pool, sid, "anything else") for sid in ids])
        assert list(again) == list(results), "a slug, once given, never changes"
        await pool.close()
    asyncio.run(run())


@needs_db
def test_a_slug_can_be_assigned_inside_the_callers_transaction():
    async def run():
        pool, out, first = await build(DSN)
        sid = await pool.fetchval("INSERT INTO salons (name, wa_phone_number_id) VALUES ('Zuri Cuts', $1) RETURNING id::text", PID)
        assert await binding.ensure_slug(pool, first, "Zuri Cuts") == "zuri-cuts"
        async with pool.acquire() as con:
            with pytest.raises(RuntimeError):
                async with con.transaction():
                    assert await binding.ensure_slug(con, sid, "Zuri Cuts") == "zuri-cuts-2"
                    raise RuntimeError("the caller's transaction fails afterwards")
        assert await pool.fetchval("SELECT slug FROM salons WHERE id=$1::uuid", sid) is None, "the slug must roll back with the transaction"
        await pool.close()
    asyncio.run(run())


# -- go-live: the business, its payment details and its slug are one atomic step --------------------------------------------------------
def _registration(name: str, owner_name: str = "Amina") -> dict:
    return {"step": "confirm_all", "lang": "en", "services_confirmed": True, "duration_defaults_accepted": True, "final_confirmation_pending": True,
            "data": {"salon": name, "kind": "salon", "owners": [owner_name], "services": [{"name": "Fade", "price": 500, "minutes": 45, "explicit": True}],
                     "open": "09:00", "close": "18:00", "deposit": 0}}


async def _salon_names(pool):
    return sorted(r["name"] for r in await pool.fetch("SELECT name FROM salons"))


@needs_db
def test_a_failed_go_live_leaves_no_half_created_business(monkeypatch):
    async def run():
        from app.main import app
        pool, out, first = await build(DSN)
        ctx = app.state.ctx
        async def boom(*a, **k): raise RuntimeError("slug step failed")
        monkeypatch.setattr(binding, "ensure_slug", boom)
        with pytest.raises(RuntimeError):
            await ob._go_live(ctx, PID, "254722000001", _registration("Mama Lucy Salon"))
        assert await _salon_names(pool) == ["Zuri Cuts"], "no business, staff or services may be left behind"
        assert await pool.fetchval("SELECT count(*) FROM staff WHERE name='Amina'") == 0
        await pool.close()
    asyncio.run(run())


@needs_db
def test_businesses_going_live_at_the_same_moment_each_get_their_own_link():
    async def run():
        from app.main import app
        pool, out, first = await build(DSN)
        ctx = app.state.ctx
        senders = [f"2547220000{i:02d}" for i in range(8)]
        results = await asyncio.gather(*[ob._go_live(ctx, PID, s, _registration("Mama Lucy Salon", f"Owner{i}")) for i, s in enumerate(senders)], return_exceptions=True)
        assert not [r for r in results if isinstance(r, Exception)], results
        rows = await pool.fetch("SELECT slug, owner_wa_id FROM salons WHERE name='Mama Lucy Salon'")
        assert len(rows) == 8 and len({r["slug"] for r in rows}) == 8 and all(r["slug"] for r in rows)
        for r in rows:
            assert any(r["slug"] in body for body in out.bodies(r["owner_wa_id"])), f"{r['owner_wa_id']} was not sent its own invite link"
        await pool.close()
    asyncio.run(run())


# -- owners are told when a business with a similar name exists, and can add their area ---------------------------------------------------
@needs_db
def test_both_owners_are_nudged_to_add_an_area_when_names_are_similar():
    async def run():
        from app.main import app
        pool, out, first = await build(DSN)
        ctx = app.state.ctx
        await pool.execute("UPDATE salons SET owner_wa_id='254722000001' WHERE id=$1::uuid", first)
        assert await ob._go_live(ctx, PID, "254722000002", _registration("ZURI CUTS Barbershop"))
        assert any("area" in b.lower() for b in out.bodies("254722000002")), out.bodies("254722000002")
        assert any("area" in b.lower() for b in out.bodies("254722000001")), "the business that was there first is told too"
        # a business that is not similar is left alone
        assert await ob._go_live(ctx, PID, "254722000003", _registration("Amani Fresh Fade"))
        assert not any("similar name" in b.lower() for b in out.bodies("254722000003"))
        await pool.close()
    asyncio.run(run())


async def _owner_says(ctx, salon_id, owner_id, text):
    salon = await conversation._salon_row(ctx, salon_id)
    await owner.handle(ctx, salon, owner_id, text, NBO, datetime.now(NBO))


@needs_db
def test_the_owner_can_set_show_and_clear_the_area():
    async def run():
        from app.main import app
        pool, out, first = await build(DSN)
        ctx = app.state.ctx
        await pool.execute("UPDATE salons SET owner_wa_id='254722000001' WHERE id=$1::uuid", first)
        await _owner_says(ctx, first, "254722000001", "area Westlands")
        assert await pool.fetchval("SELECT area FROM salons WHERE id=$1::uuid", first) == "Westlands"
        assert "Zuri Cuts (Westlands)" in out.bodies("254722000001")[-1]
        assert (await conversation._salon_row(ctx, first))["name"] == "Zuri Cuts (Westlands)", "customers and the cached loader see the label at once"
        await _owner_says(ctx, first, "254722000001", "area 0712345678")
        assert await pool.fetchval("SELECT area FROM salons WHERE id=$1::uuid", first) == "Westlands", "a phone number is not an area"
        await _owner_says(ctx, first, "254722000001", "area clear")
        assert await pool.fetchval("SELECT area FROM salons WHERE id=$1::uuid", first) is None
        await pool.close()
    asyncio.run(run())


# -- end to end: similar businesses on one shared number, customers at the same time, nobody mixed up --------------------------------------
@needs_db
def test_customers_of_similar_named_businesses_reach_the_right_one_and_see_which():
    async def run():
        pool, out, first = await build(DSN)
        await pool.execute("UPDATE salons SET area='Westlands' WHERE id=$1::uuid", first)
        ids = [first]
        for i, area in enumerate(["Kilimani", "Kasarani", None, "Karen"], 1):
            sid = await pool.fetchval("INSERT INTO salons (name, wa_phone_number_id, tg_bot_id, area) VALUES ('Zuri Cuts', $1, 'tg:111', $2) RETURNING id::text", PID, area)
            stf = await pool.fetchval("INSERT INTO staff (salon_id, name) VALUES ($1::uuid,'Sam') RETURNING id::text", sid)
            svc = await pool.fetchval("INSERT INTO services (salon_id,name,aliases,duration_min,price_kes) VALUES ($1::uuid,'Fade','{}',45,$2) RETURNING id::text", sid, 500 + i)
            await pool.execute("INSERT INTO staff_services VALUES ($1::uuid,$2::uuid)", stf, svc)
            await pool.execute("INSERT INTO salon_payment (salon_id, kind, shortcode, party_b) VALUES ($1::uuid,'till','174379','5012345')", sid)
            ids.append(sid)
        slugs = {sid: await binding.ensure_slug(pool, sid, "Zuri Cuts") for sid in ids}
        out.script["book fade"] = {"intent": "book", "service": "fade", "date": open_day().isoformat(), "window": "morning"}
        customers = [(f"2547110{i:02d}{j:02d}", sid) for i, sid in enumerate(ids) for j in range(2)]

        async def run_customer(c, phone, sid):
            for k, body in enumerate([dict(text=f"/start {slugs[sid]}"), dict(text="book fade"), dict(reply_id="slot:0")]):
                assert (await post(c, payload(phone, mid=f"{phone}-{k}", **body))).status_code == 200

        async with client() as c:
            await asyncio.gather(*[run_customer(c, p, s) for p, s in customers])
        label = {sid: await pool.fetchval("SELECT CASE WHEN area IS NULL THEN name ELSE name||' ('||area||')' END FROM salons WHERE id=$1::uuid", sid) for sid in ids}
        for phone, sid in customers:
            assert await pool.fetchval("SELECT salon_id::text FROM sender_salon WHERE sender=$1", phone) == sid
            assert await pool.fetchval("SELECT count(*) FROM clients WHERE wa_id=$1 AND salon_id<>$2::uuid", phone, sid) == 0
            assert await pool.fetchval("SELECT count(*) FROM bookings b JOIN clients c ON c.id=b.client_id WHERE c.wa_id=$1 AND b.salon_id<>$2::uuid", phone, sid) == 0
            assert f"Welcome to {label[sid]}" in out.bodies(phone)[0], (label[sid], out.bodies(phone)[0])
        await pool.close()
    asyncio.run(run())
