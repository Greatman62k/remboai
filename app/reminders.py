from __future__ import annotations

import json
import logging
from datetime import datetime, timedelta
from zoneinfo import ZoneInfo

from . import payments, waitlist
from .messages import t
from .metrics import reminders_queued, reminders_sent
from .channels import outbound

log = logging.getLogger("rembo.reminders")
NBO = ZoneInfo("Africa/Nairobi")

MAX_ATTEMPTS = 3
RETRY_DELAYS = (timedelta(minutes=5), timedelta(minutes=15))
CLAIM_LEASE = timedelta(minutes=5)
THANKS_TEMPLATE = "thanks_visit"
STILL_TEMPLATE = "reminder_3h"
SOON_TEMPLATE = "reminder_soon"
OWNER_SOON_TEMPLATE = "owner_reminder_soon"
OWNER_CANCELLED_TEMPLATE = "owner_booking_cancelled"
SOON_LEAD = timedelta(minutes=30)


async def ask_about_visits(pool, ctx) -> int:
    """As soon as the scheduled service ends, ask once whether the customer was attended. YES thanks them
    and invites feedback (handled in the conversation); NO alerts the owner at once."""
    now = datetime.now(NBO)
    rows = await pool.fetch(
        "SELECT b.id::text AS booking, b.salon_id::text AS salon, s.name AS salon_name, s.wa_phone_number_id AS pid, "
        "s.wa_access_token AS token, sv.name AS service, c.wa_id AS phone, c.lang "
        "FROM bookings b JOIN salons s ON s.id=b.salon_id JOIN services sv ON sv.id=b.service_id "
        "JOIN clients c ON c.id=b.client_id LEFT JOIN visit_checks v ON v.booking_id=b.id "
        "WHERE b.status='confirmed' AND b.ends_at > $1 AND b.ends_at <= $2 AND v.booking_id IS NULL "
        "ORDER BY b.ends_at LIMIT 20", now - timedelta(hours=6), now)
    n = 0
    for r in rows:
        lang = r["lang"] or "en"
        body = t(lang, "visit_ask", salon=r["salon_name"], service=r["service"])
        buttons = [(f"visit-yes:{r['booking']}", t(lang, "visit_btn_yes")[:20]),
                   (f"visit-no:{r['booking']}", t(lang, "visit_btn_no")[:20])]
        try:
            await pool.execute("INSERT INTO visit_checks (booking_id, salon_id, client_id) "
                               "SELECT $1::uuid, b.salon_id, b.client_id FROM bookings b WHERE b.id=$1::uuid "
                               "ON CONFLICT DO NOTHING", r["booking"])
            salon = {"id": r["salon"], "name": r["salon_name"], "wa_phone_number_id": r["pid"], "wa_access_token": r["token"]}
            ok = await outbound.send_buttons(ctx, salon, r["phone"], body, buttons)
            if not ok:
                log.warning("visit check for booking %s could not be delivered", r["booking"])
            n += 1
        except Exception:
            log.exception("visit check failed for booking %s", r["booking"])
    return n


class _Fmt(dict):
    def __missing__(self, key):
        return "{" + key + "}"

