# Agent eval

Held-out messages the keyword rules were not tuned for. First move only. Date fixed at 2026-11-02.

| Decision maker | Right action | Right action and details | cancel | clean | context | handoff | hard | missing | question | safety | median ms |
|---|---|---|---|---|---|---|---|---|---|---|---|
| keyword rules | 25/33 | 19/33 | 1/2 | 6/6 | 0/5 | 0/2 | 4/9 | 2/2 | 5/5 | 1/2 | - |

The keyword-rules row is measured. Add the model rows by running `python scripts/agent_eval.py --models --out docs/AGENT_EVAL.md` with your keys in `.env`; categories: clean (plain requests), hard (Sheng, typos, Swahili clock, a stylist's name), context (a pick that depends on the slots just offered), question (answer in words), cancel, handoff, safety (injection), missing (ask, do not guess).
