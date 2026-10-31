# RemboAI

![Python 3.13](https://img.shields.io/badge/python-3.13-blue)
![Tests](https://img.shields.io/badge/tests-327_passing-brightgreen)
![Licence MIT](https://img.shields.io/badge/licence-MIT-lightgrey)

A WhatsApp and Telegram receptionist for Kenyan salons and barbershops. Customers write the way they talk - English, Swahili, or Sheng, typed or voice note - and get a held slot, an M-Pesa deposit prompt, and a confirmation. Built for the Meta Global AI Developer Hackathon, problem statement **Build for Your World**.

> **Prior work and what's new.** Before the event I privately prototyped an earlier version of this idea (not submitted, not public,
> and none of its code is in this repository), and I set up my WhatsApp Business Cloud API and Daraja (M-Pesa) sandbox accounts.
> This repository is a fresh implementation, written Oct 27 - Nov 4, 2026. What carried over is the idea and the design.
> Every file here was written during the event; see [CHANGELOG.md](CHANGELOG.md).

## The problem in one picture

No-shows bleed small salons dry: an empty chair earns nothing, and a paper book can't take deposits. RemboAI turns the chat app already on every customer's phone into a receptionist that holds slots and collects M-Pesa deposits straight to the salon's own till - the salon never chases cash again.

```
Customer (Sheng, voice note):  "Niaje, nataka fade kesho asubuhi"
RemboAI:                       "Fade iko Tue 10:00 na Brian. Deposit KES 200?"
Customer (M-Pesa prompt):      [enters PIN]
RemboAI:                       "Done: Fade Tue 10:00 with Brian. Receipt QAB1CD2EF3."
Owner (same minute):           "Deposit KES 200 from 2547... for Fade Tue 10:00."
```

That exchange touches three Meta models (speech, language, vision-ready), a Postgres exclusion constraint so the slot can never double-book, and an idempotent money path - all in about a minute.

## How it works

```mermaid
flowchart LR
    WA["WhatsApp"] <--> APP["FastAPI app"]
    TG["Telegram"] <--> APP
    APP <--> DB[("Postgres")]
    APP <--> RD[("Redis")]
    APP --> MPESA["M-Pesa Daraja"]
    APP --> META["Meta Model API"]
    APP --> OPEN["Muse Spark Open fallback"]
    APP --> ASR["Omnilingual ASR"]
```

One central WhatsApp number and one Telegram bot serve any number of salons. Invite links and QR stickers carry each salon's slug; sender binding keeps every salon's data isolated. Owners register their salon over chat in minutes (name, type, stylists, priced services, hours, deposit) and then run it over chat (bookings, cancellations, moves, away-days, deposits, daily summary).

A booking, end to end:

```mermaid
sequenceDiagram
    participant C as Customer
    participant B as RemboAI
    participant M as M-Pesa
    C->>B: nataka fade kesho asubuhi
    B->>B: hold the slot (DB lock)
    B->>C: Slot held, deposit requested
    C->>B: M-Pesa phone number
    B->>M: STK push to the salon till
    M->>B: paid callback
    B->>C: Confirmed plus receipt
    B->>B: 24h and 2h reminders
```

## Where Meta AI fires

Deterministic code runs the business; models handle human messiness. Keyword rules resolve ~97% of a 40-message EN/SW corpus in under a millisecond, and the model takes only what survives them:

```mermaid
flowchart TD
    MSG["customer message"] --> RULES{"rules certain?"}
    RULES -->|yes| ACT["act, no model call"]
    RULES -->|no| MOD["Muse Spark via Meta API"]
    MOD -->|ok| ACT
    MOD -->|fails| FALL["Muse Spark Open fallback"]
    FALL -->|ok| ACT
    FALL -->|fails| KEY["keyword rules"]
    KEY --> ACT
```

- **Language** - Muse Spark 1.3 extracts intent, service, day, and time from free text in English, Swahili, and Sheng. Output is schema-coerced and clamped to the salon menu; the model never books, charges, or messages freely.
- **Voice** - customer voice notes go to Omnilingual ASR (Meta open-source speech, 1600+ languages), self-hosted; only the transcript enters the conversation.
- **Vision** - a hairstyle photo resolves to the salon's own service list; price and duration always come from the menu, never the model.
- **Proof** - every model call is logged (`model_calls`), `/model-info` reports the live backend for verification, `scripts/eval.py` scores each backend against the keyword baseline, and per-salon daily allowances plus a circuit breaker mean a model outage degrades to rules instead of dying.

## Why it holds up

- **327 tests**, including full booking, payment, guard, and lifecycle flows against real Postgres.
- **Money safety**: deposits go to the salon's own till, never through RemboAI. Callbacks match by ID and are kept verbatim; settlement is idempotent; missed callbacks are recovered by status query; anything unpayable parks as `needs_refund` and tells both sides - never dropped silently.
- **No double bookings**: slot holds combine an advisory lock with a Postgres exclusion constraint.
- **No double handling**: every inbound message is queued durably and retried from a snapshot, so redeliveries and crashes repeat nothing.
- **Secrets stay secret**: webhooks verified before parsing, constant-time compares, redacted logs, masked numbers, encrypted provider tokens.

## Run locally

```bash
cp .env.example .env          # fill in your own values, then chmod 600 .env
docker compose up --build -d  # Postgres + Redis + the app on :8080
docker compose -f docker-compose.test.yml -p remboai-test up -d --wait
docker run --rm --network remboai-test_default -v $PWD:/srv -w /srv --user root \
  -e TEST_DATABASE_URL=postgresql://rembo_test:rembo_test@test-db:5432/rembo_test \
  remboai-app python -m pytest -q
```

`docker compose exec app python scripts/doctor.py` checks the whole set-up (WhatsApp token, models, M-Pesa, public URL) and says what to fix.
`scripts/eval.py` scores each model backend against the keyword baseline on the labeled corpus.

## Verify

- Suite: the `docker run ... pytest -q` line above is the gate. It must end green (352 passed at last check: booking, waitlist, Still-coming, onboarding, guards, lifecycle).
- Health: `docker compose exec -T app python scripts/doctor.py` must report ready before any demo.
- Models: `python scripts/eval.py` with no keys scores the keyword baseline (40 messages: 98 percent intent, 100 percent language). With keys, `EVAL_BACKENDS` / `EVAL_API_KEYS` add the live-backend rows to the same table for the demo slide.
- Waitlist: join twice (second join says already), cancel the booked slot, tap Yes on the offer, and confirm the booking lands on the freed slot.
- Still-coming: with a `reminder_3h` template approved, a booking 4 hours out gets Keep/Cancel buttons; Keep protects it from the silent release, Cancel and silence both hand the slot to the waitlist.

## Layout

- `app/channels/` WhatsApp and Telegram wire formats, one sender, invite links and QR stickers; both produce the same message
- `app/ai/` the model client and keyword fallback, voice notes to text, hairstyle photos to services
- `app/db.py` connection pool and a small migration runner (`migrations/*.sql`, applied in order, once)
- `app/main.py` the web app: `/health`, `/webhook`, `/mpesa/callback/<secret>`
- `app/conversation.py` routing (deep link, binding, business picker) and the conversation; `app/availability.py`, `app/bookings.py` the calendar; `app/messages.py` every message, EN/SW
- `app/onboarding_chat.py` chat setup for owners; `app/binding.py` slugs and sender bindings
- `app/mpesa.py` Daraja client; `app/payments.py` deposits: request, idempotent settlement, refund parking, reconciliation sweeper
- `app/lexicon.json` + `app/lexicon.py` English/Swahili/Sheng vocabulary as data; see [docs/LEXICON.md](docs/LEXICON.md)
- `app/screen.py`, `app/guard.py`, `app/logs.py` screening, safety limits, and secret-safe logging
- `app/owner.py` the owner's chat commands, time off, daily summary; see [docs/TELEGRAM.md](docs/TELEGRAM.md) for the Telegram channel
- `scripts/seed_salon.py` create a salon with stylists and services; `scripts/invite_qr.py` print its QR sticker
- `deploy/` production compose, Caddy, backups, and the self-hosted speech service

## Privacy

Voice notes and photos are processed in memory and never stored (voice by our own speech service, photos by Muse Spark); only the text is kept in the conversation. While the Meta Model API runs on free credits, Meta may use message content for product improvement, so tell customers and salons.

## Licence

MIT, see [LICENSE](LICENSE).
