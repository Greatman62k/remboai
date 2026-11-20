import os

import pytest

from tests.harness import build

DSN = os.environ.get("TEST_DATABASE_URL")
needs_db = pytest.mark.skipif(not DSN, reason="needs TEST_DATABASE_URL")


@needs_db
@pytest.mark.asyncio
async def test_handoff_creation_and_paused_state_roll_back_together_on_state_failure():
    from app import guard

    pool, out, salon_id = await build(DSN)
    await pool.execute("UPDATE salons SET owner_wa_id='254700000001' WHERE id=$1::uuid", salon_id)
    client = await pool.fetchrow(
        "INSERT INTO clients (salon_id,wa_id,name) VALUES ($1::uuid,'254700000002','Customer') RETURNING id::text,wa_id,name",
        salon_id)
    await pool.execute("""
        CREATE FUNCTION test_reject_handoff_conversation() RETURNS trigger LANGUAGE plpgsql AS $$
        BEGIN
          IF NEW.state->>'state' = 'handoff' THEN RAISE EXCEPTION 'injected handoff state failure'; END IF;
          RETURN NEW;
        END $$;
        CREATE TRIGGER test_reject_handoff_conversation BEFORE INSERT OR UPDATE ON conversations
        FOR EACH ROW EXECUTE FUNCTION test_reject_handoff_conversation();
    """)
    salon = dict(await pool.fetchrow("SELECT id::text,owner_wa_id,wa_phone_number_id FROM salons WHERE id=$1::uuid", salon_id))
    try:
        with pytest.raises(Exception, match="injected handoff state failure"):
            await guard.hand_off(type("Ctx", (), {"pool": pool})(), salon, dict(client), "en", "complaint", "Please help")
    finally:
        await pool.execute("DROP TRIGGER test_reject_handoff_conversation ON conversations")
        await pool.execute("DROP FUNCTION test_reject_handoff_conversation()")
    assert await pool.fetchval("SELECT count(*) FROM handoffs") == 0
    assert await pool.fetchval("SELECT count(*) FROM handoff_events") == 0
    assert await pool.fetchval("SELECT count(*) FROM conversations WHERE wa_id=$1", client["wa_id"]) == 0
    assert out.sent == []
    await pool.close()


@needs_db
@pytest.mark.asyncio
async def test_customer_messages_are_deduplicated_and_bump_the_open_case_version():
    from app.handoffs import create_handoff, get_handoff_context, list_open_handoffs, record_customer_message

    pool, _, salon_id = await build(DSN)
    await pool.execute("UPDATE salons SET owner_wa_id='254700000007' WHERE id=$1::uuid", salon_id)
    client = await pool.fetchrow(
        "INSERT INTO clients (salon_id,wa_id) VALUES ($1::uuid,'254700000003') RETURNING id::text,wa_id", salon_id)
    case = await create_handoff(pool, salon_id, client["id"], client["wa_id"], "complaint", "First message",
                                source_message_id="wamid.first", owner_notification="Internal owner alert")
    assert case["version"] == 1
    update = await record_customer_message(pool, salon_id, client["wa_id"], "More context", "wamid.second")
    assert update["version"] == 2 and update["duplicate"] is False
    repeated = await record_customer_message(pool, salon_id, client["wa_id"], "More context", "wamid.second")
    assert repeated["version"] == 2 and repeated["duplicate"] is True
    events = await pool.fetch(
        "SELECT sequence,event_type,actor_type,visibility,body FROM handoff_events WHERE handoff_id=$1::uuid ORDER BY sequence", case["id"])
    assert [(e["event_type"], e["visibility"]) for e in events] == [
        ("customer_message", "customer_visible"), ("case_opened", "owner_internal"),
        ("owner_notification", "owner_internal"), ("customer_message", "customer_visible")]
    assert events[2]["body"] == "Internal owner alert"
    assert await pool.fetchval("SELECT count(*) FROM handoff_events WHERE provider_message_id='wamid.second'") == 1
    inbox = await list_open_handoffs(pool, salon_id, "254700000007")
    assert len(inbox) == 1 and inbox[0]["id"] == case["id"]
    context = await get_handoff_context(pool, salon_id, case["id"], "254700000007")
    assert [m["body"] for m in context["messages"]] == ["First message", "More context"]
    assert all(m["actor_type"] == "customer" for m in context["messages"])
    assert "Internal owner alert" not in str(context)
    with pytest.raises(PermissionError):
        await get_handoff_context(pool, salon_id, case["id"], "254700000099")
    await pool.close()


