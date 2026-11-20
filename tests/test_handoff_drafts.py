import os
from types import SimpleNamespace

import pytest

from app.agent.client import Call, Step
from app.handoff_drafts import UnsafeDraft, generate_draft
from app.handoff_outbox import StaleCaseVersion, approve_reply
from app.handoffs import create_handoff, record_customer_message
from tests.harness import build

DSN = os.environ.get("TEST_DATABASE_URL")
needs_db = pytest.mark.skipif(not DSN, reason="needs TEST_DATABASE_URL")
OWNER = "254700000020"


async def _case(pool, salon_id, customer="254700000021"):
    await pool.execute("UPDATE salons SET owner_wa_id=$2 WHERE id=$1::uuid", salon_id, OWNER)
    client = await pool.fetchrow(
        "INSERT INTO clients (salon_id,wa_id) VALUES ($1::uuid,$2) RETURNING id::text,wa_id", salon_id, customer)
    return await create_handoff(pool, salon_id, client["id"], client["wa_id"], "complaint", "My haircut was uneven",
                                source_message_id=f"wamid-{customer}", owner_notification="Internal owner alert")


@needs_db
@pytest.mark.asyncio
async def test_draft_calls_model_without_tools_and_owner_can_edit_exact_text_before_approval():
    pool, _, salon_id = await build(DSN)
    case = await _case(pool, salon_id)

    class Model:
        def __init__(self):
            self.tools = None
            self.prompt = None

        async def step(self, system, messages, tools):
            self.tools, self.prompt = tools, messages
            return Step(text="I'm sorry about that. What part should we fix?", model="Muse-Spark-Test")

    model = Model()
    ctx = SimpleNamespace(pool=pool, agent=model)
    with pytest.raises(PermissionError):
        await generate_draft(ctx, salon_id, case["id"], "254700000099", expected_version=1)
    assert model.tools is None, "unauthorized callers must be rejected before any model call"
    draft = await generate_draft(ctx, salon_id, case["id"], OWNER, expected_version=1)
    assert model.tools == []
    assert "Internal owner alert" not in model.prompt[0]["content"]
    assert draft["model_id"] == "Muse-Spark-Test" and draft["based_case_version"] == 1
    assert draft["body"] == "I'm sorry about that. What part should we fix?"
    assert (draft["expires_at"] - draft["created_at"]).total_seconds() == pytest.approx(600, abs=2)

    edited = "I'm sorry about that. Please tell me what you would like us to correct."
    queued = await approve_reply(pool, salon_id, case["id"], OWNER, 1, edited, "draft-approval-1", draft_id=draft["id"])
    row = await pool.fetchrow(
        "SELECT e.body,o.state,o.draft_id::text,d.consumed_at FROM outbound_outbox o "
        "JOIN handoff_events e ON e.id=o.approved_event_id JOIN handoff_drafts d ON d.id=o.draft_id WHERE o.id=$1::uuid",
        queued["id"])
    assert row["body"] == edited and row["state"] == "queued" and row["draft_id"] == draft["id"]
    assert row["consumed_at"] is not None
    with pytest.raises(StaleCaseVersion):
        await approve_reply(pool, salon_id, case["id"], OWNER, 2, edited, "draft-approval-2", draft_id=draft["id"])
    assert await pool.fetchval("SELECT count(*) FROM outbound_outbox") == 1
    await pool.close()


@needs_db
@pytest.mark.asyncio
async def test_model_tool_attempt_and_unsupported_payment_claim_are_rejected_and_not_saved():
    pool, _, salon_id = await build(DSN)
    case = await _case(pool, salon_id)

    class Attempt:
        def __init__(self, step):
            self.result = step

        async def step(self, system, messages, tools):
            assert tools == []
            return self.result

    ctx = SimpleNamespace(pool=pool, agent=Attempt(Step(text="safe", calls=[Call("x", "book", {})], model="Muse-Spark-Test")))
    with pytest.raises(UnsafeDraft, match="tool call"):
        await generate_draft(ctx, salon_id, case["id"], OWNER, 1)
    ctx.agent = Attempt(Step(text="Payment received, thank you.", model="Muse-Spark-Test"))
    with pytest.raises(UnsafeDraft, match="safety checks"):
        await generate_draft(ctx, salon_id, case["id"], OWNER, 1)
    assert await pool.fetchval("SELECT count(*) FROM handoff_drafts") == 0
    await pool.close()


