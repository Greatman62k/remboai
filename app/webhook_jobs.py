from __future__ import annotations

import asyncio
import logging
from contextlib import asynccontextmanager

from .mpesa import parse_callback
from .payments import settle_with_verification

log = logging.getLogger("rembo.webhooks")
MAX_ATTEMPTS = 8
LEASE_SECONDS = 60
_QUEUE_LOCK = 1383000578


class QueueCapacityError(Exception):
    pass


class PermanentJobError(Exception):
    pass


@asynccontextmanager
async def _execution_lock(pool, job_id: int):
    key = -int(job_id)
    async with pool.acquire() as conn:
        acquired = bool(await conn.fetchval("SELECT pg_try_advisory_lock($1::bigint)", key))
        try:
            yield acquired
        finally:
            if acquired:
                try:
                    await conn.execute("SELECT pg_advisory_unlock($1::bigint)", key)
                except Exception:
                    log.exception("failed to release execution lock for job %s", job_id)


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
        if provider == "whatsapp":
            for eid, payload in pending.items():
                wa_id = payload.get("wa_id") or payload.get("sender")
                if wa_id:
                    c = await conn.fetchval("SELECT count(*) FROM webhook_jobs WHERE provider='whatsapp' AND status IN ('pending','processing') AND payload->>'wa_id'=$1", str(wa_id))
                    if int(c or 0) >= max(1, int(max_pending_per_sender)):
                        raise QueueCapacityError(f"per-sender queue capacity reached for {wa_id}")
        for eid, payload in pending.items():
            await conn.execute("INSERT INTO webhook_jobs (provider, event_id, payload) VALUES ($1,$2,$3)", provider, eid, payload)


async def claim_next(pool):
    async with pool.acquire() as conn, conn.transaction():
        row = await conn.fetchrow(
            "SELECT * FROM webhook_jobs WHERE status IN ('pending','processing') AND (locked_until IS NULL OR locked_until < now()) AND available_at <= now() ORDER BY available_at, id LIMIT 1 FOR UPDATE SKIP LOCKED"
        )
        if row is None:
            return None
        job = dict(row)
        await conn.execute("UPDATE webhook_jobs SET status='processing', attempts=attempts+1, locked_until=now()+($2 * interval '1 second'), updated_at=now() WHERE id=$1", job["id"], LEASE_SECONDS)
        job["attempts"] += 1
        return job


async def complete_job(pool, job_id: int) -> None:
    await pool.execute("UPDATE webhook_jobs SET status='processed', payload='{}'::jsonb, locked_until=NULL, last_error=NULL, completed_at=now(), updated_at=now() WHERE id=$1", job_id)


async def dead_job(pool, job_id: int, error: str) -> None:
    await pool.execute("UPDATE webhook_jobs SET status='dead', locked_until=NULL, last_error=$2, updated_at=now() WHERE id=$1", job_id, error[:2000])


async def retry_job(pool, job: dict, error: Exception) -> None:
    if job["attempts"] >= MAX_ATTEMPTS:
        await dead_job(pool, job["id"], f"{error.__class__.__name__}: {error}")
        return
    delay = min(300, 2 ** job["attempts"])
    await pool.execute("UPDATE webhook_jobs SET status='pending', locked_until=NULL, last_error=$2, available_at=now()+($3 * interval '1 second'), updated_at=now() WHERE id=$1", job["id"], f"{error.__class__.__name__}: {error}"[:2000], delay)


async def requeue_dead_job(pool, job_id: int) -> bool:
    r = await pool.execute("UPDATE webhook_jobs SET status='pending', attempts=0, locked_until=NULL, last_error=NULL, available_at=now(), updated_at=now() WHERE id=$1 AND status='dead'", job_id)
    return r.endswith("1")


async def list_dead_jobs(pool):
    rows = await pool.fetch("SELECT id, provider, event_id, payload, attempts, last_error, created_at FROM webhook_jobs WHERE status='dead' ORDER BY created_at DESC LIMIT 100")
    return [dict(r) for r in rows]


