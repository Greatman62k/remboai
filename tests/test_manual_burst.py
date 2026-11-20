"""Many customers asking for manual-payment availability and confirmation at once, and one owner answering them."""
import asyncio
import os
from datetime import datetime, timedelta, timezone

import pytest

from app import bookings, guard
from app.ratelimit import RateLimiter
from tests.harness import build, client, open_day, payload, post

DSN = os.environ.get("TEST_DATABASE_URL")
needs_db = pytest.mark.skipif(not DSN, reason="needs TEST_DATABASE_URL")


async def _manual_shop(pool, owner: str):
    _, _, salon = None, None, None
    return salon


async def _shop(owner: str):
    pool, out, salon = await build(DSN)
    await pool.execute("UPDATE salons SET owner_wa_id=$2 WHERE id=$1::uuid", salon, owner)
    await pool.execute("UPDATE salon_payment SET manual=true, kind='till', party_b='5012345', shortcode=NULL WHERE salon_id=$1::uuid", salon)
    return pool, out, salon


async def _pending_check(pool, salon, i: int) -> str:
    """A customer whose booking is waiting for the owner's availability answer."""
    cid = await pool.fetchval("INSERT INTO clients (salon_id, wa_id) VALUES ($1::uuid,$2) RETURNING id::text", salon, f"2547110{i:04d}")
    sid = await pool.fetchval("SELECT id::text FROM services WHERE name='Fade'")
    stid = await pool.fetchval("SELECT id::text FROM staff LIMIT 1")
    start = datetime.now(timezone.utc) + timedelta(days=2, hours=i)
    b = await bookings.hold_slot(pool, salon_id=salon, staff_id=stid, service_id=sid, client_id=cid, starts_at=start, ends_at=start + timedelta(minutes=45), minutes=5)
    await pool.execute("UPDATE bookings SET owner_check_status='pending', owner_check_expires_at=now()+interval '5 minutes' WHERE id=$1::uuid", b)
    return b


def _button_ids(out, wa_id):
    ids = []
    for m in out.to(wa_id):
        for b in m.get("interactive", {}).get("action", {}).get("buttons", []):
            ids.append(b["reply"]["id"])
    return ids


# -- the owner is the bottleneck, so the owner is not rate limited like a spammer ----------------------------------------------------------
@needs_db
def test_an_owner_can_answer_a_long_queue_in_a_minute():
    async def run():
        owner = "254799000001"
        pool, out, salon = await _shop(owner)
        ids = [await _pending_check(pool, salon, i) for i in range(30)]
        async with client() as c:
            for k, b in enumerate(ids):
                await post(c, payload(owner, reply_id=f"availability:yes:{b}", mid=f"t{k}"))
        assert await pool.fetchval("SELECT count(*) FROM bookings WHERE owner_check_status='approved'") == 30, "every tap must take effect"
        assert await pool.fetchval("SELECT count(*) FROM abuse_strikes WHERE sender=$1", owner) == 0, "an owner doing their job earns no abuse strikes"
        assert not await guard.blocked(pool, owner, 3)
        await pool.close()
    asyncio.run(run())


@needs_db
def test_customers_are_still_rate_limited():
    async def run():
        pool, out, salon = await _shop("254799000002")
        customer = "254711999001"
        async with client() as c:
            for k in range(40):
                await post(c, payload(customer, text="hi", mid=f"h{k}"))
        assert len(out.bodies(customer)) < 40, "a customer sending 40 messages in a minute must still be throttled"
        await pool.close()
    asyncio.run(run())


@needs_db
def test_an_owner_is_throttled_too_but_far_later(monkeypatch):
    async def run():
        from app.main import app
        owner = "254799000003"
        pool, out, salon = await _shop(owner)
        monkeypatch.setattr(app.state.ctx.settings, "owner_rate_factor", 2)
        async with client() as c:
            for k in range(60):
                await post(c, payload(owner, text="help", mid=f"o{k}"))
        answered = len(out.bodies(owner))
        assert 30 < answered <= 45, f"limit is 20 a minute times 2 for an owner, so about 40 answers, got {answered}"
        await pool.close()
    asyncio.run(run())


class _FakeRedis:
    def __init__(self): self.n = {}
    async def eval(self, script, nkeys, key, ttl):
        self.n[key] = self.n.get(key, 0) + 1
        return self.n[key]


def test_the_redis_limiter_gives_an_owner_a_higher_ceiling():
    async def run():
        limiter = RateLimiter(_FakeRedis(), None)                         # burst 10 a minute, 300 a day
        results = [await limiter.check_sender("global", "customer") for _ in range(12)]
        assert results[:10] == ["ok"] * 10 and results[10] == "notify" and results[11] == "drop"
        owner = [await limiter.check_sender("global", "owner", factor=10) for _ in range(100)]
        assert set(owner) == {"ok"}, "an owner working a queue stays under ten times the customer limit"
        assert [await limiter.check_sender("global", "owner", factor=10) for _ in range(2)] == ["notify", "drop"]
    asyncio.run(run())


