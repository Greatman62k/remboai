# RemboAI

RemboAI is a conversational booking receptionist for barbershops and salons, with workflows designed for Kenyan operators. Customers can ask about services and availability, book appointments, manage bookings, and receive reminders through messaging channels. Salon owners configure their business and handle exceptions through chat.

The product combines model-assisted language understanding with application code that validates availability, booking changes, payment state, and owner actions. The repository contains the backend, database migrations, deployment configuration, and a static product site.

## What it does

- Handles salon registration, service menus, stylist schedules, opening hours, booking requests, cancellations, and rescheduling.
- Uses database-backed holds and constraints to protect appointment availability from conflicting bookings.
- Supports reminders, waitlists, owner summaries, and owner commands through messaging channels.
- Supports M-PESA Daraja STK prompts when configured. Manual M-PESA and Airtel Money instructions are also supported; the owner reviews and confirms manual payment claims.
- Accepts voice notes and hairstyle or payment images through the configured speech and vision integrations when those services are configured.
- Routes sensitive, ambiguous, or explicitly escalated conversations to the salon owner.
- Provides a RemboAI Glance API for paired owners to inspect and manage escalated cases from a separate Even Realities client.

The application is configurable for English, Swahili, and Sheng conversations. External integrations require their own credentials and provider-side setup.

## System architecture

```text
Telegram / WhatsApp webhooks
              |
       FastAPI application
        /      |       \
 PostgreSQL   Redis    model / ASR providers
        |               |
 booking, payment,   language, vision,
 handoff, outbox     transcription
```

The application owns booking and payment state. The model can interpret a message and propose a tool action; application services validate and execute business changes. When model calls are unavailable or rejected, deterministic flows remain available for supported tasks. PostgreSQL stores business data and enforces important invariants. Redis supports rate limits and short-lived coordination. Caddy handles HTTPS in the supplied deployment configuration.

## Channels and integration status

