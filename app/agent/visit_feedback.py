"""No-tools model replies for the short post-service feedback conversation."""
from __future__ import annotations

import json
import logging
import re

from app.agent.grounding import Facts, check
from app.agent.policy import PolicyError, load_policy
from app.messages import t

log = logging.getLogger("rembo.visit_feedback")

_FORMAT = re.compile(
    r"\s*SENTIMENT:\s*(POSITIVE|NEUTRAL|NEGATIVE)\s*\n\s*REPLY:\s*(.+?)\s*\Z", re.I | re.S
)
_NEGATIVE = re.compile(
    r"\b(?:bad|poor|terrible|horrible|awful|worst|rude|unhappy|disappointed|dissatisfied|unsatisfied|"
    r"not\s+(?:good|okay|ok|happy|satisfied)|didn't\s+like|did\s+not\s+like|"
    r"mbaya|sikuridhika|sijaridhika|sikufurahia|haikuwa\s+(?:nzuri|sawa)|"
    r"imeharibika|umeharibu|ameharibu|alinikata|ameniumiza|imeuma|haikunifurahisha)\b", re.I
)
_POSITIVE = re.compile(
    r"\b(?:great|excellent|amazing|wonderful|perfect|good|nice|loved|happy|satisfied|"
    r"nzuri|safi|poa|fiti|nimefurahia|nimeridhika|imekaa\s+sawa)\b", re.I
)
_RATING = re.compile(r"^\s*([1-5])\s*(?:/\s*5)?\s*[.!]*\s*$")
_APOLOGY = re.compile(r"\b(?:sorry|apolog\w*|regret|pole|samahani)\b", re.I)
_THANKS = re.compile(r"\b(?:thank\w*|thanks|asante|shukran)\b", re.I)
_WELCOME = re.compile(r"\b(?:welcome back|welcome anytime|always welcome|love to welcome|glad to have you|karibu tena|karibu wakati wowote|furahi kukuona tena)\b", re.I)
_IMPROVE = re.compile(r"\b(?:improv\w*|better|learn from|tuboresha|kuboresha|kuboreka|tujifunze)\b", re.I)
_UNSAFE_COMMITMENT = re.compile(
    r"\b(?:refund|compensat\w*|voucher|free service|discount|punguzo|fidia|rudisha pesa|"
    r"guarantee\w*|never happen again|will (?:fix|replace|redo|refund|call|contact)|"
    r"we(?:'ll| will) (?:fix|replace|redo|refund|call|contact)|"
    r"owner will (?:call|contact|reply|get back))\b", re.I
)
_OWNER_MENTION = re.compile(r"\b(?:owner|manager|mmiliki|meneja)\b", re.I)


def _explicit_sentiment(text: str) -> str | None:
    rating = _RATING.fullmatch(text or "")
    if rating:
        score = int(rating.group(1))
        return "negative" if score <= 2 else "positive" if score >= 4 else "neutral"
    if _NEGATIVE.search(text or ""):
        return "negative"
    if _POSITIVE.search(text or ""):
        return "positive"
    return None


def _fallback(phase: str, sentiment: str, lang: str, salon: str, service: str, owner_notified: bool) -> str:
    owner_notice = t(lang, "visit_owner_notice") if owner_notified else ""
    if phase == "welcome":
        return t(lang, "visit_welcome", salon=salon, service=service)
    if phase == "missed":
        return t(lang, "visit_missed_prompt", service=service, owner_notice=owner_notice)
    if phase == "details":
        return t(lang, "visit_feedback_details", owner_notice=owner_notice)
    if sentiment == "negative":
        return t(lang, "visit_feedback_negative", owner_notice=owner_notice)
    if sentiment == "positive":
        return t(lang, "visit_feedback_positive", salon=salon)
    return t(lang, "visit_feedback_neutral", salon=salon)


def _valid_reply(reply: str, facts: Facts, phase: str, sentiment: str, owner_notified: bool) -> bool:
    text = (reply or "").strip()
    if not text or len(text) > 360 or check(text, facts) or _UNSAFE_COMMITMENT.search(text):
        return False
    if not owner_notified and _OWNER_MENTION.search(text):
        return False
    if phase in ("missed", "details") or (phase == "feedback" and sentiment == "negative"):
        if not _APOLOGY.search(text) or not _IMPROVE.search(text):
            return False
        if phase == "details" and not _THANKS.search(text):
            return False
        if phase != "details" and "?" not in text:
            return False
    elif phase == "welcome":
        if not _THANKS.search(text) or not _WELCOME.search(text) or "?" not in text:
            return False
    elif not _THANKS.search(text):
        return False
    return True


async def reply(
    ctx,
    *,
    salon: str,
    service: str,
    lang: str,
    phase: str,
    feedback: str = "",
    previous_feedback: str = "",
    owner_notified: bool = False,
) -> tuple[str, str]:
    """Return (sentiment, grounded reply); use concise code wording if the model cannot safely answer."""
    explicit = _explicit_sentiment(feedback)
    sentiment = explicit or "neutral"
    if phase == "welcome":
        sentiment = "positive"
    elif phase in ("missed", "details"):
        sentiment = "negative"
    if not (getattr(ctx, "agent", None) is not None and getattr(ctx.settings, "agent_mode", "off") == "on"):
        return sentiment, _fallback(phase, sentiment, lang, salon, service, owner_notified)

    try:
        policy = load_policy(getattr(ctx.settings, "agent_policy_path", None))["visit_feedback"]
        system = "\n".join([
            "RemboAI visit feedback assistant",
            policy["identity"],
            "Configured response rules:\n" + "\n".join(f"- {rule}" for rule in policy["reply_rules"]),
            "The customer language is " + ("Swahili or Sheng" if lang == "sw" else "English"),
            "Output the required sentiment line and reply line exactly. No tools are available.",
        ])
        context = {
            "stage": phase.upper(),
            "verified_business_name": salon,
            "verified_service_name": service,
            "owner_notification_succeeded": owner_notified,
            "previous_customer_feedback_untrusted": previous_feedback[:700],
            "latest_customer_feedback_untrusted": feedback[:700],
        }
        step = await ctx.agent.step(
            system,
            [{"role": "user", "content": json.dumps(context, ensure_ascii=False)}],
            [],
        )
        match = _FORMAT.fullmatch(step.text or "") if not step.calls else None
        if match:
            model_sentiment = match.group(1).lower()
            candidate = match.group(2).strip()
            # Clear language signals override the model's label. If its wording does
            # not fit that label, reject it and use the matching safe response.
            if phase == "feedback":
                if explicit and explicit != model_sentiment:
                    raise ValueError("sentiment label conflicts with explicit customer feedback")
                sentiment = model_sentiment
            if _valid_reply(candidate, Facts(text=[f"Business: {salon}", f"Service: {service}"],
                                              user=[feedback, previous_feedback], handed_off=owner_notified),
                            phase, sentiment, owner_notified):
                return sentiment, candidate
            raise ValueError("model reply failed visit-feedback grounding or style checks")
        raise ValueError("model did not follow the visit-feedback reply format")
    except (PolicyError, ValueError) as exc:
        log.warning("visit-feedback model reply rejected: %s", exc)
    except Exception as exc:
        log.warning("visit-feedback model unavailable: %s", exc.__class__.__name__)
    return sentiment, _fallback(phase, sentiment, lang, salon, service, owner_notified)
