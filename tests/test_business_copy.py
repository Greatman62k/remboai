import json
from datetime import datetime, timezone
from pathlib import Path

from app.agent.admin import admin_prompt
from app.agent.grounding import Facts, check
from app.agent.prompt import system_prompt
from app.ai.vision import build_payment_prompt, build_prompt
from app.messages import t


def test_unlinked_assistant_messages_name_both_supported_business_types():
    english = " ".join(t("en", key) for key in (
        "main_identity", "main_help", "main_assistant_fallback", "main_thanks",
    )).lower()
    swahili = " ".join(t("sw", key) for key in (
        "main_identity", "main_help", "main_assistant_fallback", "main_thanks",
    )).lower()

    assert "barbershop" in english and "salon" in english
    assert "kinyozi" in swahili and "saluni" in swahili


def test_registration_prompts_use_business_neutral_name_and_summary_labels():
    for lang, name_word, summary_word in (("en", "shop", "business"), ("sw", "biashara", "biashara")):
        name_prompt = t(lang, "ob_ask_name").lower()
        name_error = t(lang, "ob_name_bad").lower()
        summary = t(lang, "ob_confirm_all", salon="Example Shop", owners="Brian", lines="Fade", hours="09:00-18:00",
                    deposit=0, payment="none").lower()

        assert name_word in name_prompt
        assert summary_word in summary
        assert "salon name" not in name_prompt + name_error
        assert "jina la saluni" not in name_prompt + name_error


def test_owner_and_safety_messages_do_not_label_every_owner_as_a_salon_owner():
    assert "business owners" in t("en", "owner_help").lower()
    assert "mmiliki wa biashara" in t("sw", "owner_help").lower()

    facts = Facts()
    assert "claims an owner hand-off that no tool made" in check(
        "I have notified the business owner who will reply shortly.", facts,
    )


def test_model_policy_uses_business_neutral_assistant_context():
    policy_path = Path(__file__).parents[1] / "config" / "remboai_agent_policy.json"
    policy = json.loads(policy_path.read_text())

    assert "business named" in policy["booking_receptionist"]["identity"].lower()
    assert "business context" in policy["salon_owner_assistant"]["identity"].lower()
    assert "barbershop" in policy["registration"]["step_instructions"]["ask_type"].lower()
    assert "salon" in policy["registration"]["step_instructions"]["ask_type"].lower()

    business = {"name": "Brian's Kinyozi", "open_hours": {}, "deposit_kes": 0}
    receptionist_prompt = system_prompt(
        salon=business, services=[], staff_by_service={}, now=datetime.now(timezone.utc), st={},
        deposit_line="Deposit: none is required.", channel="Telegram", lang_hint="English", policy=policy,
    )
    owner_prompt = admin_prompt(business, datetime.now(timezone.utc), policy)

    for prompt in (receptionist_prompt, owner_prompt):
        assert "Business: Brian's Kinyozi" in prompt
        assert "LIVE CHAT CONTEXT" in prompt or "LIVE BUSINESS CONTEXT" in prompt
        assert "LIVE SALON CONTEXT" not in prompt
        assert "VERIFIED SALON FACTS" not in prompt

    vision_prompt = build_prompt(["Fade"], policy)
    payment_prompt = build_payment_prompt("airtel_money", policy)
    assert "Available business services" in vision_prompt
    assert "business's configured manual payment provider" in payment_prompt
