# RemboAI

A WhatsApp receptionist for Kenyan salons and barbershops. A customer writes the way they talk, in English, Swahili or Sheng;
the receptionist finds a free slot, holds it, and asks for an M-Pesa deposit. Built for the Meta Global AI Developer Hackathon,
problem statement **Build for Your World**.

> **Prior work and what's new.** Before the event I privately prototyped an earlier version of this idea (not submitted, not public,
> and none of its code is in this repository), and I set up my WhatsApp Business Cloud API and Daraja (M-Pesa) sandbox accounts.
> This repository is a fresh implementation, written Oct 27 - Nov 4, 2026. What carried over is the idea and the design.
> Every file here was written during the event; see [CHANGELOG.md](CHANGELOG.md).

## Status
Working: signed WhatsApp webhook, booking conversation (English / Swahili / Sheng) on Meta's models with failover, slot holds that cannot double-book, M-Pesa deposits paid straight to the salon's own till (sandbox-tested; production needs Safaricom Go Live). owner commands over WhatsApp, an owner web page and API (docs/OWNER_API.md). voice notes (Meta's Omnilingual ASR on Modal). photo matching (Muse Spark vision). a Telegram channel on the same engine (text, buttons, voice notes, photos, deposits, owner commands). Next: reminders, first server deployment.

## Run locally
```bash
cp .env.example .env          # fill in your own values
docker compose up --build     # Postgres + the app on :8080
python -m pytest -q           # tests (database tests need TEST_DATABASE_URL)
```
Testing the whole thing end to end: docs/E2E_TEST.md. `python scripts/doctor.py` checks the whole set-up (WhatsApp token, models, M-Pesa, public URL) and says what to fix.

## Layout
- `app/whatsapp.py` signature check, payload parsing, sending, explanations of Meta's error codes
- `app/db.py` connection pool and a small migration runner (`migrations/*.sql`, applied in order, once)
- `app/main.py` the web app: `/health`, `/webhook`, `/mpesa/callback/<secret>`
- `app/conversation.py` the conversation; `app/llm.py` the model client and keyword fallback; `app/availability.py`, `app/bookings.py` the calendar; `app/messages.py` every message, EN/SW
- `app/mpesa.py` Daraja client; `app/payments.py` deposits: request, idempotent settlement, refund parking, reconciliation sweeper
- `app/vision.py` hairstyle photo -> one of the salon's services; `app/voice.py` voice notes -> text; `deploy/modal_asr.py` the speech service; `deploy/` production compose, Caddy, backups
- `app/ownerapi.py` + `app/web/` the owner JSON API and web page
- `app/owner.py` the owner's chat commands, time off, daily summary
- `app/telegram.py` Telegram as a second channel (`tg:<id>` addresses, polling or webhook); `app/inbound.py` the channel-independent entry point; see docs/TELEGRAM.md
- `scripts/seed_salon.py` create a salon with stylists and services

## Privacy
Voice notes and photos are processed in memory and never stored (voice by our own speech service, photos by Muse Spark); only the text is kept in the conversation. While the Meta Model API runs on free credits, Meta may use message content for product improvement, so tell customers and salons.

## Security notes
Every webhook is verified with the app secret (HMAC-SHA256) before anything is read. Meta may deliver a message more than once;
each message id is stored and processed exactly once. Phone numbers are masked in logs. Secrets live only in `.env`, which is never committed.

## Money safety
RemboAI never holds money: deposits go to the salon's own till or paybill. Callbacks are matched by CheckoutRequestID and kept verbatim;
settlement is idempotent; a missed callback is recovered with a status query; a payment that cannot be honoured (slot taken, booking cancelled,
duplicate, under-payment) is parked as `needs_refund` and reported to the customer and the owner, never dropped.

## Licence
MIT, see [LICENSE](LICENSE).
