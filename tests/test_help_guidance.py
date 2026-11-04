import asyncio
import copy
import json
from datetime import datetime, timezone
from pathlib import Path
from types import SimpleNamespace

from app.agent.admin import admin_prompt
from app.agent.bare_assistant import _system_prompt as bare_system_prompt
from app.agent.platform_assistant import _system_prompt as platform_system_prompt
from app.agent.policy import load_policy
from app.agent.prompt import system_prompt
from app.messages import ensure_help_hint, t
from app.onboarding_agent import _prompt as registration_prompt
from app.onboarding_agent import advance as registration_advance
from app.onboarding_chat import _registration_help
from app.platform import HELP_TEXT_SW, help_text, role_text
from app.smalltalk import asks_help


ROOT = Path(__file__).parents[1]
POLICY_PATH = ROOT / "config" / "remboai_agent_policy.json"
POLICY = load_policy(str(POLICY_PATH))
NOW = datetime(2026, 11, 3, 9, tzinfo=timezone.utc)
BUSINESS = {
    "name": "Brian's Kinyozi",
    "open_hours": {},
    "deposit_kes": 0,
}


def test_opening_messages_invite_help_in_english_and_swahili():
    examples = {
        "en": {
            "welcome": {"salon": "Brian's Kinyozi", "example": "Fade Saturday afternoon"},
            "welcome_back_ask": {"example": "Fade Saturday afternoon"},
            "main_identity": {}, "main_help": {}, "main_hello": {},
            "main_voice": {}, "main_photo": {}, "voice_failed": {},
            "ob_ask_name": {}, "ob_platform_owner_confirm": {},
        },
        "sw": {
            "welcome": {"salon": "Kinyozi cha Brian", "example": "Fade Jumamosi mchana"},
            "welcome_back_ask": {"example": "Fade Jumamosi mchana"},
            "main_identity": {}, "main_help": {}, "main_hello": {},
            "main_voice": {}, "main_photo": {}, "voice_failed": {},
            "ob_ask_name": {}, "ob_platform_owner_confirm": {},
        },
    }
    for lang, keys in examples.items():
        expected = "HELP" if lang == "en" else "MSAADA"
        for key, values in keys.items():
            assert expected in t(lang, key, **values), (lang, key)


def test_help_sheets_are_short_copyable_and_bilingual():
    customer_en = t("en", "assistant_help", salon="Brian's Kinyozi", example="Fade tomorrow")
    customer_sw = t("sw", "assistant_help", salon="Kinyozi cha Brian", example="Fade kesho")
    owner_en = t("en", "owner_help")
    owner_sw = t("sw", "owner_help")

    for body in (customer_en, customer_sw):
        assert "1. " in body and "4. " in body
        assert "prices" in body.lower() or "bei" in body.lower()
    for body in (owner_en, owner_sw):
        assert "\n1. " in body and "\n7. " in body
        assert "payment till 1234567" in body
        assert "payment pochi" in body and "payment send" in body and "payment airtel" in body
    assert "cancel 2" in owner_en and "ghairi 2" in owner_sw
    assert "del Brian's Kinyozi YES" in help_text("en")
    assert "del Brian's Kinyozi YES" in HELP_TEXT_SW
    assert "del Brian's Kinyozi YES" in role_text("sw")


def test_model_prompts_teach_help_and_first_reply_hint_for_each_chat_role():
    scope = " ".join(POLICY["bare_assistant"]["scope"]).lower()
    assert "never list businesses" in scope and "ask a customer to choose a shop" in scope
    assert "ask the business to send or resend it" in scope
    assert "business_selection_reply_patterns" in POLICY["bare_assistant"]
    assert "invite link" in POLICY["bare_assistant"]["business_link_required_reply"]["en"].lower()
    assert "kiungo" in POLICY["bare_assistant"]["business_link_required_reply"]["sw"].lower()
    bare = bare_system_prompt(POLICY, "en", first_reply=True)
    assert "Type HELP anytime to see what I can do." in bare
    assert "HELP GUIDE, KISWAHILI" in bare and "Jinsi ya kutumia RemboAI" in bare
    assert "explain each option in simple words" in json.dumps(POLICY["bare_assistant"]).lower()
    assert any("even if that takes extra lines" in line for line in POLICY["bare_assistant"]["response_style"])

    booking = system_prompt(
        salon=BUSINESS, services=[{"id": "s1", "name": "Fade", "price_kes": 500, "duration_min": 30}],
        staff_by_service={"s1": ["Brian"]}, now=NOW, st={}, deposit_line="Deposit: none is required.",
        channel="Telegram", lang_hint="English", policy=POLICY, first_reply=True, help_example="Fade",
    )
    assert "Type HELP anytime to see what I can do." in booking
    assert 'book by sending "Fade Saturday afternoon"' in booking
    assert 'book by sending "Fade Jumamosi mchana"' in booking

    owner_en = admin_prompt(BUSINESS, NOW, POLICY, lang="en", first_reply=True)
    owner_sw = admin_prompt(BUSINESS, NOW, POLICY, lang="sw", first_reply=True)
    assert "Type HELP anytime to see what I can do." in owner_en
    assert "Tuma MSAADA wakati wowote uone ninavyoweza kukusaidia." in owner_sw
    assert "A question about what a command does is not a request to run it." in owner_en
    assert "payment pochi 0712 345 678" in owner_sw

    platform_en = platform_system_prompt(POLICY, "en", first_reply=True)
    platform_sw = platform_system_prompt(POLICY, "sw", first_reply=True)
    assert "Type HELP anytime to see what I can do." in platform_en
    assert "PLATFORM HELP REFERENCE, KISWAHILI" in platform_en
    assert "Tuma MSAADA wakati wowote uone ninavyoweza kukusaidia." in platform_sw