# -- the owner is never asked about more bookings than one person can answer ----------------------------------------------------------------
@needs_db
def test_only_so_many_availability_questions_reach_the_owner_at_once(monkeypatch):
    async def run():
        from app.main import app
        owner = "254799000004"
        pool, out, salon = await _shop(owner)
        monkeypatch.setattr(app.state.ctx.settings, "manual_owner_check_max_pending", 3)
        day = open_day()
        custs = [f"2547110{i:04d}" for i in range(6)]
        for i in range(6):
            out.script[f"book {i}"] = {"intent": "book", "service": "fade", "date": day.isoformat(), "time": f"{9 + i:02d}:00"}
        async with client() as c:
            await asyncio.gather(*[_pick(c, i, p) for i, p in enumerate(custs)])
        pending = await pool.fetchval("SELECT count(*) FROM bookings WHERE owner_check_status='pending'")
        assert pending == 3, f"{pending} availability questions are open"
        assert len(out.bodies(owner)) == 3, "and the owner received exactly those three"
        told = [p for p in custs if any("several booking requests" in b for b in out.bodies(p))]
        assert len(told) == 3, "the rest were told the owner is busy"
        assert await pool.fetchval("SELECT count(*) FROM bookings WHERE status='held'") == 3, "and took no slot"
        # once the owner answers one, the next customer gets through
        first = await pool.fetchval("SELECT id::text FROM bookings WHERE owner_check_status='pending' ORDER BY created_at LIMIT 1")
        # The concurrent requests may have left 09:00 occupied. Use a separate
        # valid slot so this assertion measures the owner queue cap, not slot contention.
        out.script["book 0"] = {"intent": "book", "service": "fade", "date": day.isoformat(), "time": "17:00"}
        async with client() as c:
            await post(c, payload(owner, reply_id=f"availability:no:{first}", mid="no1"))
            await _pick(c, 0, told[0])
        assert await pool.fetchval("SELECT count(*) FROM bookings WHERE owner_check_status='pending'") == 3
        await pool.close()
    asyncio.run(run())


async def _pick(c, i, phone):
    await post(c, payload(phone, text=f"book {i}", mid=f"{phone}-a{i}"))
    await post(c, payload(phone, reply_id="slot:0", mid=f"{phone}-b{i}"))


# -- "pending payments" brings back everything waiting for the owner, in a bounded list -------------------------------------------------------
@needs_db
def test_pending_lists_availability_requests_first_and_in_a_bounded_list():
    async def run():
        owner = "254799000005"
        pool, out, salon = await _shop(owner)
        for i in range(14):
            await _pending_check(pool, salon, i)
        before = len(out.bodies(owner))
        async with client() as c:
            await post(c, payload(owner, text="pending payments", mid="list"))
        sent = out.bodies(owner)[before:]
        buttons = [b for b in _button_ids(out, owner) if b.startswith("availability:yes:")]
        assert len(buttons) == 10, f"a page of ten, not all {14}"
        assert sent[0].startswith("You have 14"), sent[0]
        assert "4 more" in sent[-1], sent[-1]
        assert len(sent) == 12, "a summary, ten questions and a note about the rest"
        await pool.close()
    asyncio.run(run())


@needs_db
def test_pending_with_nothing_waiting_still_says_so():
    async def run():
        owner = "254799000006"
        pool, out, salon = await _shop(owner)
        async with client() as c:
            await post(c, payload(owner, text="pending payments", mid="none"))
        assert "no manual payment claims" in out.bodies(owner)[-1] or "nothing" in out.bodies(owner)[-1].lower()
        await pool.close()
    asyncio.run(run())


# -- two customers using the same payment code at the same instant ---------------------------------------------------------------------------
@needs_db
def test_the_same_code_claimed_twice_at_once_is_one_claim_and_a_clear_answer():
    async def run():
        owner = "254799000007"
        pool, out, salon = await _shop(owner)
        a, b = [await _pending_check(pool, salon, i) for i in range(2)]
        async with client() as c:
            for bid in (a, b):
                await post(c, payload(owner, reply_id=f"availability:yes:{bid}", mid=f"av{bid[:6]}"))
            phones = [await pool.fetchval("SELECT cl.wa_id FROM bookings b JOIN clients cl ON cl.id=b.client_id WHERE b.id=$1::uuid", x) for x in (a, b)]
            before = len(out.bodies(owner))
            r = await asyncio.gather(*[post(c, payload(p, text="I paid KES 200 code SAMECODE01", mid=f"claim-{k}")) for k, p in enumerate(phones)])
        assert [x.status_code for x in r] == [200, 200]
        assert await pool.fetchval("SELECT count(*) FROM payments WHERE mpesa_receipt='SAMECODE01'") == 1
        assert len(out.bodies(owner)) - before == 1, "the owner is asked about one claim, not two"
        told = [p for p in phones if any("already used" in x.lower() for x in out.bodies(p)[-2:])]
        assert len(told) == 1, "the other customer is told the code was already used"
        assert await pool.fetchval("SELECT count(*) FROM webhook_jobs WHERE status NOT IN ('done','processed','completed')") == 0, "no job is left failing and retrying"
        await pool.close()
    asyncio.run(run())
