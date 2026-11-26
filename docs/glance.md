# RemboAI Glance

RemboAI Glance is the owner escalation companion for Even Realities G2. It brings open customer handoffs from the RemboAI booking receptionist to a glanceable display, with the paired phone application used for full context review and exact-text reply editing.

The RemboAI backend implements paired owner sessions, a salon-scoped owner attention feed, the open-case inbox and case context, AI and manual drafts, reply approval and status, and safe case resolution. The companion client is maintained separately. End-to-end HTTPS staging and packaged Even Hub WebView acceptance have not yet been completed, so these capabilities should not be presented as a live service.

## Owner attention feed

Authenticated Glance sessions may call `GET /api/v1/glance/attention?limit=50` to retrieve a bounded, prioritized feed of durable owner work. It combines open customer escalations with booking availability decisions, payment-review and refund follow-up flags, recent booking reminders, new confirmed bookings, recent customer feedback, inactivity warnings and the daily summary counts.

Each item contains `attention_id`, `kind`, `priority`, `state`, a short `title` and `summary`, UTC `created_at` (and optional `expires_at`), an opaque `resource_id`, an optional `case_id`, and an `action`. The current read-only action values are `open_case`, `availability_decision_owner_workflow`, `payment_review_owner_workflow`, `refund_owner_workflow`, `view_booking_owner_workflow`, `view_feedback_owner_workflow`, `view_schedule_owner_workflow`, `keep_business_owner_workflow` and `view_summary_owner_workflow`. A `service_issue` item means the customer reported they were not attended; it does not resolve, rebook or cancel anything automatically.

The feed is read-only. It never exposes payment receipts, payment evidence, phone numbers, provider credentials or internal handoff events. Items whose mutation still belongs to the existing Telegram/WhatsApp owner workflow carry an explicit owner-workflow action; they are not presented as completed or silently executable Glance actions. The existing `/inbox` and case routes remain the source for full escalation context.

## Owner schedule

An authenticated Glance session may request the active schedule for a salon-local day:
`GET /api/v1/glance/schedule?date=YYYY-MM-DD&limit=20&offset=0`. Omitting `date` uses the salon's local today. The response contains only booking ID, status, compact customer label, service, staff, UTC start/end timestamps and a bounded payment state (`paid`, `awaiting_deposit` or `no_deposit_recorded`). It never returns customer phone numbers, payment amounts, references or evidence. Results are read-only and remain subject to session ownership revalidation.

Some chat-only notices cannot be replayed as independent feed entries yet: a one-off owner response such as a manually moved booking, a similar-name setup nudge and the final post-deletion message have no retained owner-notification record. The feed exposes the durable business state where one exists (for example, a schedule delay, current booking, lifecycle warning or reminder). A future notification ledger can add exact delivery history without scraping conversation text.

These short labels are UI copy. Case, payment, and delivery state must always come from the backend.

| Situation | Display copy |
|---|---|
| New escalation | New request needs your review |
| Payment review | Check payment in your account |
| Complaint | Customer shared a concern |
| Booking issue | Booking needs your attention |
| AI draft ready | Reply draft is ready |
| Listening | Listening… |
| Transcribing | Turning speech into text… |
| Transcript ready | Transcript ready. Review it. |
| Approval required | Review and approve before sending |
| Message queued | Reply queued |
| Message submitted | Reply accepted by the messaging service |
| Message failed | Reply was not accepted. Try again. |
| Delivery unconfirmed | Sending result is unclear. Check before retrying. |
| Case resolved | Case marked resolved |
| Session expired | Glance session expired. Pair again. |
| Connection lost | Connection lost. Reconnect to continue. |

## State wording rules

- “Queued” means RemboAI saved the approved reply for sending.
- “Submitted” means the messaging provider accepted the request. It does not mean the customer received or read it.
- “Delivery unconfirmed” means the provider may have accepted the request, but RemboAI cannot establish the outcome. Do not suggest an automatic retry.
- “Case resolved” describes the owner's explicit case action. It does not imply that a payment was verified, refunded, or otherwise settled.
- Payment review asks the owner to check the receiving account. A screenshot or extracted receipt fields are not proof of payment.
- Keep customer-visible replies separate from these owner-only labels and from internal case notes.
