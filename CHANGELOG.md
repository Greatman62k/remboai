# Changelog

Everything in this repository was written during the Meta Global AI Developer Hackathon (building period Oct 27 - Nov 4, 2026).

## Nov 2 (assistant policy, registration agent, conversation evidence)

399 tests, all passing.

- **Editable policy** (`config/remboai_agent_policy.json`, `app/agent/policy.py`): the prompts, topic allowlist and reply-quality rules for every assistant live in one versioned JSON file mounted read-only by both Compose files. A valid edit is picked up on the next turn; an invalid one is logged and the last valid policy keeps running.
- **Bare assistant** (`app/agent/bare_assistant.py`): chats not linked to a salon get a model-written reply about RemboAI itself, with no tools or business data; out-of-scope requests never reach the model.
- **Registration agent** (`app/onboarding_agent.py`): the model reads the owner's free-form message, every extracted value must be backed by evidence in that message and pass the existing parsers, and only the deterministic onboarding code creates the salon after an explicit final yes.
- **Platform assistant** (`app/agent/platform_assistant.py`): read-only natural-language questions for platform administrators (`PLATFORM_OWNER_IDS` adds backup admins); changes still go through the confirmed commands in `app/platform.py`.
- **Conversation evidence** (migrations 029-032): `/ops/conversation-trace` shows each inbound turn, every reply with its send result, and the model/tool trace, for the agent retention period (14 days by default); deleting a salon deletes its traces.
- **Hardening** (migration 033): webhook job leases are fenced to one claim token so a stale worker cannot complete or retry a reclaimed job; exact M-Pesa callback replays are not stored twice; handoffs, callback bodies and raw unmatched-payment payloads are purged on `HANDOFF_RETENTION_DAYS`, `MPESA_CALLBACK_RETENTION_DAYS`, `UNMATCHED_RAW_RETENTION_DAYS`.
- **Manual till format** (migration 034): manual tills store and accept only the seven-digit till number. Daraja shortcode details remain required only for STK configuration.
- **Conversation polish from live traces**: onboarding accepts spoken hours such as "from eight to eight" and acknowledgement-prefixed registration commands; the short "ye" affirmative is accepted only at the non-creating ownership notice; platform status commands tolerate one-character typos, and suggestion offers get a natural follow-up.
- Diagrams: the README flow chart is now `docs/assets/agent-flow.svg` (renders everywhere, matches agent mode); the old keyword-first `model-fallback.svg` is removed and the other two diagrams no longer say rules read the intent.
- README rewritten for judges: honest channel status, Meta-model-at-the-core section with the technical design, measured results (keyword baselines and the 33/33 Muse Spark result), trust table, privacy note covering the 14-day trace.
- Owner-facing replies show Telegram customers as "Telegram ID ..." instead of a fake phone number (`app/contact.py`).

## Nov 1 (the Meta model runs the receptionist)

- **Agent mode** (`AGENT_MODE=on`, `app/agent/`): Muse Spark decides each step through tools - `find_slots`, `reserve_slot`, `my_bookings`, `cancel_booking`, `join_waitlist`, `hand_to_owner` - and code validates and executes every call on the existing booking engine. Native function calling with an automatic fall back to JSON steps for hosts without it.
- **Grounding checker**: a reply with a price, time, link, phone number, payment claim, confirmation or deposit waiver the model was not given is rejected, repaired once, then replaced by code-written templates built from the same tool results. Holds, payment steps and cancellations are always worded by code.
- **Owner assistant**: plain-words questions about bookings and deposits, and owner-requested cancellations, through `bookings_on`, `deposits`, `cancel_booking`.
- **Fails soft**: model down, breaker open or budget spent falls back to the keyword flow; a retried message resends the stored reply (`agent_turns`, migration 028) instead of asking the model again.
- **Proof**: `/ops/agent-trace`, `python scripts/probe_tools.py` (does this host do tool calling), `python scripts/agent_eval.py` (model vs keyword rules on 33 messages the rules were not tuned for; rules alone score 19/33), `docs/AGENT.md`, doctor agent lines.
- Mid-conversation follow-ups with no salon words ("ok Brian then") are no longer screened out when agent mode is on.
- Telegram typing indicator while the model thinks; `_cancel` can target one booking.

## Oct 31 (waitlist, Still-coming, proxy auth, site)

352 tests, all passing.

