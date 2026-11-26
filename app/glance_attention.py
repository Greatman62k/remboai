"""Owner attention items that can be safely surfaced in RemboAI Glance.

The owner channel still receives the original WhatsApp/Telegram messages.  This
module is a read model over durable business records so a paired Glance device
can discover the same work without relying on delivery of those messages.

This is intentionally read-only.  Items whose existing action is sensitive or
time-critical identify the existing owner workflow instead of inventing a new
state transition in the Glance API.
"""
from __future__ import annotations

import re
import unicodedata
from datetime import date, datetime, timezone
from typing import Any
from zoneinfo import ZoneInfo


_BIDI = {
    "\u061c", "\u200e", "\u200f", "\u202a", "\u202b", "\u202c", "\u202d", "\u202e",
    "\u2066", "\u2067", "\u2068", "\u2069",
}
_PHONE_LIKE = re.compile(r"(?<!\w)\+?\d[\d ().-]{7,}\d(?!\w)")
_EMAIL_LIKE = re.compile(r"(?<![\w.+-])[\w.+-]{1,64}@[\w.-]{1,190}\.[A-Za-z]{2,}(?![\w.-])")
_URL_LIKE = re.compile(r"\bhttps?://\S+", re.I)
_REFERENCE_LIKE = re.compile(r"(?<![A-Z0-9])[A-Z0-9]{10}(?![A-Z0-9])")

_PRIORITY_ORDER = {"critical": 0, "high": 1, "normal": 2, "low": 3}


def _clean(value: Any, limit: int = 240) -> str:
    text = unicodedata.normalize("NFC", str(value or ""))
    kept: list[str] = []
    for char in text:
        category = unicodedata.category(char)
        if char in _BIDI or category in {"Cf", "Cs", "Co", "Cn"}:
            continue
        if category == "Cc" and char not in "\n\t":
            continue
        kept.append(char)
    text = "".join(kept)
    text = _PHONE_LIKE.sub("[contact redacted]", text)
    text = _EMAIL_LIKE.sub("[contact redacted]", text)
    text = _URL_LIKE.sub("[link redacted]", text)
    text = _REFERENCE_LIKE.sub("[reference redacted]", text)
    text = "\n".join(" ".join(line.split()) for line in text.splitlines())
    return text.strip()[:limit].strip()


def _label(value: Any) -> str:
    label = _clean(value, 40)
    return label if re.search(r"[A-Za-z\u00c0-\uffff]", label) else "Customer"


def _timestamp(value: Any) -> datetime | None:
    if value is None:
        return None
    if isinstance(value, datetime):
        return value if value.tzinfo else value.replace(tzinfo=timezone.utc)
    if isinstance(value, date):
        return datetime(value.year, value.month, value.day, tzinfo=timezone.utc)
    return None


def _item(
    *,
    attention_id: str,
    kind: str,
    priority: str,
    state: str,
    title: str,
    summary: str,
    created_at: Any,
    expires_at: Any = None,
    case_id: str | None = None,
    resource_id: str | None = None,
    action: str,
) -> dict[str, Any]:
    return {
        "attention_id": attention_id,
        "kind": kind,
        "priority": priority,
        "state": state,
        "title": _clean(title, 80),
        "summary": _clean(summary, 240),
        "created_at": _timestamp(created_at),
        "expires_at": _timestamp(expires_at),
        "case_id": case_id,
        "resource_id": resource_id,
        "action": action,
    }


async def _verified_owner(con, salon_id: str, owner_identity: str) -> dict[str, Any]:
    row = await con.fetchrow(
        "SELECT id::text AS id,name,owner_wa_id,timezone,last_summary_on,"
        "inactivity_warned_at,inactivity_final_at FROM salons WHERE id=$1::uuid FOR SHARE",
        salon_id,
    )
    if row is None or not row["owner_wa_id"]:
        raise PermissionError("salon not found")
    if row["owner_wa_id"].strip().lstrip("+") != (owner_identity or "").strip().lstrip("+"):
        raise PermissionError("actor is not the salon owner")
    return dict(row)


