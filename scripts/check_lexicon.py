#!/usr/bin/env python3
"""Check a vocabulary file before using it.

    python scripts/check_lexicon.py                  # the shipped app/lexicon.json
    python scripts/check_lexicon.py filled.json      # a file somebody filled in: nothing is replaced
    python scripts/check_lexicon.py filled.json --write app/lexicon.json    # check it, then save a cleaned copy (generic words removed)

Prints how many entries each category has, every entry that would be dropped and why, and which of the file's example messages the word
lists get wrong (a salon message that would be turned away, an off-topic one that would reach the paid model). Exit code 1 if anything needs fixing."""
import os
import sys
from datetime import date
from pathlib import Path

args = [a for a in sys.argv[1:] if a != "--write"]
WRITE = Path(sys.argv[sys.argv.index("--write") + 1]) if "--write" in sys.argv else None
if WRITE:
    args = [a for a in args if a != str(WRITE)]
if args:
    os.environ["LEXICON_PATH"] = str(Path(args[0]).resolve())
sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
import json  # noqa: E402
from app import guard, lexicon  # noqa: E402
from app.screen import route  # noqa: E402


def main() -> int:
    lx = lexicon.load()
    print(f"file: {lexicon.PATH}\n")
    print(f"{'category':26} {'en':>5} {'sw':>5} {'sheng':>6}")
    for cat, block in lx.data.items():
        if cat != "examples":
            print(f"{cat:26} {len(block['en']):>5} {len(block['sw']):>5} {len(block['sheng']):>6}")
    bad = 0
    if lx.removed:
        print(f"\n{len(lx.removed)} entries are too generic and are ignored automatically (nothing to fix; --write removes them from the file):")
        for p in lx.removed[:12]:
            print("  -", p)
        if len(lx.removed) > 12:
            print(f"  ... and {len(lx.removed) - 12} more")
    if lx.problems:
        print(f"\n{len(lx.problems)} entries are malformed or misplaced and must be fixed:")
        for p in lx.problems:
            print("  -", p)
        bad += len(lx.problems)
    today, menu = date.today(), [{"name": "Fade", "aliases": [], "price_kes": 500}]
    missed = [m for m in lx.examples("on_topic") if route(guard.clean(m), menu, "idle", today) == "off"]
    leaked = [m for m in lx.examples("off_topic") if route(guard.clean(m), menu, "idle", today) == "model"]   # 'rules' is free: only the model costs money
    if missed:
        print(f"\n{len(missed)} salon messages the screen would TURN AWAY (add the missing word to a topic category):")
        for m in missed:
            print("  -", m)
    if leaked:
        print(f"\n{len(leaked)} off-topic messages the screen would SEND TO THE PAID MODEL (a word in services_styles or booking_words is too generic):")
        for m in leaked:
            print("  -", m)
    bad += len(missed) + len(leaked)
    if WRITE:
        WRITE.write_text(json.dumps(lexicon.cleaned(json.loads(lexicon.PATH.read_text(encoding="utf-8"))), indent=1, ensure_ascii=False) + "\n", encoding="utf-8")
        print(f"\ncleaned copy written to {WRITE}")
    print("\nAll good." if not bad else f"\n{bad} thing(s) to fix.")
    return 1 if bad else 0


sys.exit(main())

