# Agent eval, second corpus

`scripts/agent_corpus_v2.json` holds **117 messages in 13 categories**. It sits beside the original 33-message corpus (`scripts/agent_corpus.json`), which is unchanged, and uses the same harness, fixture salon and scoring.

The original corpus answers "is the model better than keyword rules on hard messages?". This one is for a different question: **where does the receptionist still go wrong?** It is bigger, it covers things the first one did not (dates, pricing pressure, voice-note text, compound requests, more injection styles, replies that must not act), and its context items carry the conversation that offered the slots.

## Run it

```
python scripts/agent_eval.py --corpus scripts/agent_corpus_v2.json                      # keyword rules only, no keys
python scripts/agent_eval.py --models --corpus scripts/agent_corpus_v2.json --verbose   # also the configured model, listing every miss
python scripts/agent_eval.py --models --corpus scripts/agent_corpus.json --corpus scripts/agent_corpus_v2.json --out docs/AGENT_EVAL.md
```

## What is in it

| Category | n | What it tests |
|---|---|---|
| clean | 12 | Plain requests, no tricks. Guards against regressions on the easy path. |
| swahili | 13 | Kiswahili and Sheng, Swahili clock times (saa nane = 14:00), service aliases, a named stylist, kesho kutwa, wiki ijayo. |
| typos | 6 | Misspellings, shorthand and shouting (fadde, brades, TMRW). |
| dates | 8 | Calendar dates (10 November, the 5th, tarehe 6), day after tomorrow, "Friday next week". Today is Monday 2 Nov 2026 in the fixture. |
| context | 15 | Short replies that only make sense next to the slots just offered (the one with Amina, ya saa nne), a change of day, and replies that must **not** reserve (ok, hmm let me think). Every item carries the conversation that offered the slots. |
| question | 13 | Prices, durations, hours, the deposit, a service not on the menu, facts the business never supplied (address, card payments). Answer from the facts, invent nothing. |
| pricing | 2 | The customer pushes on money: asks for a discount. No invented discounts. |
| cancel_waitlist | 8 | Cancel in English and Swahili, and join the waitlist. |
| handoff | 8 | Payment disputes, speaking to a person, health, complaints, a special request: `hand_to_owner`, with the right reason. |
| safety | 14 | Prompt-extraction in English and Swahili, impersonating the owner, asking to mark a deposit paid, text that imitates a tool result, an injected tool call, off-topic requests. |
| missing | 8 | One thing short of a lookup (no service, or no day). The right move is one short question. |
| compound | 4 | More than one request in one message. |
| voice | 6 | What a transcribed voice note looks like: lowercase, no punctuation, filler words. |

## How items are scored

- Only the model's **first move** is scored: the tool it calls first, or its words if it answers without a tool.
- **Right action** means the first tool is one of the allowed ones (or it answered in words when that was allowed). **Right action and details** also needs the arguments to match: service, day offset from the fixture's Monday, window, 24-hour time, stylist, slot id, hand-off reason.
- When the right move is to answer in words, the item can require one of `reply_has_any` and forbid every `reply_lacks`. Where several first moves are reasonable (for example `join_waitlist` or a diary check first), the item lists all of them.
- Items marked `why` explain a non-obvious label and point at the policy rule behind it.
- Every score is shown with a **95% Wilson interval**. Compare systems by whether their intervals overlap, not by the point estimate. A category with fewer than 10 items is an anecdote.

## How the gold labels were made

They were written against `config/remboai_agent_policy.json` (the booking receptionist rules, the Swahili time guide, the tool descriptions), not against the model's behaviour, and the model was not run on them before they were committed. The rules that decide the contested cases:

- Missing service or day: ask one short question, call no tool.
- A pick is only a pick when it names a slot; "ok" after three offered slots is not, so the right move is to ask which.
- A named stylist is passed to `find_slots`, even one the salon does not have.
- A service not on the menu: say so and name the closest listed one.
- Never add up totals, invent discounts, or state a phone number, link or price that is not in the business facts.
- Complaints, refunds, health matters, special requests and "let me speak to a person" go to `hand_to_owner`.
- Never mention tools. The leak checks therefore forbid tool names in the reply, which a polite refusal will not contain.

`tests/test_agent_corpus.py` checks the labels mechanically: schema, that every weekday word agrees with its `day_offset`, that Swahili and English clock times agree with the policy's own guide, that the slot picks match the fixture, and that no message is shared with the original corpus. Deliberately corrupting a label makes it fail.

## Baseline

The keyword rules are what answers a customer when the model is unavailable, so they are measured too. This corpus found real faults in them, which were then fixed. Scored the same way before and after (right action **and** right details):

| | Original corpus (33) | This corpus (117) |
|---|---|---|
| Before the fixes | 20/33 (44-75%) | 75/117 (55-72%) |
| After the fixes | 26/33 (62-89%) | 95/117 (73-87%) |

What was found and fixed: "Friday next week" and "Wednesday next week" resolved to **this** week; calendar dates ("10 November", "the 5th", "tarehe 6") were not understood; "I'm running late", "can I reschedule" and "nimechelewa" were treated as a request to **cancel**, and a typed cancel removed the soonest booking without asking even when the customer had several; "good morning" was read as a request for the morning; and typos and Swahili words for a service ("fadde", "kusukwa", "ndevu", "mtoto") were not matched. The fixes are in `app/timeparse.py`, `app/ai/llm.py` and `app/lexicon.json`, with regression tests in `tests/test_keyword_rules.py` and `tests/test_cancel_which.py`.

The scoring of the rules was also corrected: when a booking lacks a service or a day, production asks a question instead of searching, so the harness now scores that as "answered in words", not as a search. Both columns above use the corrected scoring.

What the rules still cannot do, by design: pick a slot by its time or stylist ("the 2pm one", "Brian at 2"), because they never see the slots on offer; honour a named stylist; hand a complaint, refund or health question to the owner (0 of 8 `handoff` items); or answer a question in words. The `question`, `safety` and `pricing` rows flatter them, because they cannot write a reply and an item that expects words from the model scores as "did not act". Read those rows for the model only.

Model results are not in this file yet. Run the second command above and paste the table here.

## What it does not measure

- Only the first move. A full turn is several model calls, and later steps (the reply after a tool result, the payment text) are checked elsewhere.
- Reply checks are substring tests. They can pass an answer that is wrong in unforeseen words and fail one that is fine. They catch the obvious failures, not subtle ones.
- Photos, payment screenshots, the owner assistant, registration and the payment flow itself.
- One fixture salon (five services, two stylists, Monday to Saturday, a KES 200 deposit) and one fixed date.
- Real customers. The messages were written by one reviewer. Two or three Kenyan barbers or customers reading the Swahili and Sheng rows would catch phrasing and labels that look right on paper and are not.

## Keep it honest

Do not tune prompts or policy against these items. If you do, record it and treat them as tuned-on: they stop being evidence of generalisation. When they fail, fix the underlying behaviour, then add **new** messages (ideally real ones from pilot shops) so there is always a set the system has not seen.
