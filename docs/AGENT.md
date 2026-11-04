# The agent: the Meta model runs the receptionist

`AGENT_MODE=on` routes ordinary customer, salon-owner, platform-admin, and unlinked-chat text to Muse Spark before canned greetings, scope redirects, or keyword replies. Code still screens abuse, resolves identity and salon routing, validates tool calls, and performs business actions. Explicit payment input, button taps, registration creation, and owner/platform mutation commands stay in their guarded workflows. With agent mode off, the deterministic flows remain active; they also answer when the model is unavailable or its draft fails validation.

## One turn

1. **Brief.** The model gets the salon's real facts (services with prices and stylists, opening hours, deposit policy, today's calendar), the recent conversation, and the current situation (for example the three slots just offered).
2. **Decide.** It calls tools, one step at a time, until it has an answer. Native function calling is used when the host supports it; otherwise the same loop runs with JSON steps (`AGENT_PROTOCOL=auto` switches by itself).
3. **Do.** Each tool call is validated and executed by code (`app/agent/tools.py`) on top of the booking engine that already existed: holds with an advisory lock plus a Postgres exclusion constraint, idempotent cancellation, the configured deposit flow, and the waitlist.
4. **Check.** The model's draft reply is compared with what it was given (`app/agent/grounding.py`). Rejected: a price or time that was not in the facts, a link or phone number nobody provided, "paid" or "confirmed" or "cancelled" when no tool did it, a deposit waiver the salon never offered, mentions of tools or JSON. The model gets one chance to repair the reply; only an unavailable or rejected model turn reaches the deterministic response path.
5. **Send.** A grounded model draft is the customer-facing reply. Code supplies verified transaction details to the model and uses its own exact wording only when the model is unavailable or its draft is rejected.

## Tools

| Tool | What it does | Guarded by |
|---|---|---|
| `find_slots(service, date, window?, time?, stylist?)` | Searches the real diary, returns up to 3 slots with ids | service must match the menu, date within 60 days, stylist must do that service |
| `reserve_slot(slot_id)` | Holds one offered slot; the system then handles the deposit | only slot ids offered in an earlier turn, one reserve per turn, max active bookings per customer |
| `my_bookings()` | Lists the customer's upcoming bookings | their own bookings only |
| `cancel_booking(ref?)` | Cancels one of them | their own bookings only; refunds parked for review; waitlist offered the slot |
| `join_waitlist(service, date)` | Joins the waitlist for a full day | idempotent per customer, service and day |
| `hand_to_owner(reason, note)` | Pauses the bot and tells the owner | owner is notified; conversation switches to hand-over |

Owner tools (`app/agent/admin.py`): `bookings_on(date)`, `deposits(from, to)`, `daily_summary()`, `invite_link()`, and `cancel_booking(ref)` (the customer is told and any refund is queued). Summary and invite requests go through the model first; code supplies verified figures and generates the channel-correct link. Payment review, payment or deposit changes, and other owner mutations remain in their guarded code flows. If the model is unavailable or its answer fails grounding, those informational requests use the existing code response.

## Manual payment checks

Manual routes use two owner-confirmed stages. After a customer selects a slot with a nonzero manual deposit, code holds it for five minutes and asks the owner to confirm availability. Deposit instructions are not sent until the owner taps Available. If no response arrives, code cancels the hold and tells the customer to try later. A late owner response sends the customer a five-minute offer that repeats the service, time, and stylist. Their Yes tap rechecks and re-holds the slot under the normal database lock; if it was taken, the customer sees fresh slots. Changes to the requested details go through booking again and require a fresh owner availability check.

This preflight applies only to manual M-PESA and Airtel Money destinations. Daraja STK remains automatic: the API sends the prompt and its callback confirms payment. The customer payment prompt has **I've paid** and **Cancel booking** buttons. **I've paid** asks for a receipt screenshot or the amount and transaction reference. Text or vision can create a pending claim. Vision extracts the provider, amount, reference, date/time, and recipient name from a screenshot; the image is discarded, the output is validated against the configured route and expected amount, and no model result can settle a payment. Extracted fields are included in the owner's unverified claim message. Amount mismatches remain in the same owner inbox with expected and reported amounts shown; the owner checks the destination account and accepts the amount or declines the claim.

The customer is told to wait up to 30 minutes for manual payment verification. If that hold expires, the booking slot is released and the customer is notified, while the payment claim remains pending. Customer cancellation also leaves a submitted claim open. The owner can send `pending payments` at any time to reopen every unresolved claim, with a separate decision for each payment. A late owner confirmation restores the original booking only if it is still active and the slot is free; otherwise the verified payment enters `needs_refund` review and the customer is told not to pay again. Ops resolution records the payment as `refunded` or `closed_no_refund`, matching the review disposition so closed claims leave the unresolved inbox.

## Platform administrator assistant

When `AGENT_MODE=on`, ordinary platform-admin text, including greetings, help, status questions, and short follow-ups, reaches `app/agent/platform_assistant.py`. The model can read business status/activity and aggregate totals through read-only tools. It cannot change, warn, or delete a business. Those actions remain in `app/platform.py` behind explicit commands and their confirmation rules. Platform turns are recorded in `agent_turns` with `salon_id=NULL`, joined to the full reply in `/ops/conversation-trace`, and use a sender-scoped, 30-minute conversational context that is purged with the agent retention job. With the agent disabled or unavailable, deterministic command handling and help text remain as fallbacks.

## Salon registration