async def list_attention(pool, salon_id: str, owner_identity: str, limit: int = 50) -> list[dict[str, Any]]:
    """Return the durable owner work queue for one currently-owned salon.

    The query deliberately exposes no phone numbers, payment references,
    payment evidence, provider tokens, or internal handoff events.  The result
    is bounded in memory and sorted by urgency before the requested limit is
    applied.  The API caller uses the returned length and total separately to
    calculate ``has_more``.
    """
    limit = max(1, min(int(limit), 50))
    async with pool.acquire() as con:
        async with con.transaction():
            salon = await _verified_owner(con, salon_id, owner_identity)
            items: list[dict[str, Any]] = []

            # Existing Glance cases are part of the unified owner queue.  The
            # full context remains available only through the case endpoint.
            handoffs = await con.fetch(
                "SELECT h.id::text AS id,h.category,h.created_at,c.name AS customer_name,"
                "CASE WHEN c.wa_id LIKE 'tg:%' THEN 'Telegram' ELSE 'WhatsApp' END AS channel,"
                "COALESCE(preview.body,'') AS preview FROM handoffs h "
                "JOIN clients c ON c.id=h.client_id AND c.salon_id=h.salon_id "
                "LEFT JOIN LATERAL (SELECT e.body FROM handoff_events e "
                "WHERE e.handoff_id=h.id AND e.salon_id=h.salon_id "
                "AND e.visibility='customer_visible' "
                "AND e.event_type IN ('customer_message','assistant_reply','approved_reply') "
                "ORDER BY e.sequence DESC LIMIT 1) preview ON true "
                "WHERE h.salon_id=$1::uuid AND h.status='open' "
                "ORDER BY h.created_at,h.id LIMIT 51",
                salon_id,
            )
            for row in handoffs:
                items.append(_item(
                    attention_id=f"case:{row['id']}", kind="escalation", priority="high", state="open",
                    title=f"Customer escalation · {_label(row['customer_name'])}",
                    summary=f"{row['channel']} · {_clean(row['category'], 40)} · {_clean(row['preview'], 150)}",
                    created_at=row["created_at"], case_id=row["id"], resource_id=row["id"],
                    action="open_case",
                ))

            # Manual-payment booking availability questions expire quickly and
            # are the most time-sensitive actionable owner items.
            checks = await con.fetch(
                "SELECT b.id::text AS id,b.starts_at,b.owner_check_expires_at,c.name AS customer_name,"
                "sv.name AS service_name,st.name AS staff_name FROM bookings b "
                "JOIN clients c ON c.id=b.client_id AND c.salon_id=b.salon_id "
                "JOIN services sv ON sv.id=b.service_id AND sv.salon_id=b.salon_id "
                "JOIN staff st ON st.id=b.staff_id AND st.salon_id=b.salon_id "
                "WHERE b.salon_id=$1::uuid AND b.status='held' AND b.owner_check_status='pending' "
                "AND b.owner_check_expires_at > now() ORDER BY b.owner_check_expires_at,b.id LIMIT 51",
                salon_id,
            )
            for row in checks:
                items.append(_item(
                    attention_id=f"availability:{row['id']}", kind="availability_decision", priority="critical",
                    state="pending", title="Booking availability decision",
                    summary=f"{_label(row['customer_name'])} · {_clean(row['service_name'], 60)} · "
                            f"{_clean(row['staff_name'], 40)} · reply in the owner booking workflow",
                    created_at=row["starts_at"], expires_at=row["owner_check_expires_at"],
                    resource_id=row["id"], action="availability_decision_owner_workflow",
                ))

            # Payment claims are visible only as a request to verify the
            # receiving account.  Raw receipts, references, amounts and phone
            # numbers stay in the existing owner payment workflow.
            claims = await con.fetch(
                "SELECT p.id::text AS id,p.created_at,b.hold_expires_at,c.name AS customer_name,"
                "sv.name AS service_name,p.status FROM payments p "
                "JOIN bookings b ON b.id=p.booking_id AND b.salon_id=p.salon_id "
                "JOIN clients c ON c.id=b.client_id AND c.salon_id=b.salon_id "
                "JOIN services sv ON sv.id=b.service_id AND sv.salon_id=b.salon_id "
                "WHERE p.salon_id=$1::uuid AND p.method='manual' AND p.status IN ('pending','review') "
                "ORDER BY p.created_at,p.id LIMIT 51",
                salon_id,
            )
            for row in claims:
                items.append(_item(
                    attention_id=f"payment:{row['id']}", kind="payment_review", priority="high",
                    state="review" if row["status"] == "review" else "pending",
                    title="Payment review needed",
                    summary=f"{_label(row['customer_name'])} · {_clean(row['service_name'], 60)} · "
                            "verify the receiving account before deciding",
                    created_at=row["created_at"], expires_at=row["hold_expires_at"],
                    resource_id=row["id"], action="payment_review_owner_workflow",
                ))

            refunds = await con.fetch(
                "SELECT p.id::text AS id,p.settled_at,p.created_at,b.starts_at,c.name AS customer_name,"
                "sv.name AS service_name FROM payments p "
                "JOIN bookings b ON b.id=p.booking_id AND b.salon_id=p.salon_id "
                "JOIN clients c ON c.id=b.client_id AND c.salon_id=b.salon_id "
                "JOIN services sv ON sv.id=b.service_id AND sv.salon_id=b.salon_id "
                "WHERE p.salon_id=$1::uuid AND p.status='needs_refund' "
                "ORDER BY COALESCE(p.settled_at,p.created_at),p.id LIMIT 51",
                salon_id,
            )
            for row in refunds:
                items.append(_item(
                    attention_id=f"refund:{row['id']}", kind="refund_follow_up", priority="high", state="open",
                    title="Refund follow-up required",
                    summary=f"{_label(row['customer_name'])} · {_clean(row['service_name'], 60)} · "
                            "review in the owner payment workflow",
                    created_at=row["settled_at"] or row["created_at"], resource_id=row["id"],
                    action="refund_owner_workflow",
                ))

            # Owner-targeted reminder jobs remain durable after delivery.  Keep
            # recent sent notices briefly so a busy owner can see what changed;
            # pending jobs remain until the scheduler settles them.
            reminders = await con.fetch(
                "SELECT r.id::text AS id,r.template_name,r.status,r.run_at,r.created_at,r.sent_at,"
                "b.id::text AS booking_id,c.name AS customer_name,sv.name AS service_name "
                "FROM reminder_jobs r JOIN salons s ON s.id=r.salon_id "
                "JOIN bookings b ON b.id=r.booking_id AND b.salon_id=r.salon_id "
                "JOIN clients c ON c.id=b.client_id AND c.salon_id=b.salon_id "
                "JOIN services sv ON sv.id=b.service_id AND sv.salon_id=b.salon_id "
                "WHERE r.salon_id=$1::uuid AND r.wa_id=s.owner_wa_id "
                "AND r.template_name IN ('owner_booking_cancelled','owner_reminder_soon') "
                "AND (r.status IN ('pending','failed') OR r.sent_at > now()-interval '24 hours') "
                "ORDER BY COALESCE(r.run_at,r.created_at),r.id LIMIT 102",
                salon_id,
            )
            for row in reminders:
                cancelled = row["template_name"] == "owner_booking_cancelled"
                items.append(_item(
                    attention_id=f"reminder:{row['id']}", kind="booking_update", priority="normal",
                    state=row["status"],
                    title="Booking cancelled" if cancelled else "Appointment approaching",
                    summary=f"{_label(row['customer_name'])} · {_clean(row['service_name'], 60)}",
                    created_at=row["created_at"], resource_id=row["booking_id"],
                    action="view_booking_owner_workflow",
                ))

            bookings = await con.fetch(
                "SELECT b.id::text AS id,b.created_at,b.starts_at,c.name AS customer_name,"
                "sv.name AS service_name,st.name AS staff_name,"
                "COALESCE(p.status,'none') AS payment_status "
                "FROM bookings b JOIN clients c ON c.id=b.client_id AND c.salon_id=b.salon_id "
                "JOIN services sv ON sv.id=b.service_id AND sv.salon_id=b.salon_id "
                "JOIN staff st ON st.id=b.staff_id AND st.salon_id=b.salon_id "
                "LEFT JOIN LATERAL (SELECT status FROM payments WHERE booking_id=b.id "
                "ORDER BY created_at DESC LIMIT 1) p ON true "
                "WHERE b.salon_id=$1::uuid AND b.status='confirmed' "
                "AND b.created_at > now()-interval '24 hours' "
                "ORDER BY b.created_at DESC,b.id DESC LIMIT 51",
                salon_id,
            )
            for row in bookings:
                paid = row["payment_status"] == "paid"
                items.append(_item(
                    attention_id=f"booking:{row['id']}", kind="new_booking", priority="normal", state="confirmed",
                    title="New booking confirmed",
                    summary=f"{_label(row['customer_name'])} · {_clean(row['service_name'], 60)} · "
                            f"{_clean(row['staff_name'], 40)} · {'deposit recorded' if paid else 'no deposit'}",
                    created_at=row["created_at"], resource_id=row["id"], action="view_booking_owner_workflow",
                ))

            # A deposit may settle after the booking was created (and after
            # the 24-hour new-booking window).  announced_at is the durable
            # marker used by the existing payment announcer, so retain that
            # owner-facing event independently of the booking query above.
            paid_announcements = await con.fetch(
                "SELECT p.id::text AS payment_id,p.announced_at,p.created_at,b.id::text AS booking_id,"
                "c.name AS customer_name,sv.name AS service_name,st.name AS staff_name "
                "FROM payments p JOIN bookings b ON b.id=p.booking_id AND b.salon_id=p.salon_id "
                "JOIN clients c ON c.id=b.client_id AND c.salon_id=b.salon_id "
                "JOIN services sv ON sv.id=b.service_id AND sv.salon_id=b.salon_id "
                "JOIN staff st ON st.id=b.staff_id AND st.salon_id=b.salon_id "
                "WHERE p.salon_id=$1::uuid AND p.status='paid' AND p.announced_at > now()-interval '24 hours' "
                "ORDER BY p.announced_at DESC,p.id DESC LIMIT 51",
                salon_id,
            )
            for row in paid_announcements:
                items.append(_item(
                    attention_id=f"paid-booking:{row['payment_id']}", kind="new_booking", priority="normal",
                    state="confirmed", title="Deposit received",
                    summary=f"{_label(row['customer_name'])} · {_clean(row['service_name'], 60)} · "
                            f"{_clean(row['staff_name'], 40)} · booking confirmed",
                    created_at=row["announced_at"] or row["created_at"], resource_id=row["booking_id"],
                    action="view_booking_owner_workflow",
                ))

            # Owner lateness announcements are backed by the durable delay row
            # even though the original owner reply is a chat message.
            delays = await con.fetch(
                "SELECT d.id::text AS id,d.day,d.minutes,d.created_at,st.name AS staff_name "
                "FROM delays d JOIN staff st ON st.id=d.staff_id AND st.salon_id=d.salon_id "
                "WHERE d.salon_id=$1::uuid AND d.day BETWEEN (now() AT TIME ZONE $2)::date-1 "
                "AND (now() AT TIME ZONE $2)::date+1 ORDER BY d.created_at DESC,d.id DESC LIMIT 51",
                salon_id, salon.get("timezone") or "UTC",
            )
            for row in delays:
                items.append(_item(
                    attention_id=f"delay:{row['id']}", kind="schedule_change", priority="normal", state="active",
                    title="Schedule changed",
                    summary=f"{_clean(row['staff_name'], 40)} · remaining bookings moved by {int(row['minutes'])} minutes",
                    created_at=row["created_at"], resource_id=row["id"], action="view_schedule_owner_workflow",
                ))

            feedback = await con.fetch(
                "SELECT v.booking_id::text AS id,v.answered_at,v.attended,v.feedback,c.name AS customer_name,"
                "sv.name AS service_name FROM visit_checks v "
                "JOIN bookings b ON b.id=v.booking_id AND b.salon_id=v.salon_id "
                "JOIN clients c ON c.id=v.client_id AND c.salon_id=v.salon_id "
                "JOIN services sv ON sv.id=b.service_id AND sv.salon_id=b.salon_id "
                "WHERE v.salon_id=$1::uuid AND (v.feedback IS NOT NULL OR v.attended=false) "
                "AND b.status IN ('confirmed','cancelled','no_show') "
                "AND v.answered_at > now()-interval '7 days' ORDER BY v.answered_at DESC LIMIT 51",
                salon_id,
            )
            for row in feedback:
                missed = row["attended"] is False and not row["feedback"]
                items.append(_item(
                    attention_id=f"feedback:{row['id']}",
                    kind="service_issue" if missed else "customer_feedback",
                    priority="high" if missed else "normal", state="new",
                    title="Customer says they were not attended" if missed else "Customer feedback received",
                    summary=(f"{_label(row['customer_name'])} · {_clean(row['service_name'], 60)} · "
                             "contact the customer to rebook or cancel" if missed else
                             f"{_label(row['customer_name'])} · {_clean(row['service_name'], 60)} · "
                             f"{_clean(row['feedback'], 130)}"),
                    created_at=row["answered_at"], resource_id=row["id"],
                    action="view_feedback_owner_workflow",
                ))

            # Inactivity state is durable on the salon and needs no separate
            # notification row.  It is shown until a booking clears the state.
            if salon.get("inactivity_final_at"):
                items.append(_item(
                    attention_id=f"lifecycle:{salon['id']}:final", kind="lifecycle_warning", priority="high",
                    state="final_warning", title="Business inactivity warning",
                    summary="The business is scheduled for deletion unless the owner keeps it active.",
                    created_at=salon["inactivity_final_at"], resource_id=salon["id"], action="keep_business_owner_workflow",
                ))
            elif salon.get("inactivity_warned_at"):
                items.append(_item(
                    attention_id=f"lifecycle:{salon['id']}:warning", kind="lifecycle_warning", priority="normal",
                    state="warning", title="Business inactivity warning",
                    summary="No recent bookings were recorded. Review the business in the owner workflow.",
                    created_at=salon["inactivity_warned_at"], resource_id=salon["id"], action="keep_business_owner_workflow",
                ))

            # The daily summary is derived from the same durable records used
            # by the existing Telegram/WhatsApp summary.  Only its counts are
            # exposed here, never payment references or customer contacts.
            try:
                local_today = datetime.now(ZoneInfo(salon.get("timezone") or "UTC")).date()
            except Exception:
                local_today = datetime.now(timezone.utc).date()
            if salon.get("last_summary_on") == local_today:
                today_payments = await con.fetchrow(
                    "SELECT COALESCE(sum(amount_kes),0) AS total,count(*)::int AS count FROM payments "
                    "WHERE salon_id=$1::uuid AND status='paid' AND (settled_at AT TIME ZONE $3)::date=$2",
                    salon_id, local_today, salon.get("timezone") or "UTC",
                )
                tomorrow_bookings = await con.fetchval(
                    "SELECT count(*)::int FROM bookings WHERE salon_id=$1::uuid AND status='confirmed' "
                    "AND (starts_at AT TIME ZONE $3)::date=($2::date+1)", salon_id, local_today,
                    salon.get("timezone") or "UTC",
                )
                items.append(_item(
                    attention_id=f"summary:{salon['id']}:{local_today.isoformat()}", kind="daily_summary", priority="low",
                    state="available", title="Daily owner summary",
                    summary=f"{int(today_payments['count'])} deposits recorded today · {int(tomorrow_bookings or 0)} bookings tomorrow",
                    created_at=salon["last_summary_on"], resource_id=salon["id"], action="view_summary_owner_workflow",
                ))

            # A paid booking can appear in both the recent-booking and
            # payment-announcement reads.  Keep one stable item per kind and
            # resource so the owner does not see a duplicate alert.
            unique: dict[tuple[str, str], dict[str, Any]] = {}
            for item in items:
                key = (item["kind"], item["resource_id"] or item["attention_id"])
                prior = unique.get(key)
                item_time = item["created_at"] or datetime.min.replace(tzinfo=timezone.utc)
                prior_time = (prior["created_at"] or datetime.min.replace(tzinfo=timezone.utc)) if prior else None
                if prior is None or item_time > prior_time:
                    unique[key] = item
            items = list(unique.values())

            items.sort(key=lambda value: (
                _PRIORITY_ORDER.get(value["priority"], 9),
                value["expires_at"] is None,
                value["expires_at"] or datetime.max.replace(tzinfo=timezone.utc),
                -(value["created_at"].timestamp() if value["created_at"] else 0),
                value["attention_id"],
            ))
            return items[: limit + 1]
