from __future__ import annotations

import logging
from datetime import datetime, timedelta
from zoneinfo import ZoneInfo

from .metrics import reminders_queued, reminders_sent

log = logging.getLogger("rembo.reminders")
NBO = ZoneInfo("Africa/Nairobi")

async def schedule_reminders(pool, booking_id: str):
    row = await pool.fetchrow("SELECT b.id::text, b.salon_id::text, b.client_id::text, b.starts_at, b.status, c.wa_id, c.reminder_opt_out as client_opt, b.reminder_opt_out as booking_opt FROM bookings b JOIN clients c ON c.id=b.client_id WHERE b.id=$1::uuid", booking_id)
    if not row or row["status"] not in ("held", "confirmed"):
        return
    if row["client_opt"] or row["booking_opt"]:
        return
    starts_at = row["starts_at"]
    if starts_at.tzinfo is None:
        starts_at = starts_at.replace(tzinfo=NBO)
    now = datetime.now(NBO)
    if starts_at < now + timedelta(hours=3):
        return
    salon_id = row["salon_id"]
    templates = await pool.fetch("SELECT name FROM reminder_templates WHERE salon_id=$1::uuid AND approved=true", salon_id)
    if not templates:
        return
    names = {r["name"] for r in templates}
    jobs = []
    if "reminder_24h" in names:
        run_at = starts_at - timedelta(hours=24)
        if run_at > now:
            jobs.append(("reminder_24h", run_at))
    if "reminder_2h" in names:
        run_at = starts_at - timedelta(hours=2)
        if run_at > now:
            jobs.append(("reminder_2h", run_at))
    for name, run_at in jobs:
        exists = await pool.fetchval("SELECT 1 FROM reminder_jobs WHERE booking_id=$1::uuid AND template_name=$2", booking_id, name)
        if exists:
            continue
        await pool.execute("INSERT INTO reminder_jobs (booking_id, salon_id, client_id, wa_id, run_at, template_name) VALUES ($1::uuid,$2::uuid,$3::uuid,$4,$5,$6)", booking_id, row["salon_id"], row["client_id"], row["wa_id"], run_at, name)
        reminders_queued.inc()
        log.info("scheduled %s for %s at %s", name, booking_id, run_at.isoformat())

async def cancel_reminders(pool, booking_id: str):
    await pool.execute("UPDATE reminder_jobs SET status='cancelled' WHERE booking_id=$1::uuid AND status='pending'", booking_id)

async def run_reminders(ctx):
    rows = await ctx.pool.fetch("SELECT r.id::text, r.booking_id::text, r.salon_id::text, r.wa_id, r.template_name, b.starts_at, s.name as salon_name, s.wa_phone_number_id, s.wa_access_token, t.body, t.language FROM reminder_jobs r JOIN bookings b ON b.id=r.booking_id JOIN salons s ON s.id=r.salon_id JOIN reminder_templates t ON t.salon_id=r.salon_id AND t.name=r.template_name WHERE r.status='pending' AND r.run_at <= now() AND t.approved=true ORDER BY r.run_at LIMIT 20")
    for r in rows:
        try:
            salon = {"wa_phone_number_id": r["wa_phone_number_id"], "wa_access_token": r["wa_access_token"]}
            starts_at = r["starts_at"]
            if starts_at.tzinfo is None:
                starts_at = starts_at.replace(tzinfo=NBO)
            local = starts_at.astimezone(NBO)
            body = r["body"].format(salon=r["salon_name"], service="your appointment", when=local.strftime("%a %d %b %H:%M"), date=local.strftime("%Y-%m-%d"), time=local.strftime("%H:%M"))
            # check opt-out again
            opt = await ctx.pool.fetchval("SELECT c.reminder_opt_out OR b.reminder_opt_out FROM bookings b JOIN clients c ON c.id=b.client_id WHERE b.id=$1::uuid", r["booking_id"])
            if opt:
                await ctx.pool.execute("UPDATE reminder_jobs SET status='cancelled' WHERE id=$1::uuid", r["id"])
                continue
            # verify booking still confirmed
            status = await ctx.pool.fetchval("SELECT status FROM bookings WHERE id=$1::uuid", r["booking_id"])
            if status != "confirmed":
                await ctx.pool.execute("UPDATE reminder_jobs SET status='cancelled' WHERE id=$1::uuid", r["id"])
                continue
            sent = False
            if r["template_name"]:
                # try template first
                try:
                    sent = await ctx.wa.send_template(r["wa_phone_number_id"], r["wa_access_token"] or ctx.settings.wa_access_token, r["wa_id"], r["template_name"], r["language"], [body])
                    if sent is False:
                        sent = False
                    else:
                        sent = True
                except Exception as e:
                    log.warning("template send failed %s: %s", r["template_name"], e)
                    sent = False
            if not sent:
                # fallback to text if within 24h window (opportunistic)
                try:
                    sent = await ctx.wa.send_text(r["wa_phone_number_id"], r["wa_access_token"] or ctx.settings.wa_access_token, r["wa_id"], body)
                except Exception:
                    sent = False
            if sent:
                await ctx.pool.execute("UPDATE reminder_jobs SET status='sent', sent_at=now(), attempts=attempts+1 WHERE id=$1::uuid", r["id"])
                reminders_sent.labels("sent").inc()
                log.info("sent reminder %s to %s", r["template_name"], r["wa_id"][-4:])
            else:
                await ctx.pool.execute("UPDATE reminder_jobs SET status='failed', attempts=attempts+1, last_error='send failed' WHERE id=$1::uuid", r["id"])
                reminders_sent.labels("failed").inc()
        except Exception as e:
            log.exception("reminder %s failed", r["id"])
            await ctx.pool.execute("UPDATE reminder_jobs SET status='failed', attempts=attempts+1, last_error=$2 WHERE id=$1::uuid", r["id"], str(e)[:500])
            reminders_sent.labels("error").inc()
