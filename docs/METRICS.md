# Metrics

Prometheus metrics are exposed at `GET /metrics` (ops-auth or 127.0.0.1).

```
curl -H "X-Ops-Key: $OPS_API_KEY" https://$DOMAIN/metrics
```

## Counters
- `rembo_http_requests_total{method,route,status}`
- `rembo_bookings_created_total{status}`
- `rembo_bookings_confirmed_total`
- `rembo_payments_total{status,via}`
- `rembo_reminders_queued_total` / `rembo_reminders_sent_total{result}`
- `rembo_llm_calls_total{source}`

## Gauges
- `rembo_webhook_queue_depth`

## Histograms
- `rembo_http_duration_seconds{route}`

## Dashboard

No Grafana dashboard ships yet. Prometheus scrape:
```yaml
scrape_configs:
  - job_name: rembo
    metrics_path: /metrics
    bearer_token: $OPS_API_KEY
    static_configs: [{targets: ["app:8080"]}]
```
