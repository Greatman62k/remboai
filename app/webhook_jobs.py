from __future__ import annotations

import asyncio
import logging
import uuid
from collections import Counter
from contextlib import asynccontextmanager
from datetime import datetime

from . import poolstate
from .channels.whatsapp import mask
from .mpesa import parse_callback
from .payments import settle_with_verification

log = logging.getLogger("rembo.webhooks")
MAX_ATTEMPTS = 8
LEASE_SECONDS = 60
HEARTBEAT_SECONDS = 15
_QUEUE_LOCK = 1383000578

RESERVED_CONNECTIONS = 2


def _payload_sent_at(payload: dict) -> datetime | None:
    value = payload.get("sent_at")
    if isinstance(value, datetime):
        return value
    if not isinstance(value, str) or not value:
        return None
    try:
        return datetime.fromisoformat(value.replace("Z", "+00:00"))
    except ValueError:
        return None


def job_slots(pool) -> asyncio.Semaphore:
    st = poolstate.state(pool)
    if "slots" not in st:
        st["slots"] = asyncio.Semaphore(max(1, pool.get_max_size() - RESERVED_CONNECTIONS))
    return st["slots"]


def ordering_key(provider: str, payload: dict) -> str | None:
    """Messages from one sender on one number are handled in the order they arrived. Other jobs (M-Pesa callbacks) have no order."""
    if provider not in ("whatsapp", "telegram"):
        return None
    who = payload.get("wa_id") or payload.get("sender")
    return f"{provider}:{payload.get('phone_number_id') or ''}:{who}" if who else None


class QueueCapacityError(Exception):
    pass


class PermanentJobError(Exception):
    pass


async def _heartbeat_once(pool, job_id: int, claim_token: uuid.UUID) -> bool:
    """Renew only the lease issued to this claimant. False means the lease is no longer ours."""
    try:
        result = await pool.execute(
            "UPDATE webhook_jobs SET locked_until=now()+($3 * interval '1 second') "
            "WHERE id=$1 AND status='processing' AND claim_token=$2",
            job_id, claim_token, LEASE_SECONDS)
    except Exception:
        log.warning("could not renew the lease on job %s", job_id)
        return False
    if not result.endswith("1"):
        log.warning("lost the lease on job %s", job_id)
        return False
    return True


async def _heartbeat(pool, job_id: int, claim_token: uuid.UUID, holder: asyncio.Task | None = None) -> None:
    """Keep the lease on a job that is still running, so a slow job (a model call, a Daraja query) is not claimed a second time."""
    while True:
        await asyncio.sleep(HEARTBEAT_SECONDS)
        if not await _heartbeat_once(pool, job_id, claim_token):
            if holder is not None:
                holder.cancel()
            return


async def enqueue_jobs(pool, provider: str, events: list[tuple[str, dict]], *, max_pending: int = 10000, max_pending_per_sender: int = 100) -> None:
    if not events:
        return
    unique = dict(events)
    async with pool.acquire() as conn, conn.transaction():
        await conn.fetchval("SELECT pg_advisory_xact_lock($1::bigint)", _QUEUE_LOCK)
        existing = await conn.fetch("SELECT event_id FROM webhook_jobs WHERE provider=$1 AND event_id=ANY($2::text[])", provider, list(unique))
        existing_ids = {r["event_id"] for r in existing}
        pending = {eid: payload for eid, payload in unique.items() if eid not in existing_ids}
        if not pending:
            return
        pending_count = await conn.fetchval("SELECT count(*) FROM webhook_jobs WHERE status IN ('pending','processing')")
        if int(pending_count or 0) + len(pending) > max(1, int(max_pending)):
            raise QueueCapacityError("global webhook queue capacity reached")
        if provider in ("whatsapp", "telegram"):
            limit = max(1, int(max_pending_per_sender))
            batch = Counter((p.get("wa_id") or p.get("sender") or "") for p in pending.values())
            for sender, n in batch.items():
                if not sender:
                    continue
                c = await conn.fetchval(
                    "SELECT count(*) FROM webhook_jobs WHERE provider=$1 AND status IN ('pending','processing') "
                    "AND (payload->>'wa_id'=$2 OR payload->>'sender'=$2)", provider, str(sender))
                if int(c or 0) + n > limit:
                    raise QueueCapacityError(f"per-sender queue capacity reached for {sender}")
        for eid, payload in pending.items():
            await conn.execute("INSERT INTO webhook_jobs (provider, event_id, payload, ordering_key) VALUES ($1,$2,$3,$4)", provider, eid, payload, ordering_key(provider, payload))


