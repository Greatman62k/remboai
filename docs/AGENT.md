# The agent: the Meta model runs the receptionist

`AGENT_MODE=on` uses Muse Spark for customer conversations, owner questions about the diary, and new salon registration. With it off, the earlier keyword/extraction and deterministic registration flows remain active. They also handle turns when the model is unavailable.

## One turn

1. **Brief.** The model gets the salon's real facts (services with prices and stylists, opening hours, deposit policy, today's calendar), the recent conversation, and the current situation (for example the three slots just offered).
2. **Decide.** It calls tools, one step at a time, until it has an answer. Native function calling is used when the host supports it; otherwise the same loop runs with JSON steps (`AGENT_PROTOCOL=auto` switches by itself).
3. **Do.** Each tool call is validated and executed by code (`app/agent/tools.py`) on top of the booking engine that already existed: holds with an advisory lock plus a Postgres exclusion constraint, idempotent cancellation, the deposit prompt, the waitlist.
4. **Check.** The model's draft reply is compared with what it was given (`app/agent/grounding.py`). Rejected: a price or time that was not in the facts, a link or phone number nobody provided, "paid" or "confirmed" or "cancelled" when no tool did it, a deposit waiver the salon never offered, mentions of tools or JSON. The model gets one chance to repair the reply; after that code's templates answer.
5. **Send.** The customer gets the model's words around code's words. Anything that changed something (the hold, the payment steps, a cancellation, the waitlist, the hand-over) is worded by code from the tool result and is always included.

## Tools

| Tool | What it does | Guarded by |
|---|---|---|
| `find_slots(service, date, window?, time?, stylist?)` | Searches the real diary, returns up to 3 slots with ids | service must match the menu, date within 60 days, stylist must do that service |
| `reserve_slot(slot_id)` | Holds one offered slot; the system then handles the deposit | only slot ids offered in an earlier turn, one reserve per turn, max active bookings per customer |
| `my_bookings()` | Lists the customer's upcoming bookings | their own bookings only |
| `cancel_booking(ref?)` | Cancels one of them | their own bookings only; refunds parked for review; waitlist offered the slot |
| `join_waitlist(service, date)` | Joins the waitlist for a full day | idempotent per customer, service and day |
| `hand_to_owner(reason, note)` | Pauses the bot and tells the owner | owner is notified; conversation switches to hand-over |

Owner tools (`app/agent/admin.py`): `bookings_on(date)`, `deposits(from, to)`, `cancel_booking(ref)` (the customer is told and any refund is queued).

## Platform administrator assistant

When `AGENT_MODE=on`, natural-language platform questions are handled by `app/agent/platform_assistant.py`. The model can read business status/activity and aggregate totals through read-only tools. It cannot change, warn, or delete a business. Those actions remain in `app/platform.py` behind explicit commands and their confirmation rules. Platform turns are recorded in `agent_turns` with `salon_id=NULL`, joined to the full reply in `/ops/conversation-trace`, and use a sender-scoped, 30-minute conversational context that is purged with the agent retention job. With the agent disabled or unavailable, the platform hub shows its deterministic help text.

## Salon registration

`app/onboarding_agent.py` uses the same `AgentModel` and bounded `drive()` loop for natural intake. The model can extract several fields from one message and phrase follow-up questions in English, Swahili, or Sheng. Its only tool returns evidence quotes from the current message; code checks each quote, applies the existing name, service, duration, hours, price, and deposit validators, and persists only valid values. Earlier conversation text helps the model understand the exchange but cannot serve as evidence for a new value.

The registration agent cannot create salons or issue invite links. `app/onboarding_chat.py` validates the complete draft and requires a clear final yes before its transaction creates the salon, stylists, and services. Duplicate-name checks, registration limits, cancellation, and the deterministic fallback remain code-controlled. Registration model turns are written to `agent_turns` with no salon id, so `/ops/agent-trace` can show whether the model or a safe template answered before a salon exists. These traces use the existing retention period.

## Conversation evidence

`/ops/conversation-trace` records incoming chat text or transcripts, the resolved salon when applicable, every outgoing text/button response with its provider send result, and the matching model/tool trace when the agent handled that turn. Message content is retained as written (up to the application limits); sender IDs in the thread are masked. A successful send result means the channel accepted the send request; it is not a read receipt. This includes deterministic registration prompts and safety fallbacks, which are absent from `/ops/agent-trace` by itself. The endpoint requires the ops key and is limited to the configured agent retention period (14 days by default). Existing agent turns are backfilled when the migration runs, but old deterministic replies cannot be recovered because they were not stored.

This project uses its existing custom tool loop; it does not add LangChain as another orchestration dependency.

## What cannot go wrong, by construction

- The model never supplies a price, a deposit amount or a payment state. Amounts come from the menu and the salon's deposit setting; payment is settled by the Daraja callback or the owner's tap.
- The model cannot book a slot in the same turn it offers it: a customer picks, by tapping or by typing.
- A retried message (crash, redelivery) resends the stored reply instead of asking the model again, so the customer never gets two different answers (`agent_turns`).
- Customer text is data: injection attempts are screened before the model, and the model's brief tells it so; even a model that obeyed one could only call the tools above with validated arguments.

## When the model is not there

| Situation | What happens |
|---|---|
| Host down, circuit breaker open, or daily budget spent | the keyword flow answers (`agent_turns.outcome = 'unavailable'`) |
| Model replied but the checker rejected it twice | code templates built from the tool results (`outcome = 'templates'`) |
| Model failed after it already booked or cancelled | code's confirmation of what happened is still sent |

## Verify it

```bash
python scripts/probe_tools.py                              # does your host do tool calling? one line per backend and wire format
python scripts/agent_eval.py --models --out docs/AGENT_EVAL.md   # model vs keyword rules on 33 hard messages
curl -s -H "X-Ops-Key: $OPS_API_KEY" https://$DOMAIN/ops/agent-trace?limit=5 | python -m json.tool   # the last turns: said, tools, sent
curl -s -H "X-Ops-Key: $OPS_API_KEY" https://$DOMAIN/ops/conversation-trace?limit=20 | python -m json.tool  # complete turns and actual sent replies
docker compose exec -T app python scripts/doctor.py        # includes an agent line per backend
```

Settings: `AGENT_MODE`, `AGENT_PROTOCOL` (`auto`, `tools`, `json`), `AGENT_MAX_STEPS`, `AGENT_CALL_TIMEOUT_S`, `AGENT_TURN_TIMEOUT_S`, `AGENT_REASONING_EFFORT`, `AGENT_RETENTION_DAYS`. Each step is a model call counted against `LLM_DAILY_LIMIT`.
