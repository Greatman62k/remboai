#!/usr/bin/env python3
"""Run deterministic response-copy and corpus coverage checks without model or provider calls."""
from __future__ import annotations

import sys
from collections import Counter
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

from app import guard, smalltalk  # noqa: E402
from response_quality_corpus import CASES, reference_reply  # noqa: E402


def evaluate() -> tuple[Counter, Counter, Counter, int, int]:
    languages: Counter = Counter()
    categories: Counter = Counter()
    text_only_language: Counter = Counter()
    checked = 0
    text_only_matches = 0
    seen_inputs: set[str] = set()
    for case in CASES:
        text = case["input"]
        normalized = text.casefold().strip()
        if normalized in seen_inputs:
            raise AssertionError(f"duplicate input: {text}")
        seen_inputs.add(normalized)

        language = case["input_language"]
        observed = smalltalk.language(text, case["authoritative_backend_state"]["customer_language"])
        expected = "en" if language == "en" else "sw"
        if observed != expected:
            raise AssertionError(f"{case['id']}: expected {language}, detected {observed}: {text}")
        guessed = smalltalk.language(text, "")
        text_only_language[guessed] += 1
        text_only_matches += guessed == expected
        if case["scenario"] == "prompt_injection" and not guard.injection(text):
            raise AssertionError(f"{case['id']}: injection was not screened")

        reply = reference_reply(case)
        folded = reply.casefold()
        for fact in case["required_response_facts"]:
            if fact.casefold() not in folded:
                raise AssertionError(f"{case['id']}: missing required reply fact {fact!r}: {reply}")
        marker = case["reply_locale_evidence"]
        if marker.casefold() not in folded:
            raise AssertionError(f"{case['id']}: reply does not match its selected locale: {reply}")
        for claim in case["forbidden_claims"]:
            if claim.casefold() in folded:
                raise AssertionError(f"{case['id']}: forbidden claim {claim!r}: {reply}")

        languages[language] += 1
        categories[case["scenario"]] += 1
        checked += 1
    return languages, categories, text_only_language, checked, text_only_matches


def main() -> int:
    languages, categories, text_only_language, checked, text_only_matches = evaluate()
    print(f"Response copy checks: {checked}/{len(CASES)} passed")
    print("Cases by input language: " + ", ".join(f"{key}={languages[key]}" for key in ("en", "sw", "sheng")))
    print(f"Language routing with stored preference (English vs Kiswahili): {checked}/{len(CASES)} passed")
    print("Text-only guess from the two-way en/sw detector: " + ", ".join(f"{key}={text_only_language[key]}" for key in ("en", "sw")))
    print(f"Text-only guess matches the coarse English/Kiswahili route: {text_only_matches}/{checked}")
    print(f"Scenario categories: {len(categories)}")
    print("Sheng inputs use the current Kiswahili fallback for templates; the rule-based detector does not label Sheng separately.")
    print("Scope: static copy, state-claim and coarse language-routing checks; no live model calls.")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