@needs_db
@pytest.mark.asyncio
async def test_new_customer_message_makes_existing_draft_stale_and_blocks_approval():
    pool, _, salon_id = await build(DSN)
    case = await _case(pool, salon_id)

    class Model:
        async def step(self, system, messages, tools):
            assert tools == []
            return Step(text="I'm sorry. Can you share a little more?", model="Muse-Spark-Test")

    draft = await generate_draft(SimpleNamespace(pool=pool, agent=Model()), salon_id, case["id"], OWNER, 1)
    await record_customer_message(pool, salon_id, "254700000021", "Here is another detail", "wamid-new-detail")
    with pytest.raises(StaleCaseVersion):
        await approve_reply(pool, salon_id, case["id"], OWNER, 1, draft["body"], "stale-draft-1", draft_id=draft["id"])
    assert await pool.fetchval("SELECT count(*) FROM outbound_outbox") == 0
    await pool.close()


@needs_db
@pytest.mark.asyncio
async def test_customer_message_arriving_during_model_call_prevents_stale_draft_persistence():
    pool, _, salon_id = await build(DSN)
    case = await _case(pool, salon_id)

    class Model:
        async def step(self, system, messages, tools):
            assert tools == []
            await record_customer_message(pool, salon_id, "254700000021", "Please add this context", "wamid-racing")
            return Step(text="Thanks for explaining. What else should we know?", model="Muse-Spark-Test")

    with pytest.raises(StaleCaseVersion, match="while the reply was being drafted"):
        await generate_draft(SimpleNamespace(pool=pool, agent=Model()), salon_id, case["id"], OWNER, 1)
    assert await pool.fetchval("SELECT count(*) FROM handoff_drafts") == 0
    await pool.close()


@needs_db
@pytest.mark.asyncio
async def test_expired_unapproved_draft_is_removed_by_retention():
    from app.webhook_jobs import purge_expired_data

    pool, _, salon_id = await build(DSN)
    case = await _case(pool, salon_id)

    class Model:
        async def step(self, system, messages, tools):
            return Step(text="I'm sorry. What should we address?", model="Muse-Spark-Test")

    draft = await generate_draft(SimpleNamespace(pool=pool, agent=Model()), salon_id, case["id"], OWNER, 1)
    await pool.execute("UPDATE handoff_drafts SET expires_at=now()-interval '2 days' WHERE id=$1::uuid", draft["id"])
    result = await purge_expired_data(pool)
    assert result["handoff_drafts"] == 1
    assert await pool.fetchval("SELECT count(*) FROM handoff_drafts WHERE id=$1::uuid", draft["id"]) == 0
    await pool.close()


@needs_db
@pytest.mark.asyncio
async def test_owner_cannot_approve_an_expired_draft():
    pool, _, salon_id = await build(DSN)
    case = await _case(pool, salon_id)

    class Model:
        async def step(self, system, messages, tools):
            return Step(text="I'm sorry. What happened?", model="Muse-Spark-Test")

    draft = await generate_draft(SimpleNamespace(pool=pool, agent=Model()), salon_id, case["id"], OWNER, 1)
    await pool.execute("UPDATE handoff_drafts SET expires_at=now()-interval '1 second' WHERE id=$1::uuid", draft["id"])
    with pytest.raises(StaleCaseVersion, match="draft expired"):
        await approve_reply(pool, salon_id, case["id"], OWNER, 1, draft["body"], "expired-approval", draft_id=draft["id"])
    assert await pool.fetchval("SELECT count(*) FROM outbound_outbox") == 0
    await pool.close()
