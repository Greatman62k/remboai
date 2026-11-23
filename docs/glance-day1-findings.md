# RemboAI Glance Day 1 code findings

The v6 workflow is authoritative for the Glance API. These findings record the inspected backend contracts before the Day 1 API changes.

## D-1 — Inline approval text and manual drafts

- `app/handoff_outbox.py`, `approve_reply(pool, salon_id, handoff_id, owner_identity, expected_version, text, idempotency_key, draft_id=None)`: approval accepts inline text when `draft_id` is omitted. If a draft ID is provided, it checks that the draft belongs to the case and owner, is unconsumed, unexpired and based on the expected case version.
- `migrations/046_handoff_drafts.sql`: draft rows are persisted, case-bound and consumed by the outbox FK/unique constraint.
- **Conclusion:** the manual draft remains necessary for Glance provenance, author identity, version binding, expiry, idempotency and the required draft ID. Inline service support is not a reason to bypass it. No approval behavior was changed on Day 1.

## D-2 — Case version increments

- `app/handoffs.py`, `create_handoff()`: a new case starts at version 1; its initial customer event is the initial version, while `case_opened` and owner notification are internal and do not add increments. A new customer event on an already-open case increments once.
- `app/handoffs.py`, `record_customer_message()`: each non-duplicate customer message increments once; a duplicate provider message does not.
- `app/handoffs.py`, `record_customer_reply()`: assistant replies are customer-visible but do not increment the case version.
- `app/handoff_outbox.py`, `approve_reply()`: an approved owner-reply event increments once; outbox insertion does not add a second increment.
- `app/handoff_outbox.py`, `_complete()` and `record_whatsapp_receipt()`: delivery-status events use `_next_event()` without `bump_version`, and outbox state transitions do not increment the version.
- `app/handoffs.py`, `resolve_handoff()`: the open-to-resolved update increments once in the same transaction as the internal resolution event.
- **Conclusion:** current behavior matches v6 for customer messages, approved owner replies, internal events and outbox state. Initial creation establishes version 1. The notable boundary is that customer-visible assistant replies do not bump the version, consistent with v6's narrower “customer events” rule. No shared resolver or outbox behavior was changed on Day 1.

## D-3 — Transcriber language hints

- `app/ai/voice.py`, `Transcriber.transcribe(audio, mime, lang, salon_id=None)`: accepts `lang: str | None` and passes a non-null value to the ASR service as the `lang` query parameter.
- **Conclusion:** language hints are supported. The existing ASR contract uses codes such as `swh_Latn`; the proposed Glance values `auto|sw|en` need an API mapping (`auto` to `None`, and supported language codes to the provider form) before a transcription route is added. Transcription is outside Day 1.

## D-4 — Limits, enumerations and idempotency

- `app/handoff_outbox.py`, `approve_reply()`: accepts 1–4096 characters and idempotency keys of 1–200 characters. Its replay lookup is `(salon_id, idempotency_key)` and rejects a reuse that points at another case, owner, text or draft.
- `app/handoff_drafts.py`, `generate_draft()`, and `migrations/046_handoff_drafts.sql`: generated drafts are limited to 700 characters; the schema requires a non-empty `model_id`. The service has a ten-minute TTL and no API-level idempotency record.
- `migrations/045_handoff_outbox.sql`: outbox `state` permits `queued`, `sending`, `submitted`, `delivered`, `failed`, `unknown` and `cancelled`; current idempotency uniqueness is `UNIQUE (salon_id, idempotency_key)`.
- `migrations/044_handoff_events.sql`: event actors include `customer`, `assistant`, `owner` and `system`; visibility is `customer_visible` or `owner_internal`. The Day 1 API exposes only the first three actors on allowlisted customer events.
- `app/guard.py`, `_SENSITIVE`/`classify()`: emitted handoff categories are `emergency`, `legal`, `medical`, `payment_dispute`, `deposit`, `harassment`, `complaint`, `privacy` and `human_request`. The `waiting` and `default` strings in `_REPLIES` are response fallbacks, not categories emitted by `classify()`.
- **Conclusion:** the proposed v6 1000-character reply cap is stricter than the existing approval service; 300-character revision instructions are not an existing service contract; persisted draft body limits are 700. V6 idempotency scope is broader than the current approval service/schema and draft generation has no idempotency behavior. These are later-stage integration gaps and were not silently changed on Day 1.

## D-5 — Actual service signatures and migration structures

- `app/handoffs.py`, `list_open_handoffs(pool, salon_id, owner_identity, limit=20) -> list[dict]`: verifies the current salon owner in a transaction and returns oldest-open-first tenant-scoped cases. Day 1 adds a customer-visible preview and total-open count to each returned item without changing the return shape.
- `app/handoffs.py`, `get_handoff_context(pool, salon_id, handoff_id, owner_identity, limit=10) -> dict | None`: verifies owner and salon, caps the existing path at ten customer-visible events, and returns them in ascending sequence. Day 1 adds a separate `get_glance_handoff_context()` path with an event-type allowlist, 11 visible-event look-ahead and latest reply state.
- `app/handoff_drafts.py`, `generate_draft(ctx, salon_id, handoff_id, owner_identity, expected_version) -> dict | None`: reads authorized context, calls one model step with no tools, rejects unsafe output, rechecks the version under lock and stores a ten-minute draft.
- `app/handoff_outbox.py`, `approve_reply(...) -> dict | None`: verifies owner; serializes same-key actions with an advisory transaction lock; optionally validates a draft; writes the approved event and queued outbox row in one transaction. `draft_id` is optional today.
- `app/handoffs.py`, `resolve_handoff(pool, salon_id, handoff_id, actor_identity, reason) -> dict | None`: verifies owner, locks the case, rejects `sending`, cancels queued replies, resolves and updates conversation state transactionally. It has no expected-version, unknown-reply acknowledgement, cancellation-ID or original-result replay contract yet.
- `migrations/044_handoff_events.sql`: creates tenant-bound, immutable case events, event sequence, case version and resolution attribution.
- `migrations/045_handoff_outbox.sql`: creates tenant-bound outbound audit/state, provider snapshot, and salon-scoped idempotency uniqueness.
- `migrations/046_handoff_drafts.sql`: creates tenant-bound drafts with 700-character bodies, mandatory model IDs and draft-to-outbox FK/unique constraints.
- **Conclusion:** the Day 1 routes call the existing inbox service and a Glance-specific filtered context service. No draft, approval, outbox, resolution or transcription endpoints were added.
