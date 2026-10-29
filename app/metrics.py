from __future__ import annotations

import time
from prometheus_client import Counter, Gauge, Histogram, generate_latest, CONTENT_TYPE_LATEST

http_requests_total = Counter("rembo_http_requests_total", "HTTP requests", ["method", "route", "status"])
http_duration_seconds = Histogram("rembo_http_duration_seconds", "HTTP duration", ["route"])
bookings_created = Counter("rembo_bookings_created_total", "Bookings created", ["status"])
bookings_confirmed = Counter("rembo_bookings_confirmed_total", "Bookings confirmed")
payments_total = Counter("rembo_payments_total", "Payments", ["status", "via"])
webhook_queue_depth = Gauge("rembo_webhook_queue_depth", "Webhook queue depth")
reminders_queued = Counter("rembo_reminders_queued_total", "Reminders queued")
reminders_sent = Counter("rembo_reminders_sent_total", "Reminders sent", ["result"])
llm_calls_total = Counter("rembo_llm_calls_total", "LLM calls", ["source"])

def observe_request(method: str, route: str, status: int, duration: float):
    http_requests_total.labels(method, route, str(status)).inc()
    http_duration_seconds.labels(route).observe(duration)

def render():
    return generate_latest(), CONTENT_TYPE_LATEST

async def update_queue_depth(pool):
    try:
        n = await pool.fetchval("SELECT count(*) FROM webhook_jobs WHERE status IN ('pending','processing')")
        webhook_queue_depth.set(int(n or 0))
    except Exception:
        pass