async def claim_next(pool):
    async with pool.acquire() as conn, conn.transaction():
        row = await conn.fetchrow(
            "SELECT j.* FROM webhook_jobs j WHERE j.status IN ('pending','processing') AND (j.locked_until IS NULL OR j.locked_until < now()) AND j.available_at <= now() "
            "AND (j.ordering_key IS NULL OR NOT EXISTS (SELECT 1 FROM webhook_jobs e WHERE e.ordering_key = j.ordering_key AND e.id < j.id AND e.status IN ('pending','processing'))) "
            "ORDER BY j.available_at, j.id LIMIT 1 FOR UPDATE OF j SKIP LOCKED"
        )
        if row is None:
            return None
        job = dict(row)
        claim_token = uuid.uuid4()
        await conn.execute(
            "UPDATE webhook_jobs SET status='processing', attempts=attempts+1, claim_token=$3, "
            "locked_until=now()+($2 * interval '1 second'), updated_at=now() WHERE id=$1",
            job["id"], LEASE_SECONDS, claim_token)
        job["attempts"] += 1
        job["claim_token"] = claim_token
        return job


async def complete_job(pool, job_id: int, claim_token: uuid.UUID) -> bool:
    result = await pool.execute(
        "UPDATE webhook_jobs SET status='processed', payload='{}'::jsonb, locked_until=NULL, claim_token=NULL, "
        "last_error=NULL, completed_at=now(), updated_at=now() "
        "WHERE id=$1 AND status='processing' AND claim_token=$2", job_id, claim_token)
    return result.endswith("1")


async def dead_job(pool, job_id: int, error: str, claim_token: uuid.UUID) -> bool:
    result = await pool.execute(
        "UPDATE webhook_jobs SET status='dead', locked_until=NULL, claim_token=NULL, last_error=$3, "
        "completed_at=now(), updated_at=now() "
        "WHERE id=$1 AND status='processing' AND claim_token=$2", job_id, claim_token, error[:2000])
    return result.endswith("1")


async def retry_job(pool, job: dict, error: Exception) -> None:
    if job["attempts"] >= MAX_ATTEMPTS:
        await dead_job(pool, job["id"], f"{error.__class__.__name__}: {error}", job["claim_token"])
        return
    delay = min(300, 2 ** job["attempts"])
    await pool.execute(
        "UPDATE webhook_jobs SET status='pending', locked_until=NULL, claim_token=NULL, last_error=$3, "
        "available_at=now()+($4 * interval '1 second'), updated_at=now() "
        "WHERE id=$1 AND status='processing' AND claim_token=$2",
        job["id"], job["claim_token"], f"{error.__class__.__name__}: {error}"[:2000], delay)


async def requeue_dead_job(pool, job_id: int) -> bool:
    r = await pool.execute("UPDATE webhook_jobs SET status='pending', attempts=0, locked_until=NULL, claim_token=NULL, last_error=NULL, completed_at=NULL, available_at=now(), updated_at=now() WHERE id=$1 AND status='dead'", job_id)
    return r.endswith("1")


async def list_dead_jobs(pool):
    rows = await pool.fetch("SELECT id, provider, event_id, payload, attempts, last_error, created_at FROM webhook_jobs WHERE status='dead' ORDER BY created_at DESC LIMIT 100")
    return [dict(r) for r in rows]


