import pytest
import re
from app.onboarding import normalize_phone, normalize_kenyan_mobile, csrf_for_token, _token_hash, _PHONE_RE
from app.config import Settings

def test_normalize_phone():
    assert normalize_phone("+254712345678", "x") == "254712345678"
    assert normalize_phone("254 712 345 678", "x") == "254712345678"
    assert normalize_phone("+254-712-345-678", "x") == "254712345678"
    try:
        normalize_phone("123", "x")
        assert False
    except Exception:
        pass

def test_normalize_kenyan_mobile():
    assert normalize_kenyan_mobile("712345678", "x") == "254712345678"
    assert normalize_kenyan_mobile("112345678", "x") == "254112345678"
    try:
        normalize_kenyan_mobile("612345678", "x")
        assert False
    except Exception:
        pass

def test_csrf():
    token = "a"*32
    c1 = csrf_for_token(token)
    c2 = csrf_for_token(token)
    assert c1 == c2
    assert len(c1) == 64
    assert csrf_for_token("b"*32) != c1

def test_token_hash():
    h = _token_hash("test-token-123")
    assert len(h) == 64
    assert re.fullmatch(r"[0-9a-f]{64}", h)

@pytest.mark.asyncio
async def test_create_and_get_invite():
    import os
    dsn = os.environ.get("TEST_DATABASE_URL")
    if not dsn:
        pytest.skip("needs TEST_DATABASE_URL")
    from app.db import make_pool, migrate
    from app.onboarding import create_setup_invite, get_setup_invite
    pool = await make_pool(dsn)
    await pool.execute("DROP SCHEMA public CASCADE; CREATE SCHEMA public;")
    await migrate(pool)
    token, invite = await create_setup_invite(pool, "PID123", "254712345678", hours=1)
    assert len(token) >= 32
    fetched = await get_setup_invite(pool, token)
    assert fetched is not None
    assert fetched.wa_business_number == "254712345678"
    # duplicate should conflict
    try:
        await create_setup_invite(pool, "PID123", "254712345679", hours=1)
        assert False
    except Exception as e:
        assert "already" in str(e).lower()
    # invalid token returns None
    assert await get_setup_invite(pool, "invalid-token") is None
    await pool.close()

@pytest.mark.asyncio
async def test_complete_invite_creates_salon():
    import os
    dsn = os.environ.get("TEST_DATABASE_URL")
    if not dsn:
        pytest.skip("needs TEST_DATABASE_URL")
    from app.db import make_pool, migrate
    from app.onboarding import create_setup_invite, complete_setup_invite, parse_setup_form
    pool = await make_pool(dsn)
    await pool.execute("DROP SCHEMA public CASCADE; CREATE SCHEMA public;")
    await migrate(pool)
    token, invite = await create_setup_invite(pool, "PID999", "254700000001", hours=1)
    form = {
        "name": "Test Salon",
        "deposit_kes": "200",
        "opening_time": "09:00",
        "closing_time": "18:00",
        "sunday_open": "on",
        "owner_wa_local": "712345678",
        "staff_0": "Amina",
        "staff_1": "Brian",
        "service_name_0": "Fade",
        "service_category_0": "barbering",
        "service_duration_0": "30",
        "service_price_0": "500",
        "service_name_1": "Braids",
        "service_category_1": "braids",
        "service_duration_1": "60",
        "service_price_1": "1000",
    }
    setup = parse_setup_form(form, invite.wa_business_number)
    salon = await complete_setup_invite(pool, token, setup)
    assert salon["name"] == "Test Salon"
    assert salon["wa_business_number"] == "254700000001"
    # invite should be deleted
    assert await pool.fetchval("SELECT 1 FROM salon_setup_invites WHERE token_hash=$1", __import__("hashlib").sha256(token.encode()).hexdigest()) is None
    # salon exists
    assert await pool.fetchval("SELECT count(*) FROM salons WHERE wa_phone_number_id='PID999'") == 1
    assert await pool.fetchval("SELECT count(*) FROM staff WHERE salon_id=$1::uuid", salon["id"]) == 2
    assert await pool.fetchval("SELECT count(*) FROM services WHERE salon_id=$1::uuid", salon["id"]) == 2
    await pool.close()