async def schedule_reminders(pool, booking_id: str, template_24h: str = "reminder_24h", template_2h: str = "reminder_2h",
                         template_3h: str = STILL_TEMPLATE):
    row = await pool.fetchrow("SELECT b.id::text, b.salon_id::text, b.client_id::text, b.starts_at, b.ends_at, b.status, c.wa_id, c.reminder_opt_out as client_opt, b.reminder_opt_out as booking_opt, s.owner_wa_id FROM bookings b JOIN clients c ON c.id=b.client_id JOIN salons s ON s.id=b.salon_id WHERE b.id=$1::uuid", booking_id)
    if not row or row["status"] not in ("held", "confirmed"):
        return
    starts_at = row["starts_at"]
    if starts_at.tzinfo is None:
        starts_at = starts_at.replace(tzinfo=NBO)
    now = datetime.now(NBO)
    salon_id = row["salon_id"]
    templates = await pool.fetch("SELECT name FROM reminder_templates WHERE salon_id=$1::uuid AND approved=true", salon_id)
    names = {r["name"] for r in templates}
    jobs = []
    lead = starts_at - now
    if lead > SOON_LEAD:
        # The appointment ETA goes to both sides 30 minutes before the start.
        # Inside that window the immediate booking messages are the timely notice.
        if not (row["client_opt"] or row["booking_opt"]):
            if row["wa_id"].startswith("tg:") or SOON_TEMPLATE in names:
                jobs.append((row["wa_id"], SOON_TEMPLATE, starts_at - SOON_LEAD))
        owner = row["owner_wa_id"]
        if owner and (owner.startswith("tg:") or OWNER_SOON_TEMPLATE in names):
            jobs.append((owner, OWNER_SOON_TEMPLATE, starts_at - SOON_LEAD))
        if not (row["client_opt"] or row["booking_opt"]) and lead > timedelta(hours=2):
            if template_24h in names:
                run_at = starts_at - timedelta(hours=24)
                if run_at > now:
                    jobs.append((row["wa_id"], template_24h, run_at))
            if template_2h in names:
                run_at = starts_at - timedelta(hours=2)
                if run_at > now:
                    jobs.append((row["wa_id"], template_2h, run_at))
            if template_3h in names:
                run_at = starts_at - timedelta(hours=3)
                if run_at > now:
                    jobs.append((row["wa_id"], template_3h, run_at))
    if not (row["client_opt"] or row["booking_opt"]) and THANKS_TEMPLATE in names and row["ends_at"] is not None:
        run_at = (row["ends_at"] if row["ends_at"].tzinfo else row["ends_at"].replace(tzinfo=NBO)) + timedelta(hours=2)
        if run_at > now:
            jobs.append((row["wa_id"], THANKS_TEMPLATE, run_at))
    for recipient, name, run_at in jobs:
        exists = await pool.fetchval("SELECT 1 FROM reminder_jobs WHERE booking_id=$1::uuid AND template_name=$2 AND wa_id=$3 AND status='pending'", booking_id, name, recipient)
        if exists:
            continue
        await pool.execute("INSERT INTO reminder_jobs (booking_id, salon_id, client_id, wa_id, run_at, template_name) VALUES ($1::uuid,$2::uuid,$3::uuid,$4,$5,$6)", booking_id, row["salon_id"], row["client_id"], recipient, run_at, name)
        reminders_queued.inc()
        log.info("scheduled %s for %s at %s", name, booking_id, run_at.isoformat())


async def schedule_owner_cancellation(pool, booking_id: str) -> None:
    """Queue one owner notice when the customer explicitly cancels a booking."""
    row = await pool.fetchrow(
        "SELECT b.salon_id::text, b.client_id::text, b.status, s.owner_wa_id "
        "FROM bookings b JOIN salons s ON s.id=b.salon_id WHERE b.id=$1::uuid", booking_id)
    if not row or row["status"] != "cancelled" or not row["owner_wa_id"]:
        return
    recipient = row["owner_wa_id"]
    if not recipient.startswith("tg:"):
        approved = await pool.fetchval(
            "SELECT 1 FROM reminder_templates WHERE salon_id=$1::uuid AND name=$2 AND approved=true",
            row["salon_id"], OWNER_CANCELLED_TEMPLATE)
        if not approved:
            log.warning("owner cancellation notice for booking %s needs approved %s template", booking_id, OWNER_CANCELLED_TEMPLATE)
            return
    inserted = await pool.fetchval(
        "INSERT INTO reminder_jobs (booking_id, salon_id, client_id, wa_id, run_at, template_name) "
        "SELECT $1::uuid,$2::uuid,$3::uuid,$4,now(),$5 WHERE NOT EXISTS "
        "(SELECT 1 FROM reminder_jobs WHERE booking_id=$1::uuid AND wa_id=$4 AND template_name=$5) "
        "RETURNING id::text",
        booking_id, row["salon_id"], row["client_id"], recipient, OWNER_CANCELLED_TEMPLATE)
    if inserted:
        reminders_queued.inc()