- Waitlist fill (`app/waitlist.py`, migration 026): any cancel (customer, late tap, owner) offers the freed slot to the oldest waiter with Yes/No buttons; Yes books the exact slot within 10 minutes (held with deposit prompt, or confirmed when free); No, expiry, or a taken slot moves the offer on; a minute sweep expires stale offers.
- Still-coming 3h reminders (migration 027): approved `reminder_3h` templates send with Keep/Cancel buttons; Keep marks the nudge answered, Cancel frees the slot at once, and a minute sweep releases never-answered bookings under 2h out toward the waitlist.
- Voice notes run behind Modal proxy auth: the endpoint requires a proxy token before any container starts, our own token moved to the `X-Asr-Token` header, and the app sends both (`ASR_PROXY_KEY`/`ASR_PROXY_SECRET` in `.env`). Image also lifts NumPy to 2 for pyarrow.
- Marketing site started in `site/` (Home, Pricing, Setup guide) in the reference house style with Telegram blue and WhatsApp green accents; contact links are still placeholders.
- README verification section and keyword evaluation baseline re-run (40 messages: 98 percent intent, 100 percent language).

## Oct 31 (dependency refresh)

327 tests, all passing.

- Pinned dependencies moved to their latest releases: pydantic 2.14.2, redis 8.1.0, cryptography 50.0.2, prometheus-client 0.26.0, pytest 9.2.0. Everything else was already latest.
- The refresh fixed a real gap as a side effect: the QR sticker tests now run instead of skipping on a broken `qrcode` import.
- Verified end to end: host suite plus the full Docker suite on a rebuilt image, against real Postgres and Redis.

## Oct 30 (chat-first rebuild)

278 tests, all passing.

### Chat onboarding
- Onboarding registers a salon in minutes with no model in the decision path: name with twin check, barbershop-or-salon type with matching examples, one or many stylists, priced services with times, opening hours, live invite link.
- Interruptions steer instead of being stored as names; BACK/CANCEL work on every step and name the previous task; corrections ("Fade 600") and plain-language changes ("i want to change time", "they are ok") return to the summary.
- Guessed times are marked `(~)` and never re-asked when the owner already gave them; a bare time belongs to the only service.
- Plain sentences register too ("i want to register a business", "nataka kusajili saluni yangu"), in either language.

### One bot, many salons
- One central WhatsApp number and one Telegram bot serve any number of salons (migrations 021, 022). Customers arrive by invite link or QR sticker with the business code prefilled, or pick their business; owners with one salon land in it.
- Tenant data stays salon-scoped (bookings, clients, payments, reminders, owner commands), proven by a cross-salon test.
- New `/id` command returns the sender's address for owner setup.

### Conversation hospitality
- Pleasantries ("thanks", "sawa", "perfect", "ok") get warm replies in the writer's language with no model call, never the off-topic lecture.
- The reply language follows each message instead of sticking after one Swahili greeting; neutral words never flip it alone.
- Customer invite links are built per channel in one module (`app/channels/invites.py`); the owner `invite` command returns the link any time, and `scripts/invite_qr.py` prints the QR sticker.
- The bot introduces itself as whoever the sender reached: a linked sender meets the booking assistant for their business, anyone else meets RemboAI itself with plain REGISTER steps for owners. `help`/`msaada` explains use in non-technical words, in either language, and asking mid-booking loses nothing.
- Pumping the shared bot with chatter no longer re-sends the business list every time: after two lists it answers a one-line nudge and records an abuse strike, feeding the existing ingress block.
- A bot with no businesses answers as RemboAI, tells senders their id, and registers the first business by chat; repeat chatter gets a one-line REGISTER nudge, and every exchange is kept in `stranger_state` so it stays checkable. `doctor.py` treats the fresh-bot state as ready, not broken.
- Returning customers get one welcome ("Welcome back! What would you like to book?") instead of two; owners asking "who are you" meet the assistant; bare "id" works; the cancel notice covers booking from another number.
- Platform control hub (`app/platform.py`): the `PLATFORM_OWNER_ID` address runs shops, stats, off/on, del with typed YES confirm, and warn from chat; strangers never reach it.
- Business lifecycle (`app/lifecycle.py`, migration 024): no booking for 7 days warns the owner, day 13 warns finally, day 14 deletes the salon and everything in it; any booking or KEEP cancels the countdown, and the daily loop runs the sweep.

### Owner operations and model gateway (built earlier in the week, first recorded here)
- Late pushes return the day's new schedules; cancel or move any customer by id or number with only the affected customer told; away-day blocks notify without cancelling; post-visit follow-ups check attendance and alert the owner on a miss.
- Model gateway with labeled backends, tunable breaker, call logging and reasoning strip, plus `/model-info` and a 40-item eval corpus.

### Removed
- The owner web page, its API, and web setup were deleted before launch; chat is the only setup path. Dead routes left the proxy allowlist and metrics labels with them.

### Housekeeping
- Channel code grouped into `app/channels`, model code into `app/ai`; comment-only lines stripped from all Python files; weak secrets rotated; docs rewritten for the chat-first product with private notes moved out of the commit path.

## Oct 29