async def _process_claimed_job(ctx, job: dict) -> None:
    if job["provider"] == "whatsapp":
        from .inbound import inbound as handle_inbound
        from .channels.whatsapp import Message
        payload = job["payload"] if isinstance(job["payload"], dict) else __import__("json").loads(job["payload"])
        msg = Message(
            id=payload.get("id") or payload.get("wa_message_id") or job["event_id"],
            sender=payload.get("sender") or payload.get("wa_id") or "",
            phone_number_id=payload.get("phone_number_id") or payload.get("wa_phone_number_id") or "",
            kind=payload.get("kind") or "text",
            text=payload.get("text") or "",
            media_id=payload.get("media_id"),
            name=payload.get("name"),
            reply_id=payload.get("reply_id"),
            sent_at=_payload_sent_at(payload),
        )
        if not msg.sender or not msg.id:
            raise PermanentJobError("invalid whatsapp payload")
        await handle_inbound(ctx, msg)
        await complete_job(ctx.pool, job["id"], job["claim_token"])
        return
    if job["provider"] == "telegram":
        from .inbound import inbound as handle_inbound
        from .channels.whatsapp import Message
        payload = job["payload"] if isinstance(job["payload"], dict) else __import__("json").loads(job["payload"])
        if not isinstance(payload, dict) or not payload.get("sender"):
            raise PermanentJobError("invalid telegram payload")
        msg = Message(
            id=payload.get("id") or job["event_id"],
            sender=payload.get("sender") or "",
            phone_number_id=payload.get("phone_number_id") or "",
            kind=payload.get("kind") or "text",
            text=payload.get("text") or "",
            media_id=payload.get("media_id"),
            name=payload.get("name"),
            reply_id=payload.get("reply_id"),
            sent_at=_payload_sent_at(payload),
        )
        await handle_inbound(ctx, msg)
        await complete_job(ctx.pool, job["id"], job["claim_token"])
        return
    if job["provider"] == "mpesa":
        payload = job["payload"] if isinstance(job["payload"], dict) else __import__("json").loads(job["payload"])
        cb = parse_callback(payload)
        if cb is None:
            raise PermanentJobError("invalid M-Pesa callback payload")
        outcome = await settle_with_verification(ctx, cb)
        if outcome == "unverified":
            if job["attempts"] < MAX_ATTEMPTS:
                raise RuntimeError(f"cannot verify {cb.checkout_request_id} with Daraja yet")
            log.error("PAYMENT NOT YET VERIFIED: checkout=%s receipt=%s amount=%s; left for the sweeper", cb.checkout_request_id, cb.receipt, cb.amount)
        elif outcome == "unknown":
            if job["attempts"] < MAX_ATTEMPTS:
                raise RuntimeError(f"unknown CheckoutRequestID {cb.checkout_request_id}")
            if cb.code == 0:
                await ctx.pool.execute(
                    "INSERT INTO unmatched_payments (checkout_request_id, merchant_request_id, result_code, amount, phone, mpesa_receipt, raw) VALUES ($1,$2,$3,$4,$5,$6,$7::jsonb) ON CONFLICT DO NOTHING",
                    cb.checkout_request_id, None, cb.code, cb.amount, cb.phone, cb.receipt, payload,
                )
                log.error("UNMATCHED PAYMENT recorded: checkout=%s amount=%s receipt=%s phone=%s", cb.checkout_request_id, cb.amount, cb.receipt, mask(cb.phone or ""))
        await complete_job(ctx.pool, job["id"], job["claim_token"])
        return
    raise PermanentJobError(f"unsupported provider {job['provider']}")


async def process_job(ctx, job: dict) -> bool:
    """Run one claimed job. Raises if it fails (the caller decides between retry and dead; see run_claimed)."""
    beat = asyncio.create_task(_heartbeat(ctx.pool, job["id"], job["claim_token"], asyncio.current_task()))
    try:
        async with job_slots(ctx.pool):
            await _process_claimed_job(ctx, job)
        return True
    finally:
        beat.cancel()
        await asyncio.gather(beat, return_exceptions=True)


