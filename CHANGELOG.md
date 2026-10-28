# Changelog

Everything in this repository was written during the Meta Global AI Developer Hackathon (building period Oct 27 - Nov 4, 2026).

## Oct 27
- Project skeleton, MIT licence, configuration from environment variables.
- Migration runner and first migration (inbound message dedupe).
- WhatsApp Cloud API module: signature verification, payload parsing, text sending, delivery-failure explanations.
- Signed, idempotent webhook that echoes the message back.
- Booking core: salons, stylists, services, clients, bookings with a database exclusion constraint against double booking.
- Availability on a 30 minute grid, per-stylist slot holds under an advisory lock, hold expiry.
- Language-model client with a primary and a failover endpoint (Meta Model API, Muse Spark Open) and a keyword-rule fallback that fills the same form.
- English and Swahili messages; Swahili clock parsing (saa nane = 14:00); greetings and "how are you" answered, then the request carries on.
- Conversation memory in Postgres; button taps and slot numbers skip the model.
- `scripts/seed_salon.py` to set up a salon from the command line.

## Oct 27 (evening)
- M-Pesa deposits via Daraja STK push: per-salon payment account (till or paybill) in the database, money goes straight to the salon.
- Callback route with a secret in the path; every callback stored verbatim for audit; settlement is idempotent.
- Money states are explicit: late payments re-take a free slot, payments that cannot be honoured (slot taken, booking cancelled, duplicate, under-payment) are parked as `needs_refund` and reported to the customer and the owner.
- Sweeper: expires holds (but not while a prompt is open), reconciles missed callbacks with STK status queries, writes off silent prompts.
- Conversation: prompt goes to the chat number automatically; "pay" retries; a typed Kenyan number redirects the prompt; salons with no till confirm without a deposit.
- 16 wire-level payment tests with a fake Daraja.

## Oct 27 (night)
- Owner tools over WhatsApp, matched by exact commands in code (no model): `today`, `tomorrow`, `bookings friday`, `block friday 14:00-16:00`, `unblock friday`, `cancel 2`, `summary`, `help`, with Swahili equivalents.
- Time off table; customers are never offered blocked time, and a slot offered just before a block is refused when tapped.
- Owner cancels a booking: the customer is told (with a refund note if a deposit was paid) and the owner is told whom to refund.
- End-of-day summary to the owner once per local day (tomorrow's bookings, today's deposits).
- 7 owner tests.
- Per-sender rate limit (default 20 messages a minute) so nobody can burn model credits.
- `scripts/doctor.py`: one command that checks configuration, database, each salon's WhatsApp number, each model endpoint, Daraja and the public URL, and says what to fix.

## Oct 28
- Owner web page (mobile first, plain HTML/JS, strict CSP): bookings by day with cancel, services, time off, opening hours, deposit, stylists, refunds to send.
- Passwordless sign-in: the owner sends `link` on WhatsApp, gets a single-use 15 minute URL; sessions are stored hashed; cookie writes need a CSRF header.
- The page is a thin client of a JSON owner API (`docs/OWNER_API.md`), which a native Kotlin app can use unchanged.
- Refunds: payments that cannot be honoured are shown to the owner with the reason until he marks them done.
- 8 API tests, including proof that one salon cannot read or change another's data.
- Deployment: production Docker Compose with automatic HTTPS (Caddy), server hardening script, nightly backups, `/ready` health check, `docs/DEPLOY.md`.
- Voice notes: downloaded from Meta, transcribed by Meta's open-source Omnilingual ASR running on our own Modal GPU service (`deploy/modal_asr.py`), then handled like typed text; the bot says what it heard. Audio is never stored.
- 5 voice tests (fake speech service), `doctor` checks the speech service.
- Photo matching: a customer sends a hairstyle photo, Muse Spark (vision) suggests which of the salon's services it needs, code validates the suggestion against the menu (price and time come from the menu), and the booking carries on, with a caption such as "kesho mchana" continuing straight to slots. Doubtful, off-menu or non-hair photos are never guessed at. Photos are never stored.
- `doctor` shows the model a plain red square to prove image input works; 8 vision tests.

