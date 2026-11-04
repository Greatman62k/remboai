from pathlib import Path

from app.agent.bare_assistant import _request_is_out_of_scope
from app.agent.policy import load_policy, matches_any


POLICY = load_policy(Path(__file__).parents[1] / "config" / "remboai_agent_policy.json")
BARE = POLICY["bare_assistant"]


def test_bare_assistant_recognizes_capability_identity_and_contextual_questions():
    assert not _request_is_out_of_scope(POLICY, "What do you have?")
    assert not _request_is_out_of_scope(POLICY, "What can I do here?")
    assert not _request_is_out_of_scope(POLICY, "Who am I?")
    assert not _request_is_out_of_scope(POLICY, "Clear", has_context=True)
    assert _request_is_out_of_scope(POLICY, "Clear", has_context=False)


def test_bare_reply_guard_allows_normal_prose_but_detects_code():
    patterns = BARE["unsafe_reply_patterns"]
    assert not matches_any(patterns, "Open the invite link from the shop's message.")
    assert not matches_any(patterns, "1. Type REGISTER to begin setup.\n2. Ask the shop for its link.")
    assert matches_any(patterns, "from pathlib import Path")
    assert matches_any(patterns, "```python\nprint('hello')\n```")


def test_bare_assistant_still_blocks_unrelated_topics():
    assert _request_is_out_of_scope(POLICY, "Can you help me with maths?")
    assert _request_is_out_of_scope(POLICY, "Write Python code for this")


def test_bare_identity_answers_must_disclaim_verification_and_cannot_assign_roles():
    assert matches_any(BARE["identity_safe_reply_patterns"], "I can't verify who you are or your role from this chat.")
    assert matches_any(BARE["identity_claim_patterns"], "You are the platform administrator.")
    assert matches_any(BARE["identity_question_patterns"], "Who am I?")