async def run_claimed(ctx, job: dict) -> None:
    """process_job plus the failure policy, shared by the background workers and the in-request drain."""
    try:
        await process_job(ctx, job)
    except PermanentJobError as e:
        await dead_job(ctx.pool, job["id"], str(e), job["claim_token"])
        log.error("Webhook job %s/%s rejected: %s", job["provider"], job["event_id"], e)
    except Exception as e:
        log.warning("webhook job %s/%s failed on attempt %s: %s", job["provider"], job["event_id"], job["attempts"], e.__class__.__name__)
        await retry_job(ctx.pool, job, e)


async def webhook_worker(ctx) -> None:
    while True:
        try:
            job = await claim_next(ctx.pool)
            if job is None:
                await asyncio.sleep(0.5)
                continue
            await run_claimed(ctx, job)
        except asyncio.CancelledError:
            raise
        except Exception:
            log.exception("webhook worker iteration failed")
            await asyncio.sleep(1)


async def purge_expired_data(pool, webhook_days: int = 30, inbound_days: int = 90, agent_days: int = 14,
                             handoff_days: int = 30, callback_days: int = 90,
                             unmatched_raw_days: int = 90) -> dict:
    w = await pool.fetchval("WITH deleted AS (DELETE FROM webhook_jobs WHERE status IN ('processed','dead') AND COALESCE(completed_at, updated_at) < now() - ($1 * interval '1 day') RETURNING 1) SELECT count(*) FROM deleted", webhook_days)
    i = await pool.fetchval("WITH deleted AS (DELETE FROM inbound_messages WHERE received_at < now() - ($1 * interval '1 day') RETURNING 1) SELECT count(*) FROM deleted", inbound_days)
    o = await pool.fetchval("WITH deleted AS (DELETE FROM outbound_log WHERE sent_at < now() - ($1 * interval '1 day') RETURNING 1) SELECT count(*) FROM deleted", inbound_days)
    await pool.execute("DELETE FROM sender_locks WHERE locked_until < now() - interval '1 day'")
    await pool.execute("DELETE FROM agent_turns WHERE created_at < now() - ($1 * interval '1 day')", agent_days)
    p = await pool.fetchval("WITH deleted AS (DELETE FROM platform_agent_sessions WHERE updated_at < now() - ($1 * interval '1 day') RETURNING 1) SELECT count(*) FROM deleted", agent_days)
    t = await pool.fetchval("WITH deleted AS (DELETE FROM conversation_trace WHERE created_at < now() - ($1 * interval '1 day') RETURNING 1) SELECT count(*) FROM deleted", agent_days)
    h = await pool.fetchval(
        "WITH deleted AS (DELETE FROM handoffs WHERE COALESCE(resolved_at, created_at) < now() - ($1 * interval '1 day') RETURNING 1) "
        "SELECT count(*) FROM deleted", handoff_days)
    cb = await pool.fetchval(
        "WITH deleted AS (DELETE FROM mpesa_callbacks c WHERE c.received_at < now() - ($1 * interval '1 day') "
        "AND NOT EXISTS (SELECT 1 FROM payments p WHERE p.checkout_request_id=c.checkout_request_id AND p.status='pending') RETURNING 1) "
        "SELECT count(*) FROM deleted", callback_days)
    ur = await pool.fetchval(
        "WITH scrubbed AS (UPDATE unmatched_payments SET raw=NULL WHERE raw IS NOT NULL "
        "AND created_at < now() - ($1 * interval '1 day') RETURNING 1) SELECT count(*) FROM scrubbed", unmatched_raw_days)
    return {"webhook_jobs": int(w or 0) if isinstance(w, int) else 0, "inbound_messages": int(i or 0) if isinstance(i, int) else 0,
            "outbound_log": int(o or 0) if isinstance(o, int) else 0, "conversation_trace": int(t or 0) if isinstance(t, int) else 0,
            "platform_agent_sessions": int(p or 0) if isinstance(p, int) else 0,
            "handoffs": int(h or 0) if isinstance(h, int) else 0,
            "mpesa_callbacks": int(cb or 0) if isinstance(cb, int) else 0,
            "unmatched_payment_payloads": int(ur or 0) if isinstance(ur, int) else 0}
