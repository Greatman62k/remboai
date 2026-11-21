"""Conversation copy is checked against synthetic inputs and explicit backend states."""
import json
from pathlib import Path

import pytest

from app import guard, smalltalk
from app.agent.grounding import Facts, check
from app.agent.prompt import system_prompt
from app.messages import t
from scripts.response_quality_corpus import CASES, reference_reply

ROOT = Path(__file__).resolve().parents[1]


def test_response_corpus_is_broad_unique_and_records_state_contracts():
    assert len(CASES) >= 100
    assert len({case["id"] for case in CASES}) == len(CASES)
    assert len({case["input"].casefold() for case in CASES}) == len(CASES)
    assert len({case["scenario"] for case in CASES}) >= 20
    assert {case["input_language"] for case in CASES} == {"en", "sw", "sheng"}
    for case in CASES:
        assert case["input"].strip()
        assert case["expected_intent"]
        assert case["authoritative_backend_state"]
        assert case["required_response_facts"]
        assert isinstance(case["forbidden_claims"], list)
        assert case["acceptable_response_characteristics"]
        assert case["expected_reply_language"]


@pytest.mark.parametrize("case", CASES, ids=lambda case: case["id"])
def test_reference_responses_are_grounded_concise_and_language_consistent(case):
    reply = reference_reply(case)
    folded = reply.casefold()
    assert reply.strip()
    assert len(reply) <= 700
    for fact in case["required_response_facts"]:
        assert fact.casefold() in folded, (case["id"], fact, reply)
    for forbidden in case["forbidden_claims"]:
        assert forbidden.casefold() not in folded, (case["id"], forbidden, reply)

    expected = "en" if case["input_language"] == "en" else "sw"
    preferred_language = case["authoritative_backend_state"]["customer_language"]
    assert smalltalk.language(case["input"], preferred_language) == expected, case["id"]
    assert case["reply_locale_evidence"].casefold() in folded, (case["id"], reply)
    if case["scenario"] == "prompt_injection":
        assert guard.injection(case["input"]), case["id"]


def test_quality_prompt_matches_language_without_forcing_sheng():
    policy = json.loads((ROOT / "config/remboai_agent_policy.json").read_text())
    prompt = system_prompt(
        salon={"name": "Brian's Kinyozi", "open_hours": {}, "deposit_kes": 0},
        services=[{"id": "s1", "name": "Fade", "aliases": [], "price_kes": 500, "duration_min": 30}],
        staff_by_service={"s1": ["Brian"]}, now=__import__("datetime").datetime(2026, 11, 21),
        st={"state": "held"}, deposit_line="Deposit: KES 200.", channel="Telegram",
        lang_hint="Swahili or Sheng", policy=policy,
    )
    assert "use Sheng only when the customer uses it" in prompt
    assert "A payment prompt, payment claim, or held slot is not a completed payment" in prompt
    assert "ask only one question" in prompt


def test_grounding_rejects_invented_price_and_unearned_payment_or_booking_claims():
    facts = Facts(text=["Menu: Fade, KES 500"], prices={500})
    assert "amount 900 was not in the facts" in check("Fade costs KES 900.", facts)
    assert "claims a payment (only the payment system may)" in check("Your payment is confirmed.", facts)
    assert "claims a confirmed booking that no tool confirmed" in check("Your booking is confirmed.", facts)


def test_unknown_service_copy_does_not_repeat_or_invent_the_requested_service():
    case = next(case for case in CASES if case["id"] == "unknown_service-en-1")
    reply = reference_reply(case).casefold()
    assert "fade" in reply and "beard trim" in reply
    assert "locs" not in reply and "perm" not in reply


def test_handoff_copy_does_not_promise_that_an_owner_will_reply_or_contact_the_customer():
    for lang in ("en", "sw"):
        for category in ("complaint", "human_request", "payment_dispute"):
            reply = guard.reply_for(category, lang).casefold()
            assert "will reply shortly" not in reply
            assert "will contact you" not in reply
        assert "saved" in guard.reply_for("waiting", "en").casefold() if lang == "en" else "hifadhi" in guard.reply_for("waiting", "sw").casefold()


def test_feedback_and_payment_copy_are_truthful_about_review_not_completion():
    assert "refund review" in t("en", "refund_notice", amount=200, ref="", why="the slot was taken", salon="Brian's Kinyozi")
    assert "refund is complete" not in t("en", "refund_notice", amount=200, ref="", why="the slot was taken", salon="Brian's Kinyozi").casefold()
    assert "not confirmed" in t("en", "stk_sent", amount=200, phone="0712 345 678").casefold()
    assert "haijathibitishwa" in t("sw", "stk_sent", amount=200, phone="0712 345 678").casefold()
    assert "what went wrong" in t("en", "visit_feedback_negative", owner_notice="").casefold()
    assert "nini hakikuenda vizuri" in t("sw", "visit_feedback_negative", owner_notice="").casefold()
    stale = t("en", "stale_message").casefold()
    assert "over 30 minutes old" in stale and "send it again" in stale
    assert "offline" not in stale
    claim_review = t("en", "till_review", salon="Brian's Kinyozi")
    assert "saved for Brian's Kinyozi to review" in claim_review
    assert "not confirmed" in claim_review
    evidence = t("en", "manual_payment_evidence", salon="Brian's Kinyozi")
    assert "owner must verify" in evidence and "Never send your PIN" in evidence
    assert "we will send the details" not in evidence.lower()
    cancelled_claim = t("en", "cancelled_pending_claim", when="Wed 14:00", salon="Brian's Kinyozi")
    assert "contact Brian's Kinyozi about the payment" in cancelled_claim
    assert "business will contact you" not in cancelled_claim.lower()
    expired = t("en", "till_hold_expired", salon="Brian's Kinyozi")
    assert "contact brian's kinyozi about next steps" in expired.casefold()
    assert "owner will contact you" not in expired.lower()
    unavailable = t("en", "till_verified_slot_unavailable", amount=200)
    assert "contact the business" in unavailable.lower()
    assert "business will contact you" not in unavailable.lower()


def test_thanks_replies_are_short_and_keep_the_current_conversation_in_view():
    english = t("en", "thanks")
    swahili = t("sw", "thanks")
    main_english = t("en", "main_thanks")
    main_swahili = t("sw", "main_thanks")
    assert english == "You're welcome! Message me whenever you need another booking."
    assert swahili == "Karibu! Ukihitaji miadi nyingine, niandikie."
    assert "open the business's invite link" in main_english
    assert "fungua kiungo cha mwaliko cha biashara" in main_swahili
    assert "opening hours" not in english
    assert "muda wa kufungua" not in swahili
