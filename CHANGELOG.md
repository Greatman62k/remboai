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


## Oct 28 (Telegram)
- Telegram as a second channel on the same engine, because a WhatsApp Business account cannot message until Meta business verification completes. Bots need no verification and no public URL while testing (long polling).
- Customers and owners are `tg:<chat id>` addresses; a channel hub routes every send by address, so deposits, reminders, owner alerts and the daily summary reach the right app.
- Inline keyboards for slot buttons, voice notes and photos through the same speech and vision pipelines, `/start`, `/id` (shows the id to use as `--owner`), shared contacts.
- Webhook mode with a secret header (constant-time check, closed when no secret is set) or polling; polling backs off on errors and removes any webhook first.
- Found by tests: a Telegram button tap carried no text, which the conversation treats as unreadable; the tapped button's label is now used, as WhatsApp does.
- 10 Telegram tests with a fake Bot API. 76 tests in total.
- End-to-end test guide and Telegram set-up guide.

## Oct 29
- Found in the first real run: Docker's `env_file` keeps `# comment` text as part of a value, so the example `.env` produced a malformed `PUBLIC_BASE_URL` and a bad `ASR_URL`. Comments in `.env.example` now sit on their own lines, settings strip inline comments whichever way the file is loaded, and `doctor` reports a malformed URL instead of crashing.
- `doctor` shows the vision model a 256 px red square instead of 16 px (tiny images are padded or rescaled by vision encoders). Request format confirmed against Meta's image-understanding docs.
- 4 more tests (80 total).
- Daily ceilings on metered calls (text model, photo matching, speech) and on messages per person, counted in Postgres (`migrations/007_usage.sql`, `app/budget.py`). Past a ceiling the feature degrades instead of spending: keyword rules, the menu, "please type". `doctor` shows today's usage. 5 tests (85 total).
- Screen in front of the model (`app/screen.py`): first messages with no salon or booking vocabulary never reach the model and get a short pointer back to what the bot does; follow-ups in a running conversation always pass; plain greetings are answered without the model. 6 tests (91 total).
- Guardrails (`app/guard.py`, `app/logs.py`, `migrations/008_abuse.sql`): input normalisation (look-alike, zero-width and bidi characters removed), display names cleaned, prompt-injection phrases stopped before the model with the same reply as any off-topic message, abuse strikes with a 24-hour silence after three, one strike per message burst, a cap on upcoming bookings per customer.
- Found while checking: httpx logs every request URL at INFO and a Telegram bot token is part of its URL, so tokens would have reached the logs. Logging now redacts tokens, Authorization headers, the M-Pesa callback secret path and sign-in link tokens, including inside tracebacks; httpx/httpcore are silenced below WARNING. Verified against a running server. 9 tests (101 total).
- Vocabulary as data (`app/lexicon.json`, `app/lexicon.py`): the screen, keyword rules, date and time words, greetings and Swahili injection phrases all read one English/Swahili/Sheng word file, validated on load (malformed or too-common entries are dropped and reported, never fatal). `scripts/check_lexicon.py` checks a filled-in file without replacing anything and reports which example messages the lists get wrong. `docs/LEXICON.md` has the fill-in prompt.
- Fixed: "kesho kutwa" (the day after tomorrow) was read as tomorrow; "day after tomorrow" now works too. 7 tests (108 total).
- Vocabulary filled in with another tool's help (about 2,000 English, Swahili and Sheng entries across salon, barbershop, nails and spa services, booking, price, hours, cancel, greetings, days and parts of the day, and Swahili/Sheng injection phrases). Reviewed with `scripts/check_lexicon.py`, which removes generic words automatically (people slang, "am"/"pm" as parts of the day, money slang) and reports which example messages the lists get wrong; `--write` saves a cleaned copy.
- Screen now has three routes instead of two: strong salon words go to the model; prices, hours, cancel, greetings and bare days or times go to the free keyword rules (no model call); everything else gets the pointer. A message the rules cannot read but that has a specific price, payment, hours or cancel word still gets one model try. The price reply now mentions the deposit. 112 tests.