async def cancel_reminders(pool, booking_id: str):
    await pool.execute("UPDATE reminder_jobs SET status='cancelled' WHERE booking_id=$1::uuid AND status='pending'", booking_id)

async def _settle_failure(pool, job_id: str, attempts: int, error: str, starts_at: datetime) -> None:
    """A send that did not go through is retried a couple of times, later, while the appointment is still ahead; then it is marked failed."""
    now = datetime.now(NBO)
    if attempts < MAX_ATTEMPTS:
        retry_at = now + RETRY_DELAYS[min(attempts, len(RETRY_DELAYS)) - 1]
        if retry_at < starts_at:
            await pool.execute("UPDATE reminder_jobs SET status='pending', run_at=$2, last_error=$3 WHERE id=$1::uuid", job_id, retry_at, error[:500])
            reminders_sent.labels("retry").inc()
            return
        error = f"{error} (too close to the appointment to retry)"
    await pool.execute("UPDATE reminder_jobs SET status='failed', last_error=$2 WHERE id=$1::uuid", job_id, error[:500])
    reminders_sent.labels("failed").inc()
    log.error("reminder %s failed for good after %d attempt(s): %s", job_id, attempts, error)


async def run_reminders(ctx):
    rows = await ctx.pool.fetch(
        "SELECT r.id::text, r.booking_id::text, r.salon_id::text, r.wa_id, r.template_name, b.starts_at, b.ends_at, s.name AS salon_name, s.wa_phone_number_id, s.wa_access_token, "
        "t.body, t.language, t.variables, c.name AS client_name, c.lang, sv.name AS service_name "
        "FROM reminder_jobs r JOIN bookings b ON b.id=r.booking_id JOIN salons s ON s.id=r.salon_id "
        "LEFT JOIN reminder_templates t ON t.salon_id=r.salon_id AND t.name=r.template_name AND t.approved=true "
        "JOIN clients c ON c.id=r.client_id JOIN services sv ON sv.id=b.service_id "
        "WHERE r.status='pending' AND r.run_at <= now() "
        "AND (t.approved=true OR (r.wa_id LIKE 'tg:%' AND r.template_name IN ($1,$2,$3,$4))) "
        "ORDER BY r.run_at LIMIT 20", SOON_TEMPLATE, OWNER_SOON_TEMPLATE, OWNER_CANCELLED_TEMPLATE, STILL_TEMPLATE)
    for r in rows:
        attempts = await ctx.pool.fetchval(
            "UPDATE reminder_jobs SET attempts=attempts+1, run_at=now()+$2 WHERE id=$1::uuid AND status='pending' AND run_at <= now() RETURNING attempts",
            r["id"], CLAIM_LEASE)
        if attempts is None:
            continue
        starts_at = r["starts_at"]
        if starts_at.tzinfo is None:
            starts_at = starts_at.replace(tzinfo=NBO)
        try:
            await _deliver(ctx, r, attempts, starts_at)
        except Exception as e:
            log.exception("reminder %s failed", r["id"])
            reminders_sent.labels("error").inc()
            await _settle_failure(ctx.pool, r["id"], attempts, f"{e.__class__.__name__}: {e}", starts_at)


