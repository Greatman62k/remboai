from app.agent.grounding import Facts
from app.agent.visit_feedback import _explicit_sentiment, _valid_reply


def test_clear_feedback_ratings_are_classified_without_guessing():
    assert _explicit_sentiment("1") == "negative"
    assert _explicit_sentiment("2/5") == "negative"
    assert _explicit_sentiment("3") == "neutral"
    assert _explicit_sentiment("5") == "positive"
    assert _explicit_sentiment("The barber was rude") == "negative"
    assert _explicit_sentiment("The cut was great") == "positive"
    assert _explicit_sentiment("It took a while") is None


def test_negative_feedback_reply_must_apologize_ask_and_commit_to_improvement():
    facts = Facts(text=["Business: Zuri Cuts", "Service: Fade"])
    assert _valid_reply(
        "I'm sorry that happened. What went wrong? We'll use your feedback to improve.",
        facts, "feedback", "negative", False,
    )
    assert not _valid_reply(
        "I'm sorry that happened. We will refund your deposit and fix it.",
        facts, "feedback", "negative", False,
    )
    assert not _valid_reply(
        "That sounds disappointing. What went wrong? We'll learn from this.",
        facts, "feedback", "negative", False,
    )


def test_welcome_and_final_detail_replies_must_complete_their_promised_steps():
    facts = Facts(text=["Business: Zuri Cuts", "Service: Fade"])
    assert _valid_reply(
        "Thank you for visiting. You're always welcome back. How did it go?",
        facts, "welcome", "positive", False,
    )
    assert not _valid_reply(
        "Thank you for visiting. How did it go?",
        facts, "welcome", "positive", False,
    )
    assert _valid_reply(
        "I'm sorry that happened. Thank you for explaining. We'll use it to improve.",
        facts, "details", "negative", False,
    )
    assert not _valid_reply(
        "I'm sorry that happened. We'll use it to improve.",
        facts, "details", "negative", False,
    )


def test_model_cannot_claim_owner_notification_that_code_did_not_send():
    facts = Facts(text=["Business: Zuri Cuts", "Service: Fade"])
    reply = "Thank you for the feedback. I've shared it with the owner."
    assert not _valid_reply(reply, facts, "feedback", "positive", False)
    assert _valid_reply(reply, facts, "feedback", "positive", True)