| Integration | Code in this repository | External requirements and limits |
|---|---|---|
| Telegram | Bot API adapter, webhook and polling modes, buttons, media handling, and owner commands | Requires a bot token. A configured bot or live service is not implied by the code being present. |
| WhatsApp | WhatsApp Cloud API webhook, message sending, media, template and delivery-status handling | Requires a configured WhatsApp Business account, approved templates where required, and valid Meta credentials. Live production readiness is not claimed here. |
| M-PESA | Daraja STK initiation, callback handling, reconciliation, and payment-state checks | The example configuration defaults to sandbox. Production STK requires Safaricom approval and production credentials for each participating business. |
| Manual M-PESA and Airtel Money | Salon-specific till or phone destinations, customer instructions, and owner-reviewed payment claims | The customer sends funds directly to the salon. RemboAI does not independently verify a manual transfer; an owner confirms the claim. No Airtel payment API integration is provided. |
| RemboAI Glance | Backend pairing, session, case, draft, reply, and resolution APIs | The separate Even Realities client and real staging workflow still require packaged WebView and HTTPS staging acceptance. See [Glance status](#remboai-glance). |

These rows describe implemented code paths, not the current state of any provider account, deployment, or business verification.

## AI and media integrations

The default model settings in [`app/config.py`](app/config.py) point to Meta's Model API and the `muse-spark-1.3` model. `AGENT_MODE` controls the tool-using assistant; when enabled, model calls are bounded by application policy and validated tools. The example `.env` enables agent mode, while the application setting defaults to off when the variable is omitted. Optional fallback model settings are available through `LLM_FALLBACK_*`; no fallback is configured by default.

Voice transcription uses a separately configured ASR endpoint. [`deploy/modal_asr.py`](deploy/modal_asr.py) provides a Modal deployment for Meta's open-source Omnilingual ASR model. Photo and payment-receipt interpretation use the configured vision-capable model endpoint. Model, ASR, and messaging credentials remain server-side.

Model output is not authoritative booking or payment evidence. Application code matches suggested services against the salon menu, checks availability, applies booking rules, and leaves manual payment confirmation to the owner. RemboAI does not claim that a model can confirm a transfer or settle money.

## Booking, payment, and owner escalation

For a booking, the service resolves the salon and requested service, checks the schedule, and creates a time-limited hold or booking according to the configured payment flow. PostgreSQL prevents overlapping active bookings for a stylist. Cancellations can release a slot to the waitlist. Reminder and payment workers handle asynchronous tasks.

When a conversation needs a person, RemboAI records a salon-scoped handoff and pauses the ordinary assistant flow for that case. The owner can review the customer-visible context and respond through the existing owner channel. Glance adds a paired owner API for the same case workflow. Approved Glance replies are persisted with an outbox record before dispatch; provider submission is not treated as delivery confirmation. Resolution uses the shared case service and does not change payment state.

## RemboAI Glance

The backend implementation is in this repository. Migrations 047–049 add pairing/session security, draft/approval/idempotency and safe-resolution data, plus transcription rate limiting. The API is registered under `/api/v1/glance`; pairing codes are generated through Telegram owner interactions, while bearer sessions are validated and salon ownership is rechecked on requests.

Production Compose defaults to `GLANCE_ENABLED=false` and can opt in through the deployment environment after the pairing HMAC keyring and exact Even Hub WebView origin are configured. The application fails startup if required Glance security settings are missing. A separate staging Compose definition, staging configuration example, and synthetic seeder are provided, but this checkout does not have a configured staging host. Packaged Even Hub WebView CORS and the end-to-end HTTPS workflow have not been accepted against a live staging deployment. The frontend is maintained in a separate `remboai-glance` repository and is not bundled here.

Read [the Day 1 contract findings](docs/glance-day1-findings.md), [Glance display copy](docs/glance.md), [staging and deployment instructions](docs/DEPLOY.md), and the [complete v6 workflow](RemboAI_Glance_End-to-End_Workflow_v6.md). The v6 workflow includes Even Realities project planning and is retained as a current project document; it is not evidence that those deployment or packaged-client checks have passed.

## Technology

- Python 3.13, FastAPI, Pydantic, and asynchronous HTTP clients.
- PostgreSQL 16 with ordered SQL migrations and `asyncpg`.
- Redis 7 for short-lived coordination and rate limiting.
- Docker Compose for local, regression-test, production, and isolated staging configurations.
- Caddy for HTTPS in the deployment Compose files.
- Meta Model API, configurable compatible model endpoints, Telegram Bot API, WhatsApp Cloud API, Safaricom Daraja, and an optional Modal ASR endpoint.

## Repository layout

| Path | Purpose |
|---|---|
| `app/` | FastAPI application, booking and payment services, channel adapters, AI integrations, handoffs, and Glance API |
| `migrations/` | Ordered PostgreSQL schema migrations; the current sequence ends at 048 |
| `tests/` | Unit, API, PostgreSQL, Redis, and workflow regression tests |
| `scripts/` | Diagnostics, synthetic seeding, evaluation, and maintenance tools |
| `deploy/` | Production and staging Compose files, Caddy configuration, backups, and ASR deployment |
| `config/` | Assistant policy configuration |
| `docs/` | Architecture, operational, channel, evaluation, and Glance documentation |
| `site/` | Static product website |

## Development setup

Requirements: Docker with the Compose plugin. Python 3.13 is used by the application image. For a local application instance, copy the example environment file and set the database and Redis URLs. Provider credentials are needed only for the integrations you want to exercise.

```bash
cp .env.example .env
docker compose up --build -d
docker compose ps
docker compose logs -f app
```

The root Compose file starts PostgreSQL, Redis, and the API. The application applies pending migrations during startup. The local database and Redis use named volumes; `docker compose down` stops the services while preserving their data. Set at least `LLM_API_KEY` to use the configured model endpoint. Add channel credentials only for the channels being exercised.

The optional diagnostics command checks configured services and may make provider requests:

```bash
docker compose exec app python scripts/doctor.py
```

### Regression tests

The Docker test Compose file starts isolated PostgreSQL and Redis services and runs the test suite against them. It does not use the application `.env` or production databases.

```bash
if docker compose -f docker-compose.test.yml -p remboai-tests up --build --abort-on-container-exit --exit-code-from test test; then
  test_status=0
else
  test_status=$?
fi
docker compose -f docker-compose.test.yml -p remboai-tests down
exit "$test_status"
```

The current suite covers service, database, API, Telegram, WhatsApp parsing, payment, handoff, Glance, and staging-seeder behavior. Test totals change as the project evolves; use the output from the run you perform rather than a fixed badge count.

## Configuration

The root [`.env.example`](.env.example) lists application settings. Common variables include:

| Variable | Purpose |
|---|---|
| `DATABASE_URL`, `REDIS_URL` | Backend-only PostgreSQL and Redis connection strings |
| `LLM_BASE_URL`, `LLM_API_KEY`, `LLM_MODEL`, `AGENT_MODE` | Model endpoint and assistant mode |
| `LLM_FALLBACK_*` | Optional secondary model endpoint; empty by default |
| `TELEGRAM_BOT_TOKEN`, `TELEGRAM_MODE`, `TELEGRAM_WEBHOOK_SECRET` | Telegram channel configuration |
| `WA_APP_SECRET`, `WA_VERIFY_TOKEN`, `WA_ACCESS_TOKEN`, `GRAPH_VERSION` | WhatsApp Cloud API configuration |
| `ASR_URL`, `ASR_TOKEN`, `ASR_PROXY_KEY`, `ASR_PROXY_SECRET` | Optional speech-service endpoint and credentials |
| `MPESA_ENV`, `MPESA_CONSUMER_KEY`, `MPESA_CONSUMER_SECRET`, `MPESA_PASSKEY` | Daraja environment and credentials |
| `TOKEN_ENCRYPTION_KEY`, `OPS_API_KEY` | Sensitive token encryption and operations access |

Never commit `.env` files or put provider credentials, database URLs, operations keys, or Glance HMAC secrets in the client or static site. Keep Glance pairing keys and exact CORS origins in the private server environment. The staging-only environment template is [`deploy/.env.glance-staging.example`](deploy/.env.glance-staging.example).

## Database and migrations

On application startup, [`app/db.py`](app/db.py) acquires a migration lock, applies unapplied `migrations/*.sql` files in filename order, and records each completed migration in `schema_migrations`. Each migration runs in a transaction. Do not edit or reorder an already-applied migration; add a new numbered migration for schema changes. The migration runner currently includes migrations 001 through 048.

## Deployment and operations

The production stack is defined in [`deploy/docker-compose.prod.yml`](deploy/docker-compose.prod.yml). It runs Caddy, the API, PostgreSQL, and Redis on a private Compose network; only Caddy publishes HTTP and HTTPS. PostgreSQL and Redis are not published to the host. Glance defaults off and can be enabled through the private environment file; the application validates its required keyring and exact CORS origin before startup.

See [`docs/DEPLOY.md`](docs/DEPLOY.md) and [`deploy/PRODUCTION.md`](deploy/PRODUCTION.md) before deploying. Production operation requires a secured host, DNS, firewall rules, backups, private environment files, and provider-side webhook configuration. The separate [`deploy/docker-compose.staging.yml`](deploy/docker-compose.staging.yml) uses its own database and Redis services and must run only on a dedicated staging host with synthetic records and staging-only credentials. No staging hostname is configured in this repository.

## Security and data handling

- PostgreSQL and Redis are backend services and are not exposed by the supplied production Compose network.
- Webhook signatures and secrets are checked by the server. Operational endpoints require the operations key and must not be exposed to the Glance client.
- Booking, payment, and handoff queries use salon-scoped records and database constraints for critical invariants.
- Glance uses short-lived pairing codes, HMAC-protected pairing-code storage, opaque hashed bearer sessions, and current owner-to-salon checks. Its API has its own authorization; CORS is not authentication.
- Customer messages sent through model-assisted flows are processed by the configured model provider. Voice and image requests are sent to their configured services. Operational trace and record-retention policies vary by record type and configuration; review them and provider terms before operating on real customer data.
- Keep credentials in private server-side environment files or a secret manager. Do not put them in source, demo fixtures, the static website, or a frontend bundle.

## Current limits and next work

- WhatsApp code requires a verified, configured business account and any needed message templates; live acceptance is deployment-specific.
- M-PESA STK defaults to sandbox. Production STK depends on provider approval. Manual M-PESA and Airtel Money claims require owner verification.
- This repository does not announce a managed hosting service or publish hosted-service pricing.
- Glance backend write workflows and synthetic staging tools are present, while a real isolated HTTPS staging run and packaged WebView CORS acceptance remain outstanding.
- Glance transcription is implemented through the existing ASR service; physical G2 microphone, real provider, packaged WebView CORS, and locked-phone acceptance remain unverified.
- Evaluation corpora in [`docs/AGENT_EVAL.md`](docs/AGENT_EVAL.md) and [`docs/AGENT_EVAL_V2.md`](docs/AGENT_EVAL_V2.md) are engineering checks, not broad production quality guarantees.

## Development and contribution

Follow the existing service and migration patterns, add focused regression coverage for behavior changes, and run the relevant Docker/PostgreSQL/Redis tests before proposing a change. The product and integration status in documentation should distinguish source-code support from provider configuration and live deployment verification.

## License

RemboAI is distributed under the MIT License; see [`LICENSE`](LICENSE).