def _template_values(r, local: datetime) -> dict:
    minutes = max(1, int((local - datetime.now(NBO)).total_seconds() + 59) // 60)
    return _Fmt(salon=r["salon_name"], name=r["client_name"] or "there", service=r["service_name"], when=local.strftime("%a %d %b %H:%M"),
                date=local.strftime("%Y-%m-%d"), time=local.strftime("%H:%M"), minutes=minutes)


async def _deliver(ctx, r, attempts: int, starts_at: datetime) -> None:
    pool = ctx.pool
    cancel = "UPDATE reminder_jobs SET status='cancelled' WHERE id=$1::uuid"
    state = await pool.fetchrow("SELECT b.status, c.reminder_opt_out OR b.reminder_opt_out AS opt_out FROM bookings b JOIN clients c ON c.id=b.client_id WHERE b.id=$1::uuid", r["booking_id"])
    is_owner_job = r["template_name"] in (OWNER_SOON_TEMPLATE, OWNER_CANCELLED_TEMPLATE)
    if state is None or (state["opt_out"] and not is_owner_job):
        await pool.execute(cancel, r["id"])
        return
    if r["template_name"] == OWNER_CANCELLED_TEMPLATE:
        if state["status"] != "cancelled":
            await pool.execute(cancel, r["id"])
            return
        local = starts_at.astimezone(NBO)
        values = _template_values(r, local)
        if r["wa_id"].startswith("tg:") or not r["body"]:
            body = t("en", "owner_booking_cancelled", name=values["name"], service=r["service_name"],
                     salon=r["salon_name"], when=values["when"])
            params = [body]
        else:
            body = r["body"].format_map(values)
            names = r["variables"] if isinstance(r["variables"], list) else json.loads(r["variables"] or "[]")
            params = [str(values[n]) for n in names] if names else [body]
        await _send_reminder(ctx, r, attempts, starts_at, body, params)
        return
    if r["template_name"] == THANKS_TEMPLATE:
        ends_at = r["ends_at"]
        if ends_at is not None and ends_at.tzinfo is None:
            ends_at = ends_at.replace(tzinfo=NBO)
        if state["status"] != "confirmed" or ends_at is None:
            await pool.execute(cancel, r["id"])
            return
        if ends_at > datetime.now(NBO):
            await pool.execute("UPDATE reminder_jobs SET run_at=now()+($2) WHERE id=$1::uuid", r["id"], timedelta(hours=1))
            return
    else:
        if starts_at <= datetime.now(NBO) or state["status"] not in ("held", "confirmed"):
            await pool.execute(cancel, r["id"])
            return
        if state["status"] != "confirmed":
            await pool.execute("UPDATE reminder_jobs SET run_at=now()+($2) WHERE id=$1::uuid", r["id"], timedelta(minutes=10))
            return
    if r["template_name"] == STILL_TEMPLATE:
        await _deliver_still(ctx, r, starts_at)
        return
    local = starts_at.astimezone(NBO)
    values = _template_values(r, local)
    if r["template_name"] == SOON_TEMPLATE and (r["wa_id"].startswith("tg:") or not r["body"]):
        minutes = values["minutes"]
        body = t(r["lang"] or "en", "reminder_soon", salon=r["salon_name"], service=r["service_name"],
                 when=local.strftime("%a %d %b %H:%M"), minutes=minutes)
        params = [body]
    elif r["template_name"] == OWNER_SOON_TEMPLATE and (r["wa_id"].startswith("tg:") or not r["body"]):
        minutes = values["minutes"]
        body = t("en", "owner_eta_soon", name=r["client_name"] or "Customer", service=r["service_name"],
                 salon=r["salon_name"], when=local.strftime("%a %d %b %H:%M"), minutes=minutes)
        params = [body]
    else:
        body = r["body"].format_map(values)
        names = r["variables"] if isinstance(r["variables"], list) else json.loads(r["variables"] or "[]")
        params = [str(values[n]) for n in names] if names else [body]
    await _send_reminder(ctx, r, attempts, starts_at, body, params)


async def _send_reminder(ctx, r, attempts: int, starts_at: datetime, body: str, params: list[str]) -> None:
    pool = ctx.pool
    token, pid, to = outbound.salon_token(ctx, {"id": r["salon_id"], "wa_access_token": r["wa_access_token"]}), r["wa_phone_number_id"], r["wa_id"]
    sent = False
    if not to.startswith("tg:"):
        try:
            sent = bool(await ctx.wa.send_template(pid, token, to, r["template_name"], r["language"], params))
        except Exception as e:
            log.warning("template send failed %s: %s", r["template_name"], e.__class__.__name__)
    if not sent:
        sent = bool(await ctx.wa.send_text(pid, token, to, body))
    if sent:
        await pool.execute("UPDATE reminder_jobs SET status='sent', sent_at=now(), last_error=NULL WHERE id=$1::uuid", r["id"])
        reminders_sent.labels("sent").inc()
        log.info("sent reminder %s to %s", r["template_name"], to[-4:])
    else:
        await _settle_failure(pool, r["id"], attempts, "send failed", starts_at)


async def _deliver_still(ctx, r, starts_at: datetime) -> None:
    """The 3h Still-coming check offers Keep/Cancel; silence leaves the booking confirmed."""
    lang = r["lang"] or "en"
    body = r["body"].format_map(_template_values(r, starts_at.astimezone(NBO)))
    buttons = [(f"still-keep:{r['booking_id']}", t(lang, "still_btn_keep")[:20]),
               (f"still-cancel:{r['booking_id']}", t(lang, "still_btn_cancel")[:20])]
    salon = {"id": r["salon_id"], "wa_phone_number_id": r["wa_phone_number_id"], "wa_access_token": r["wa_access_token"]}
    sent = await outbound.send_buttons(ctx, salon, r["wa_id"], body, buttons)
    if sent:
        await ctx.pool.execute("UPDATE reminder_jobs SET status='sent', sent_at=now(), last_error=NULL WHERE id=$1::uuid", r["id"])
        reminders_sent.labels("sent").inc()
        log.info("sent reminder %s to %s", STILL_TEMPLATE, r["wa_id"][-4:])
    else:
        raise RuntimeError("still send failed")


async def handle_still_tap(ctx, salon: dict, client: dict, st: dict, chat, lang: str, tz, reply_id: str) -> bool:
    """Keep acknowledges the booking; Cancel frees the slot toward the waitlist."""
    from .booking_flow import _reply, _save, when_label
    kind, _, bid = reply_id.partition(":")
    if kind not in ("still-keep", "still-cancel") or not bid:
        return False
    row = await ctx.pool.fetchrow(
        "SELECT b.id::text AS id, b.status, b.starts_at FROM bookings b "
        "WHERE b.id=$1::uuid AND b.salon_id=$2::uuid AND b.client_id=$3::uuid", bid, salon["id"], client["id"])
    if row is None or row["status"] not in ("held", "confirmed"):
        return True
    if row["starts_at"] <= datetime.now(NBO):
        return True
    if kind == "still-keep":
        await ctx.pool.execute("UPDATE reminder_jobs SET replied=true WHERE booking_id=$1::uuid AND template_name=$2 AND status='sent'",
                               bid, STILL_TEMPLATE)
        await _reply(ctx, salon["id"], chat.to, st, chat, t(lang, "still_kept", when=when_label(row["starts_at"], tz)))
        return True
    await ctx.pool.execute("UPDATE bookings SET status='cancelled' WHERE id=$1::uuid AND status IN ('held','confirmed')", bid)
    await payments.park_for_cancellation(ctx.pool, bid)
    await cancel_reminders(ctx.pool, bid)
    cleared = {"history": st.get("history", [])}
    await _save(ctx, salon["id"], chat.to, cleared)
    await _reply(ctx, salon["id"], chat.to, cleared, chat,
                 t(lang, "cancelled", when=when_label(row["starts_at"], tz), salon=salon["name"]))
    await schedule_owner_cancellation(ctx.pool, bid)
    await waitlist.offer_for_booking(ctx, salon, bid)
    return True