### Audit fixes
- Reminders now send: the channel hub has `send_template` (it was missing, the error was swallowed and the free-text fallback is refused outside WhatsApp's 24-hour window); the salon's token is decrypted before use; template parameters follow `reminder_templates.variables`; failed sends are retried with backoff; overlapping runs cannot double-send; `REMINDER_TEMPLATE_24H/2H` are read.
- An hour next to a phone number ("saa 7 0712345678") is no longer glued into the number, so the M-Pesa prompt cannot go to a stranger.
- A paid, known payment is never filed as unmatched because Daraja's status query was down; the sweeper settles it later and saves the receipt from the stored callback.
- Messages are handled at least once (completion is recorded after handling, a crash leaves the message open for the queue's retry); README no longer says "exactly once".
- Database pool: jobs no longer pin a connection for a lock; their lease is renewed by a heartbeat, and concurrent jobs are capped by the pool size.
- Date/time: a declined today ("leo siwezi, kesho ...") is not today; `15.11` and `Ksh 1.50` are not clock times; `saa 8 mchana` is understood.
- Injection filter no longer flags the name "Dan"; "losing hair" and "police station" no longer hand the customer to the owner.
- Handoff really pauses the bot now (the state was never saved: missing `wa_id`, then overwritten), and honours `HANDOFF_HOURS`.
- A non-ASCII signature header gets 403 (was 500); no header can crash the body-limit or IP middleware; dead webhook jobs are purged; two unused settings removed.

### Retry safety, locks and tokens (migration 016)
- The per-sender lock is a lease row (`sender_locks`), not a session advisory lock: handling a message no longer pins a pooled connection through the model call and the sends. Nothing holds one connection while waiting for another, so the pool cannot deadlock; a cap on concurrent jobs only stops a burst from starving the sweeper.
- One customer's messages are handled in the order they were sent (`webhook_jobs.ordering_key`): a message waits for an earlier one from the same sender that is retrying, and is released if that one is parked as dead.
- A retried message starts from the conversation as its first attempt found it (`inbound_messages.state_before`, dropped when the message completes) and repeats nothing: sends and owner alerts are remembered per message (`outbound_log`), and slot holds, cancellations, M-Pesa prompts and abuse strikes are keyed by the message that caused them. A prompt that died between its database row and Daraja's answer is closed rather than sent twice.
- A settled payment is announced even if announcing failed the first time (`payments.announced_at`); payments settled before this release are treated as announced.
- The salon's own WhatsApp token (decrypted) is used for every send and media download, with the platform token as the fallback (`outbound.salon_token`); all sends go through `outbound`.

### Model budgets, screen, guardrails, vocabulary
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

## Oct 28

### Owner web page, API and deployment
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

### Telegram channel
- Telegram as a second channel on the same engine, because a WhatsApp Business account cannot message until Meta business verification completes. Bots need no verification and no public URL while testing (long polling).
- Customers and owners are `tg:<chat id>` addresses; a channel hub routes every send by address, so deposits, reminders, owner alerts and the daily summary reach the right app.
- Inline keyboards for slot buttons, voice notes and photos through the same speech and vision pipelines, `/start`, `/id` (shows the id to use as `--owner`), shared contacts.
- Webhook mode with a secret header (constant-time check, closed when no secret is set) or polling; polling backs off on errors and removes any webhook first.
- Found by tests: a Telegram button tap carried no text, which the conversation treats as unreadable; the tapped button's label is now used, as WhatsApp does.
- 10 Telegram tests with a fake Bot API. 76 tests in total.
- End-to-end test guide and Telegram set-up guide.

## Oct 27

### Skeleton, booking core, model client
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

### M-Pesa deposits
- M-Pesa deposits via Daraja STK push: per-salon payment account (till or paybill) in the database, money goes straight to the salon.
- Callback route with a secret in the path; every callback stored verbatim for audit; settlement is idempotent.
- Money states are explicit: late payments re-take a free slot, payments that cannot be honoured (slot taken, booking cancelled, duplicate, under-payment) are parked as `needs_refund` and reported to the customer and the owner.
- Sweeper: expires holds (but not while a prompt is open), reconciles missed callbacks with STK status queries, writes off silent prompts.
- Conversation: prompt goes to the chat number automatically; "pay" retries; a typed Kenyan number redirects the prompt; salons with no till confirm without a deposit.
- 16 wire-level payment tests with a fake Daraja.

### Owner tools over chat
- Owner tools over WhatsApp, matched by exact commands in code (no model): `today`, `tomorrow`, `bookings friday`, `block friday 14:00-16:00`, `unblock friday`, `cancel 2`, `summary`, `help`, with Swahili equivalents.
- Time off table; customers are never offered blocked time, and a slot offered just before a block is refused when tapped.
- Owner cancels a booking: the customer is told (with a refund note if a deposit was paid) and the owner is told whom to refund.
- End-of-day summary to the owner once per local day (tomorrow's bookings, today's deposits).
- 7 owner tests.
- Per-sender rate limit (default 20 messages a minute) so nobody can burn model credits.
- `scripts/doctor.py`: one command that checks configuration, database, each salon's WhatsApp number, each model endpoint, Daraja and the public URL, and says what to fix.