async def _process_claimed_job(ctx, job: dict) -> None:
    if job["provider"] == "whatsapp":
        from .inbound import inbound as handle_inbound
        from .whatsapp import Message
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
        )
        if not msg.sender or not msg.id:
            raise PermanentJobError("invalid whatsapp payload")
        await handle_inbound(ctx, msg)
        async with ctx.pool.acquire() as conn, conn.transaction():
            await conn.execute("INSERT INTO inbound_messages (wa_message_id, wa_id) VALUES ($1,$2) ON CONFLICT DO NOTHING", msg.id, msg.sender)
            await conn.execute("UPDATE webhook_jobs SET status='processed', payload='{}'::jsonb, locked_until=NULL, last_error=NULL, completed_at=now(), updated_at=now() WHERE id=$1", job["id"])
        return
    if job["provider"] == "telegram":
        from .inbound import inbound as handle_inbound
        from .whatsapp import Message
        payload = job["payload"] if isinstance(job["payload"], dict) else __import__("json").loads(job["payload"])
        msg = Message(
            id=payload.get("id") or job["event_id"],
            sender=payload.get("sender") or "",
            phone_number_id=payload.get("phone_number_id") or "",
            kind=payload.get("kind") or "text",
            text=payload.get("text") or "",
            media_id=payload.get("media_id"),
            name=payload.get("name"),
            reply_id=payload.get("reply_id"),
        )
        await handle_inbound(ctx, msg)
        await complete_job(ctx.pool, job["id"])
        return
    if job["provider"] == "mpesa":
        payload = job["payload"] if isinstance(job["payload"], dict) else __import__("json").loads(job["payload"])
        cb = parse_callback(payload)
        if cb is None:
            raise PermanentJobError("invalid M-Pesa callback payload")
        outcome = await settle_with_verification(ctx, cb)
        if outcome is not None and outcome == "unknown":
            if job["attempts"] < MAX_ATTEMPTS:
                raise RuntimeError(f"unknown CheckoutRequestID {cb.checkout_request_id}")
            if cb.code == 0:
                await ctx.pool.execute(
                    "INSERT INTO unmatched_payments (checkout_request_id, merchant_request_id, result_code, amount, phone, mpesa_receipt, raw) VALUES ($1,$2,$3,$4,$5,$6,$7::jsonb) ON CONFLICT DO NOTHING",
                    cb.checkout_request_id, None, cb.code, cb.amount, cb.phone, cb.receipt, payload,
                )
                log.error("UNMATCHED PAYMENT recorded: checkout=%s amount=%s receipt=%s phone=%s", cb.checkout_request_id, cb.amount, cb.receipt, cb.phone)
        await complete_job(ctx.pool, job["id"])
        return
    raise PermanentJobError(f"unsupported provider {job['provider']}")


async def process_job(ctx, job: dict) -> bool:
    async with _execution_lock(ctx.pool, job["id"]) as acquired:
        if not acquired:
            log.info("skipping job %s: execution lock held", job["id"])
            return False
        await _process_claimed_job(ctx, job)
        return True


async def webhook_worker(ctx) -> None:
    while True:
        try:
            job = await claim_next(ctx.pool)
            if job is None:
                await asyncio.sleep(0.5)
                continue
            try:
                await process_job(ctx, job)
            except PermanentJobError as e:
                await dead_job(ctx.pool, job["id"], str(e))
                log.error("Webhook job %s/%s rejected: %s", job["provider"], job["event_id"], e)
            except Exception as e:
                await retry_job(ctx.pool, job, e)
        except asyncio.CancelledError:
            raise
        except Exception:
            log.exception("webhook worker iteration failed")
            await asyncio.sleep(1)


async def purge_expired_data(pool, webhook_days: int = 30, inbound_days: int = 90) -> dict:
    w = await pool.fetchval("WITH deleted AS (DELETE FROM webhook_jobs WHERE status IN ('processed','dead') AND completed_at < now() - ($1 * interval '1 day') RETURNING 1) SELECT count(*) FROM deleted", webhook_days)
    i = await pool.fetchval("WITH deleted AS (DELETE FROM inbound_messages WHERE received_at < now() - ($1 * interval '1 day') RETURNING 1) SELECT count(*) FROM deleted", inbound_days)
    return {"webhook_jobs": int(w or 0) if isinstance(w, int) else 0, "inbound_messages": int(i or 0) if isinstance(i, int) else 0}