`app/onboarding_agent.py` uses the same `AgentModel` and bounded `drive()` loop for natural intake. The model can extract several fields from one message and phrase follow-up questions in English, Swahili, or Sheng. Its only tool returns evidence quotes from the current message; code checks each quote, applies the name, service, duration, hours, price, deposit, and payment-destination validators, and persists only valid values. A phone destination must be explicitly identified as M-PESA Pochi la Biashara, direct M-PESA Send Money, or Airtel Money. Earlier conversation text helps the model understand the exchange but cannot serve as evidence for a new value.

The registration agent cannot create salons or issue invite links. `app/onboarding_chat.py` validates the complete draft and requires a clear final yes before its transaction creates the salon, stylists, and services. Duplicate-name checks, registration limits, cancellation, and the deterministic fallback remain code-controlled. Registration model turns are written to `agent_turns` with no salon id, so `/ops/agent-trace` can show whether the model or a safe template answered before a salon exists. These traces use the existing retention period.

## Conversation evidence

`/ops/conversation-trace` records incoming chat text or transcripts, the resolved salon when applicable, every outgoing text/button response with its provider send result, and the matching model/tool trace when the agent handled that turn. The `agent.model_requests` array records each live model response's backend, model ID, sanitized endpoint, response/request ID, and latency. Vision calls for hairstyle photos and payment screenshots are attached to the same trace as the customer-facing reply. Message content is retained as written (up to the application limits); thread IDs are keyed opaque tokens, not sender IDs. On startup, legacy thread IDs are rekeyed from their matching inbound sender records. A successful send result means the channel accepted the send request; it is not a read receipt. This includes deterministic registration prompts and safety fallbacks, which are absent from `/ops/agent-trace` by itself. The endpoint requires the ops key and is limited to the configured agent retention period (14 days by default). Existing agent turns are backfilled when the migration runs, but old deterministic replies cannot be recovered because they were not stored.

This project uses its existing custom tool loop; it does not add LangChain as another orchestration dependency.

## RemboAI assistant policy

`config/remboai_agent_policy.json` is the editable source for the booking receptionist, salon-owner assistant, platform administrator assistant, bare assistant, registration assistant, legacy booking extractor, hairstyle matcher, payment-screenshot extractor, JSON tool protocol, tool descriptions, and reply-repair instructions. Registration reply-quality checks and the bare assistant topic allowlist are also in the versioned file. Function names, argument schemas, tool execution, identity checks, booking and payment validation, registration state changes, and grounding enforcement remain in Python.

Both Docker Compose configurations mount this file read-only into the app. The app checks its contents on each assistant turn and reloads a valid change immediately, so editing this JSON does not require an image rebuild or app restart. If an edit has invalid JSON, a bad schema, or an invalid regex, the process logs the problem and keeps using its last valid policy. With an invalid file on a fresh process, model-assisted turns fall back to the existing deterministic flow. Update the whole file atomically when possible. The optional `AGENT_POLICY_PATH` setting can point a directly run app at another policy file; without it, the app uses this repository file.

## What cannot go wrong, by construction

- The model never supplies a price, a deposit amount or a payment state. Amounts come from the menu and the salon's deposit setting; M-PESA STK is settled by the Daraja callback, while manual M-PESA and Airtel Money claims require the owner's verification and tap.
- A payment screenshot can create an unverified claim only. The 5-minute availability check and 30-minute manual-payment wait are persisted in the database; the owner can reopen all unresolved claims with `pending payments`.
- The model cannot book a slot in the same turn it offers it: a customer picks, by tapping or by typing.
- A retried message (crash, redelivery) resends the stored reply instead of asking the model again, so the customer never gets two different answers (`agent_turns`).
- Customer text is data: injection attempts are screened before the model, and the model's brief tells it so; even a model that obeyed one could only call the tools above with validated arguments.

## When the model is not there

| Situation | What happens |
|---|---|
| Primary model host down or circuit breaker open | A configured secondary model endpoint is tried; if none is configured or it also fails, the role's deterministic code fallback handles the turn (`agent_turns.outcome = 'unavailable'`) |
| Daily model budget spent | The role's deterministic code fallback handles the turn without making another model call (`agent_turns.outcome = 'unavailable'`) |
| Model replied but the checker rejected it twice | code templates built from the tool results (`outcome = 'templates'`) |
| Model failed after it already booked or cancelled | code's confirmation of what happened is still sent |

No secondary endpoint is configured in the current deployment. A secondary endpoint such as Muse Spark Open through Baseten could be added later via `LLM_FALLBACK_*`. If configured, it would use the same tool validation and reply grounding as the primary model. The sequence would be primary model, optional secondary model, then deterministic code fallback. Code validates every model action and remains the final response path when no model produces a usable answer.

## Verify it

```bash
python scripts/probe_tools.py                              # does your host do tool calling? one line per backend and wire format
python scripts/agent_eval.py --models --out docs/AGENT_EVAL.md   # model vs keyword rules on 33 hard messages
curl -s -H "X-Ops-Key: $OPS_API_KEY" https://$DOMAIN/ops/agent-trace?limit=5 | python -m json.tool   # the last turns: said, tools, sent
curl -s -H "X-Ops-Key: $OPS_API_KEY" https://$DOMAIN/ops/conversation-trace?limit=20 | python -m json.tool  # complete turns and actual sent replies
docker compose exec -T app python scripts/doctor.py        # includes an agent line per backend
```

Settings: `AGENT_MODE`, `AGENT_PROTOCOL` (`auto`, `tools`, `json`), `AGENT_MAX_STEPS`, `AGENT_CALL_TIMEOUT_S`, `AGENT_TURN_TIMEOUT_S`, `AGENT_REASONING_EFFORT`, `AGENT_RETENTION_DAYS`, and optional `AGENT_POLICY_PATH`. Each step is a model call counted against `LLM_DAILY_LIMIT`.