@needs_db
@pytest.mark.asyncio
async def test_only_current_salon_owner_can_resolve_and_payment_review_is_unchanged():
    from app.handoffs import create_handoff, resolve_handoff

    pool, _, salon_id = await build(DSN)
    await pool.execute("UPDATE salons SET owner_wa_id='254700000004' WHERE id=$1::uuid", salon_id)
    client = await pool.fetchrow(
        "INSERT INTO clients (salon_id,wa_id) VALUES ($1::uuid,'254700000005') RETURNING id::text,wa_id", salon_id)
    case = await create_handoff(pool, salon_id, client["id"], client["wa_id"], "payment_dispute", "I paid",
                                source_message_id="wamid.payment")
    staff = await pool.fetchval("SELECT id::text FROM staff WHERE salon_id=$1::uuid LIMIT 1", salon_id)
    service = await pool.fetchval("SELECT id::text FROM services WHERE salon_id=$1::uuid LIMIT 1", salon_id)
    booking = await pool.fetchval(
        "INSERT INTO bookings (salon_id,staff_id,service_id,client_id,starts_at,ends_at,status) "
        "VALUES ($1::uuid,$2::uuid,$3::uuid,$4::uuid,now()+interval '1 day',now()+interval '1 day 45 minutes','confirmed') RETURNING id::text",
        salon_id, staff, service, client["id"])
    payment = await pool.fetchval(
        "INSERT INTO payments (booking_id,salon_id,phone,amount_kes,status) VALUES ($1::uuid,$2::uuid,$3,200,'review') RETURNING id::text",
        booking, salon_id, client["wa_id"])
    await pool.execute(
        "INSERT INTO payment_reviews (payment_id,reason,status,expected_amount,received_amount) "
        "VALUES ($1::uuid,'payment_unresolved','pending_review',200,200)", payment)

    other_salon = await pool.fetchval("INSERT INTO salons (name,wa_phone_number_id) VALUES ('Other','PID2') RETURNING id::text")
    with pytest.raises(PermissionError):
        await resolve_handoff(pool, other_salon, case["id"], "254700000004", "owner action")
    with pytest.raises(PermissionError):
        await resolve_handoff(pool, salon_id, case["id"], "254700000099", "owner action")
    assert await pool.fetchval("SELECT status FROM handoffs WHERE id=$1::uuid", case["id"]) == "open"

    resolved = await resolve_handoff(pool, salon_id, case["id"], "254700000004", "owner reviewed the complaint")
    assert resolved["status"] == "resolved" and resolved["version"] == 2
    row = await pool.fetchrow("SELECT status,resolved_by,resolution_reason FROM handoffs WHERE id=$1::uuid", case["id"])
    assert tuple(row) == ("resolved", "254700000004", "owner reviewed the complaint")
    assert await pool.fetchval("SELECT status FROM payments WHERE id=$1::uuid", payment) == "review"
    assert await pool.fetchval("SELECT status FROM payment_reviews WHERE payment_id=$1::uuid", payment) == "pending_review"
    await pool.close()


@needs_db
@pytest.mark.asyncio
async def test_salon_deletion_is_blocked_until_open_handoffs_are_resolved():
    from app.handoffs import create_handoff
    from app.lifecycle import OpenHandoffsError, delete_salon

    pool, _, salon_id = await build(DSN)
    client = await pool.fetchrow(
        "INSERT INTO clients (salon_id,wa_id) VALUES ($1::uuid,'254700000006') RETURNING id::text,wa_id", salon_id)
    await create_handoff(pool, salon_id, client["id"], client["wa_id"], "human_request", "Please call me",
                         source_message_id="wamid.delete")
    with pytest.raises(OpenHandoffsError):
        await delete_salon(pool, salon_id)
    assert await pool.fetchval("SELECT count(*) FROM salons WHERE id=$1::uuid", salon_id) == 1
    assert await pool.fetchval("SELECT count(*) FROM handoffs WHERE status='open'") == 1
    await pool.close()