def test_first_identity_replies_include_the_help_invitation_in_both_languages():
    for lang, prompt in (("en", "Type HELP"), ("sw", "Tuma MSAADA")):
        for key in ("customer_self_identity", "owner_self_identity"):
            assert prompt in t(lang, key, salon="Brian's Kinyozi")


def test_registration_help_uses_model_without_changing_registration_fields(monkeypatch):
    from app.agent.client import Step

    class Pool:
        async def execute(self, *_args):
            return "OK"

    class Model:
        def __init__(self):
            self.tools = None

        async def step(self, _system, _messages, tools):
            self.tools = tools
            return Step(
                text="Tell me when your business opens and closes. For example, eight in the morning to eight at night.",
                backend="test", model="test-model", ms=2,
            )

    class Wa:
        async def typing(self, _sender):
            return None

    state = {
        "step": "ask_hours", "lang": "en", "help_hint_sent": True,
        "data": {"salon": "Brian's Kinyozi", "services": [{"name": "Fade", "price": 500, "minutes": 30}]},
    }
    original_step, original_data = state["step"], copy.deepcopy(state["data"])
    model = Model()
    ctx = SimpleNamespace(
        settings=SimpleNamespace(agent_mode="on", agent_policy_path=str(POLICY_PATH)),
        agent=model, pool=Pool(), wa=Wa(),
    )
    sent = []

    async def capture_send(_ctx, _pid, _to, body):
        sent.append(body)

    monkeypatch.setattr("app.onboarding_agent.ob._say", capture_send)

    handled = asyncio.run(registration_advance(ctx, "owner-1", "telegram-1", "what does this step mean", state))

    assert handled
    assert model.tools == []
    assert state["step"] == original_step
    assert state["data"] == original_data
    assert len(sent) == 1 and "eight in the morning" in sent[0]


def test_registration_help_explains_the_current_step_in_both_languages():
    english = _registration_help({"lang": "en", "step": "ask_payment_method"})
    swahili = _registration_help({"lang": "sw", "step": "ask_payment_method"})
    assert "seven-digit M-PESA till" in english
    assert "Pochi la Biashara" in english and "Airtel Money" in english
    assert "tarakimu saba" in swahili
    assert "Pochi la Biashara" in swahili and "Airtel Money" in swahili
    assert "jibu swali" in swahili

    prompt = registration_prompt({"step": "ask_hours", "data": {}}, "sw", POLICY)
    assert "Current step: ask_hours" in prompt
    assert "andika MSAADA" in prompt or "MSAADA" in prompt


def test_help_detection_covers_plain_language_questions_without_stealing_actions():
    for text in (
        "help", "msaada", "what does pending payments do", "explain the commands",
        "naweza kufanya nini", "amri hizi zina maana gani", "nieleze",
    ):
        assert asks_help(text), text
    for text in ("help me cancel my booking", "cancel 2", "Fade tomorrow"):
        assert not asks_help(text), text


def test_first_reply_hint_is_added_once():
    first = ensure_help_hint("Welcome to RemboAI.", "en")
    assert first.endswith("Type HELP anytime to see what I can do.")
    assert ensure_help_hint(first, "en") == first
    swahili = ensure_help_hint("Karibu RemboAI.", "sw")
    assert swahili.endswith("Tuma MSAADA wakati wowote uone ninavyoweza kukusaidia.")
    assert "Type HELP" in ensure_help_hint("I can help you send a payment.", "en")
    assert ensure_help_hint("Type help to see options.", "en") == "Type help to see options."
