<div align="center">

# RemboAI

**The receptionist that never misses a booking.**
A WhatsApp and Telegram assistant for Kenyan salons and barbershops that holds the slot, collects the M-Pesa deposit, and confirms, in English, Swahili, or Sheng, typed or by voice note.

![Python 3.13](https://img.shields.io/badge/python-3.13-blue)
![Tests](https://img.shields.io/badge/tests-380_passing-brightgreen)
![Postgres](https://img.shields.io/badge/postgres-exclusion_constraints-336791)
![M-Pesa](https://img.shields.io/badge/M--Pesa-Daraja_STK_push-00a651)
![Meta AI](https://img.shields.io/badge/Meta-Muse_Spark_%C2%B7_Omnilingual_ASR-0866ff)
![Licence MIT](https://img.shields.io/badge/licence-MIT-lightgrey)

[See it work](#see-it-work) ·
[How it works](#how-it-works) ·
[Where Meta AI fires](#where-meta-ai-fires) ·
[Why it holds up](#why-it-holds-up) ·
[Run locally](#run-locally) ·
[Layout](#layout)

Built for the **Meta Global AI Developer Hackathon** · problem statement **Build for Your World**

</div>

<!-- Add a 20-second screen recording here for maximum impact:
<p align="center"><img src="docs/assets/demo.gif" width="320" alt="RemboAI booking a fade over WhatsApp"></p> -->

> [!NOTE]
> **Prior work and what's new.** Before the event I privately prototyped an earlier version of this idea (not submitted, not public, and none of its code is in this repository), and I set up my WhatsApp Business Cloud API and Daraja (M-Pesa) sandbox accounts. This repository is a fresh implementation, written Oct 27 - Nov 4, 2026. What carried over is the idea and the design. Every file here was written during the event; see [CHANGELOG.md](CHANGELOG.md).

---

## Why I built this

There's a barber in my town who gives the best fades around, and everyone knows it. His shop is always full of walk-ins.

I would go by, see the crowd, and leave, because I had work to get back to. I did that again and again until it had been almost three months since my last fade.

He lost a regular, and I lost my fade. Nobody did anything wrong. There was just no way to say "I'll be there at 10:00, keep my chair."

RemboAI is that way. A customer messages the chat app already on their phone, in the language they already speak, and gets a held slot. A small M-Pesa deposit goes straight to the barber's own till, so the slot is worth keeping. The owner never has to stop cutting to answer a message.

## See it work

One voice note. About a minute. Three Meta models, a database that cannot double-book, and a money path that cannot double-charge.

```text
Customer (Sheng, voice note)   "Niaje, nataka fade kesho asubuhi"
RemboAI                        "Fade iko Tue 10:00 na Brian. Deposit KES 200?"
Customer (M-Pesa prompt)       [enters PIN]
RemboAI                        "Done: Fade Tue 10:00 with Brian. Receipt QAB1CD2EF3."
Owner (same minute)            "Deposit KES 200 from 2547... for Fade Tue 10:00."
```

## The problem

Two sides of the same chair. Customers walk away from a crowded shop, and owners lose money to no-shows. An empty chair earns nothing, and a paper book can't take deposits.

| Without RemboAI | With RemboAI |
|---|---|
| Customers turn up, see a crowd, and leave | Customers message the chat app already on their phone and keep a chair |
| A paper book that can double-book | A held slot enforced by Postgres, so two people can't get the same chair |
| Owner chases deposits, or never asks | M-Pesa deposit prompt, paid **straight to the salon's own till** |
| No-shows discovered at the appointment time | 24h and 2h reminders, plus a Keep/Cancel check that frees the slot for the waitlist |
| Owner is receptionist after closing | Owner runs everything over chat: bookings, moves, away-days, daily summary |

## At a glance

| | |
|---|---|
| **Languages** | English, Swahili, Sheng |
| **Input** | Typed text or voice note |
| **Channels** | WhatsApp and Telegram, one number and one bot for any number of salons |
| **Meta models** | Muse Spark 1.3 (tool-using receptionist, vision), `omniASR_LLM_1B_v2` (speech), Muse Spark Open (fallback) |
| **Conversation** | The model decides the next step through validated tools; code executes actions and verifies every binding fact |
| **Tests** | 380, including agent, booking, payment, guard, and lifecycle flows against real Postgres |

## How it works

One central WhatsApp number and one Telegram bot serve any number of salons. Invite links and QR stickers carry each salon's slug, and sender binding keeps every salon's data isolated. Owners register their salon over chat in minutes (name, type, stylists, priced services, hours, deposit) and then run it over chat.

<p align="center">
  <img src="docs/assets/architecture.svg" alt="Architecture: WhatsApp and Telegram connect to one FastAPI app backed by Postgres and Redis. The app calls M-Pesa Daraja, Muse Spark, Muse Spark Open, and a self-hosted Omnilingual ASR model." width="520">
</p>

### A booking, end to end

<p align="center">
  <img src="docs/assets/booking-flow.svg" alt="Booking sequence: customer message, speech to text and intent, slot hold, deposit offer, M-Pesa STK push to the salon's till, PIN prompt, paid callback, confirmation, owner notice, reminders." width="560">
</p>

### What each side gets

| Customers | Salon owners |
|---|---|
| Book in their own language, by text or voice note | Register the salon over chat in minutes |
| Send a hairstyle photo and get matched to a real service | Bookings, cancellations, moves, away-days, deposits, all by chat |
| Pay the deposit with an M-Pesa prompt, no app to install | Deposits land in their own till, never through RemboAI |
| Get a receipt, then 24h and 2h reminders | A daily summary, plus an instant note for every deposit |
| Join a waitlist and tap Yes when a slot frees up | Cancelled and unconfirmed slots go to the waitlist automatically |

## Where Meta AI fires

**The Meta model is the receptionist.** Muse Spark reads each message in the context of the conversation and the salon's real facts, decides what to do, and acts through tools. Code validates every tool call against the booking engine and checks the reply before the customer sees it.

```mermaid
flowchart TD
    MSG["customer message: text, voice note, or photo"] --> MOD["Muse Spark decides the next step"]
    MOD -->|tool call| TOOLS["find slots · reserve · view or cancel bookings · join waitlist · hand off"]
    TOOLS -->|validated by code| DB[("Postgres: holds, exclusion constraint, payments")]
    DB -->|result| MOD
    MOD -->|draft reply| CHECK{"grounded in salon facts?"}
    CHECK -->|yes| SEND["send model reply with code-written confirmations"]
    CHECK -->|no| TEMPL["send code-written reply from tool results"]
    MOD -.->|model down or budget spent| KEY["keyword flow fallback"]
```

| Capability | Model | What it does | What it can't do |
|---|---|---|---|
| **Conversation** | Muse Spark 1.3 | Understands English, Swahili, and Sheng; asks for missing details, searches real slots, and acts through booking tools | Set prices, mark payments as paid, choose an unoffered slot, or access another customer's booking |
| **Voice** | Omnilingual ASR, `omniASR_LLM_1B_v2` (Meta open-source speech, 1600+ languages, Apache 2.0), self-hosted | Turns voice notes into text | Store audio. Only the transcript enters the conversation |
| **Vision** | Muse Spark | Resolves a hairstyle photo to the salon's own service list | Set price or duration. Both always come from the menu, never the model |
| **Owner assistant** | Muse Spark 1.3 | Answers booking and deposit questions and handles owner-requested cancellations through validated tools | Change records outside the owner's salon |
| **Fallback** | Keyword flow | Answers when the model is unavailable, out of budget, or cannot produce a grounded reply | Replace the booking and payment code |

**Proof, not promises.** Chat text or transcripts, actual replies, channel send results, and matching tool/model evidence are available at the ops-key-protected `/ops/conversation-trace` for the configured agent retention period. Message content is retained as written; sender IDs in trace threads are masked. `/ops/agent-trace` remains available for model turns alone; model calls are logged in `model_calls`. `scripts/probe_tools.py` checks whether the configured host supports tool calling, and `scripts/agent_eval.py` scores the model on 33 challenging messages. Per-salon daily allowances and a circuit breaker mean a model outage falls back to the keyword flow.

## Why it holds up

Money and calendars are where small businesses get hurt, so those paths are built defensively.

| Risk | Guarantee |
|---|---|
| **Lost or misrouted money** | Deposits go to the salon's own till, never through RemboAI |
| **Double-charging** | Callbacks match by ID and are kept verbatim; settlement is idempotent |
| **Missed payment callbacks** | Recovered by status query and a reconciliation sweeper |
| **Payments that can't be completed** | Parked as `needs_refund` and both sides are told, never dropped silently |
| **Double bookings** | Slot holds combine an advisory lock with a Postgres exclusion constraint |
| **Duplicate or lost messages** | Every inbound message is queued durably and retried from a snapshot, so redeliveries and crashes repeat nothing |
| **Leaked secrets** | Webhooks verified before parsing, constant-time compares, redacted logs, masked numbers, encrypted provider tokens |
| **Ungrounded reply or model outage** | Replies with invented prices, times, links, phone numbers, or payment claims are rejected; code-written responses or the keyword flow take over |

## Run locally

```bash
cp .env.example .env          # fill in your own values, then chmod 600 .env
docker compose up --build -d  # Postgres + Redis + the app on :8080
```

Check the whole set-up (WhatsApp token, models, M-Pesa, public URL) and get told what to fix:

```bash
docker compose exec app python scripts/doctor.py
```

<details>
<summary><b>Run the test suite</b></summary>

<br>

```bash
docker compose -f docker-compose.test.yml -p remboai-test up -d --wait
docker run --rm --network remboai-test_default -v $PWD:/srv -w /srv --user root \
  -e TEST_DATABASE_URL=postgresql://rembo_test:rembo_test@test-db:5432/rembo_test \
  remboai-app python -m pytest -q
```

</details>

<details>
<summary><b>Verify before a demo</b></summary>

<br>

| Check | How | Expected |
|---|---|---|
| **Suite** | The `docker run ... pytest -q` command above | Ends green. 380 passed at last check, including the agent tests |
| **Health** | `docker compose exec -T app python scripts/doctor.py` | Reports ready before any demo |
| **Agent host** | `python scripts/probe_tools.py` | Checks tool calling; `python scripts/agent_eval.py --models --out docs/AGENT_EVAL.md` evaluates the live model on challenging messages |
| **Extraction baseline** | `python scripts/eval.py` | Scores the older extraction flow on its 40-message corpus |
| **Waitlist** | Join twice (second join says already), cancel the booked slot, tap Yes on the offer | The booking lands on the freed slot |
| **Still-coming** | With a `reminder_3h` template approved, book a slot 4 hours out | Keep/Cancel buttons appear. Keep protects the booking from the silent release; Cancel and silence both hand the slot to the waitlist |

</details>

## Layout

<details>
<summary><b>Where everything lives</b></summary>

<br>

| Path | Purpose |
|---|---|
| `app/channels/` | WhatsApp and Telegram wire formats, one sender, invite links and QR stickers; both produce the same message |
| `app/agent/` | The tool-using model, validated tools, grounding checks, and owner assistant |
| `app/ai/` | The extraction fallback, voice notes to text, hairstyle photos to services |
| `app/db.py` | Connection pool and a small migration runner (`migrations/*.sql`, applied in order, once) |
| `app/main.py` | The web app: `/health`, `/webhook`, `/mpesa/callback/<secret>` |
| `app/conversation.py` | Routing (deep link, binding, business picker) and the conversation |
| `app/availability.py`, `app/bookings.py` | The calendar |
| `app/messages.py` | Every message, EN/SW |
| `app/onboarding_chat.py`, `app/onboarding_agent.py` | Validated salon registration with natural model-guided intake and a deterministic fallback |
| `app/binding.py` | Slugs and sender bindings |
| `app/mpesa.py` | Daraja client |
| `app/payments.py` | Deposits: request, idempotent settlement, refund parking, reconciliation sweeper |
| `app/lexicon.json`, `app/lexicon.py` | English/Swahili/Sheng vocabulary as data; see [docs/LEXICON.md](docs/LEXICON.md) |
| `app/screen.py`, `app/guard.py`, `app/logs.py` | Screening, safety limits, and secret-safe logging |
| `app/owner.py` | The owner's chat commands, time off, daily summary; see [docs/TELEGRAM.md](docs/TELEGRAM.md) for the Telegram channel |
| `migrations/028_agent.sql` | Durable agent turn records for trace inspection and safe retries |
| `scripts/seed_salon.py` | Create a salon with stylists and services |
| `scripts/invite_qr.py` | Print a salon's QR sticker |
| `deploy/` | Production compose, Caddy, backups, and the self-hosted speech service |
| `docs/assets/` | Diagrams used in this README (static SVG, so they render on GitHub web and mobile) |

</details>

## Privacy

Voice notes and photos are processed in memory and never stored (voice by our own speech service, photos by Muse Spark). Only the text is kept in the conversation.

## Licence

MIT, see [LICENSE](LICENSE).
