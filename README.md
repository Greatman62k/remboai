# RemboAI

A WhatsApp receptionist for Kenyan salons and barbershops. A customer writes the way they talk, in English, Swahili or Sheng;
the receptionist finds a free slot, holds it, and asks for an M-Pesa deposit. Built for the Meta Global AI Developer Hackathon,
problem statement **Build for Your World**.

> **Prior work and what's new.** Before the event I privately prototyped an earlier version of this idea (not submitted, not public,
> and none of its code is in this repository), and I set up my WhatsApp Business Cloud API and Daraja (M-Pesa) sandbox accounts.
> This repository is a fresh implementation, written Oct 27 - Nov 4, 2026. What carried over is the idea and the design.
> Every file here was written during the event; see [CHANGELOG.md](CHANGELOG.md).

## Status
Working: one central WhatsApp number and one Telegram bot serve any number of salons, each reached by its own invite link or QR sticker and isolated by sender binding. Chat onboarding registers a salon in minutes (name, barbershop or salon, stylists, priced services with times, hours) with no model in the decision path. Booking conversation in English, Swahili and Sheng on Meta's models with a keyword fallback, slot holds that cannot double-book, M-Pesa deposits paid straight to the salon's own till (sandbox-tested; production needs Safaricom Go Live). owner commands over WhatsApp and Telegram. voice notes (open ASR on Modal). photo matching (Muse Spark vision). Reminders (WhatsApp templates, with retries), post-visit follow-ups, late-stylist handling with customer keep/cancel/retime, away-day blocks, and per-message language that follows the writer.

## Run locally
```bash
cp .env.example .env          # fill in your own values
docker compose up --build -d  # Postgres + Redis + the app on :8080
docker compose -f docker-compose.test.yml -p remboai-test up -d --wait
docker run --rm --network remboai-test_default -v $PWD:/srv -w /srv --user root \
  -e TEST_DATABASE_URL=postgresql://rembo_test:rembo_test@test-db:5432/rembo_test \
  remboai-app python -m pytest -q
```
`docker compose exec app python scripts/doctor.py` checks the whole set-up (WhatsApp token, models, M-Pesa, public URL) and says what to fix.

## Layout
- `app/channels/` WhatsApp and Telegram wire formats, one sender, invite links and QR stickers; both produce the same message
- `app/ai/` the model client and keyword fallback, voice notes to text, hairstyle photos to services
- `app/db.py` connection pool and a small migration runner (`migrations/*.sql`, applied in order, once)
- `app/main.py` the web app: `/health`, `/webhook`, `/mpesa/callback/<secret>`
- `app/conversation.py` routing (deep link, binding, business picker) and the conversation; `app/availability.py`, `app/bookings.py` the calendar; `app/messages.py` every message, EN/SW
- `app/onboarding_chat.py` chat setup for owners; `app/binding.py` slugs and sender bindings
- `app/mpesa.py` Daraja client; `app/payments.py` deposits: request, idempotent settlement, refund parking, reconciliation sweeper
- `app/ai/voice.py` voice notes -> text; `app/ai/vision.py` hairstyle photo -> one of the salon's services; `deploy/modal_asr.py` the speech service; `deploy/` production compose, Caddy, backups
- `app/lexicon.json` + `app/lexicon.py` English/Swahili/Sheng vocabulary as data; see `docs/LEXICON.md`
- `app/screen.py`, `app/guard.py`, `app/logs.py` screening, safety limits, and secret-safe logging
- `app/owner.py` the owner's chat commands, time off, daily summary; see `docs/TELEGRAM.md` for the Telegram channel
- `scripts/seed_salon.py` create a salon with stylists and services; `scripts/invite_qr.py` print its QR sticker

## Privacy
Voice notes and photos are processed in memory and never stored (voice by our own speech service, photos by Muse Spark); only the text is kept in the conversation. While the Meta Model API runs on free credits, Meta may use message content for product improvement, so tell customers and salons.

## Security notes
Every webhook is verified with the app secret (HMAC-SHA256) before anything is read. Meta may deliver a message more than once;
each message is queued durably and handled at least once: a message whose handling crashed is retried (up to 8 times, then parked as a dead job for the operator), and a message that was handled is never handled again when Meta redelivers it. A retry repeats nothing: it starts again from the conversation as the first attempt found it; replies, owner alerts, slot holds, cancellations, M-Pesa prompts and abuse strikes are remembered per message and not done twice; payment confirmations are sent even if the first attempt to send them failed; and one customer's messages are handled in the order they were sent. It is still not "exactly once", because that is impossible across a network call: if the process dies between WhatsApp accepting a message and the app recording that it was sent, the retry sends it again. Phone numbers are masked in logs. Secrets live only in `.env`, which is never committed.

## Money safety
RemboAI never holds money: deposits go to the salon's own till or paybill. Callbacks are matched by CheckoutRequestID and kept verbatim;
settlement is idempotent; a missed callback is recovered with a status query; a payment that cannot be honoured (slot taken, booking cancelled,
duplicate, under-payment) is parked as `needs_refund` and reported to the customer and the owner, never dropped.

## Licence
MIT, see [LICENSE](LICENSE).
