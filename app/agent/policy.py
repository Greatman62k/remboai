"""Load the live, operator-editable conversational policy.

The policy file is deliberately separate from Python so instruction and reply
quality updates can be picked up on the next turn. Invalid edits never replace
the last valid policy already loaded by this process.
"""
from __future__ import annotations

import copy
import hashlib
import json
import logging
import re
import threading
from pathlib import Path

log = logging.getLogger("rembo.agent.policy")

POLICY_VERSION = 1
_POLICY_CACHE: dict[str, tuple[str, dict]] = {}
_BAD_CONTENT: dict[str, str] = {}
_LOCK = threading.RLock()

_REQUIRED_CHECKS = {
    "unsupported_promise",
    "premature_creation",
    "unsupported_capture_claim",
    "generic_multi_detail_invite",
    "type_both",
    "per_service_deposit",
    "confirm_duration_ask",
    "premature_registration_fields",
}
_REQUIRED_PREMATURE_STEPS = {
    "ask_name", "ask_type", "ask_owner", "ask_prices", "confirm_services", "ask_durations", "ask_hours"
}


class PolicyError(ValueError):
    """The configured policy is invalid and no previously valid copy exists."""


def _strings(value, location: str) -> None:
    if not isinstance(value, list) or not all(isinstance(item, str) and item.strip() for item in value):
        raise PolicyError(f"{location} must be a list of non-empty strings")


def _validate(policy: object) -> dict:
    if not isinstance(policy, dict) or type(policy.get("version")) is not int or policy["version"] != POLICY_VERSION:
        raise PolicyError(f"policy version must be {POLICY_VERSION}")
    _strings(policy.get("prompt_wrapper"), "prompt_wrapper")

    bare = policy.get("bare_assistant")
    if not isinstance(bare, dict):
        raise PolicyError("bare_assistant must be an object")
    for key in ("identity", "purpose", "business_specific_questions"):
        if not isinstance(bare.get(key), str) or not bare[key].strip():
            raise PolicyError(f"bare_assistant.{key} must be a non-empty string")
    for key in ("known_facts", "boundaries", "response_style"):
        _strings(bare.get(key), f"bare_assistant.{key}")
    _strings(bare.get("scope"), "bare_assistant.scope")
    _strings(bare.get("in_scope_patterns"), "bare_assistant.in_scope_patterns")
    _strings(bare.get("follow_up_patterns", []), "bare_assistant.follow_up_patterns")
    _strings(bare.get("out_of_scope_patterns"), "bare_assistant.out_of_scope_patterns")
    _strings(bare.get("unsafe_reply_patterns"), "bare_assistant.unsafe_reply_patterns")
    _strings(bare.get("business_selection_reply_patterns"), "bare_assistant.business_selection_reply_patterns")
    for key in ("identity_question_patterns", "identity_safe_reply_patterns", "identity_claim_patterns"):
        _strings(bare.get(key), f"bare_assistant.{key}")
    for key in ("out_of_scope_reply", "model_unavailable_reply", "model_rejected_reply", "rate_limited_reply",
                "identity_unverified_reply", "business_link_required_reply"):
        replies = bare.get(key)
        if replies is None and key not in (
            "out_of_scope_reply", "identity_unverified_reply", "business_link_required_reply"
        ):
            continue
        if not isinstance(replies, dict) or not all(
            isinstance(replies.get(lang), str) and replies[lang].strip() for lang in ("en", "sw")
        ):
            raise PolicyError(f"bare_assistant.{key} must define en and sw messages")
    for name, patterns in (("in_scope_patterns", bare["in_scope_patterns"]),
                           ("follow_up_patterns", bare.get("follow_up_patterns", [])),
                           ("out_of_scope_patterns", bare["out_of_scope_patterns"]),
                           ("unsafe_reply_patterns", bare["unsafe_reply_patterns"]),
                           ("business_selection_reply_patterns", bare["business_selection_reply_patterns"]),
                           ("identity_question_patterns", bare["identity_question_patterns"]),
                           ("identity_safe_reply_patterns", bare["identity_safe_reply_patterns"]),
                           ("identity_claim_patterns", bare["identity_claim_patterns"])):
        for pattern in patterns:
            if len(pattern) > 2000:
                raise PolicyError(f"bare_assistant.{name} contains a pattern that is too long")
            try:
                re.compile(pattern, re.IGNORECASE)
            except re.error as exc:
                raise PolicyError(f"bare_assistant.{name} contains an invalid regex: {exc}") from exc

    admin = policy.get("platform_admin")
    if not isinstance(admin, dict):
        raise PolicyError("platform_admin must be an object")
    for key in ("identity", "purpose"):
        if not isinstance(admin.get(key), str) or not admin[key].strip():
            raise PolicyError(f"platform_admin.{key} must be a non-empty string")
    for key in ("communication", "tool_rules", "commands", "identity_policy", "response_style"):
        _strings(admin.get(key), f"platform_admin.{key}")

    receptionist = policy.get("booking_receptionist")
    if not isinstance(receptionist, dict):
        raise PolicyError("booking_receptionist must be an object")
    for key in ("identity", "purpose"):
        if not isinstance(receptionist.get(key), str) or not receptionist[key].strip():
            raise PolicyError(f"booking_receptionist.{key} must be a non-empty string")
    for key in ("language_rules", "workflow_rules", "safety_rules", "response_style"):
        _strings(receptionist.get(key), f"booking_receptionist.{key}")
    time_guide = receptionist.get("swahili_time_guide")
    if not isinstance(time_guide, dict) or not all(
        isinstance(key, str) and isinstance(value, str) for key, value in time_guide.items()
    ):
        raise PolicyError("booking_receptionist.swahili_time_guide must map phrases to explanations")

    salon_owner = policy.get("salon_owner_assistant")
    if not isinstance(salon_owner, dict):
        raise PolicyError("salon_owner_assistant must be an object")
    for key in ("identity", "purpose"):
        if not isinstance(salon_owner.get(key), str) or not salon_owner[key].strip():
            raise PolicyError(f"salon_owner_assistant.{key} must be a non-empty string")
    _strings(salon_owner.get("response_rules"), "salon_owner_assistant.response_rules")

    visit_feedback = policy.get("visit_feedback")
    if not isinstance(visit_feedback, dict):
        raise PolicyError("visit_feedback must be an object")
    if not isinstance(visit_feedback.get("identity"), str) or not visit_feedback["identity"].strip():
        raise PolicyError("visit_feedback.identity must be a non-empty string")
    _strings(visit_feedback.get("reply_rules"), "visit_feedback.reply_rules")

    registration = policy.get("registration")
    if not isinstance(registration, dict):
        raise PolicyError("registration must be an object")
    for key in ("identity", "purpose"):
        if not isinstance(registration.get(key), str) or not registration[key].strip():
            raise PolicyError(f"registration.{key} must be a non-empty string")
    for key in ("requirements", "conversation_rules", "product_facts", "capture_rules", "security_rules"):
        _strings(registration.get(key), f"registration.{key}")
    if not isinstance(registration.get("menu_instruction"), str) or not registration["menu_instruction"].strip():
        raise PolicyError("registration.menu_instruction must be a non-empty string")
    steps = registration.get("step_instructions")
    if not isinstance(steps, dict) or not steps or not all(
        isinstance(key, str) and isinstance(value, str) and value.strip() for key, value in steps.items()
    ):
        raise PolicyError("registration.step_instructions must map step names to non-empty instructions")

    checks = registration.get("reply_checks")
    if not isinstance(checks, dict) or not _REQUIRED_CHECKS.issubset(checks):
        raise PolicyError("registration.reply_checks is missing required checks")
    for name in _REQUIRED_CHECKS - {"premature_registration_fields"}:
        rule = checks[name]
        if not isinstance(rule, dict) or not isinstance(rule.get("pattern"), str) or not rule["pattern"]:
            raise PolicyError(f"registration.reply_checks.{name}.pattern must be a non-empty string")
        if len(rule["pattern"]) > 2000:
            raise PolicyError(f"registration.reply_checks.{name}.pattern is too long")
        if not isinstance(rule.get("problem"), str) or not rule["problem"].strip():
            raise PolicyError(f"registration.reply_checks.{name}.problem must be a non-empty string")
        try:
            re.compile(rule["pattern"], re.IGNORECASE)
        except re.error as exc:
            raise PolicyError(f"registration.reply_checks.{name}.pattern is invalid: {exc}") from exc
        if "steps" in rule and (
            not isinstance(rule["steps"], list)
            or not all(isinstance(step, str) and step for step in rule["steps"])
        ):
            raise PolicyError(f"registration.reply_checks.{name}.steps must be a list of step names")

    premature = checks["premature_registration_fields"]
    if not isinstance(premature, dict) or not _REQUIRED_PREMATURE_STEPS.issubset(premature):
        raise PolicyError("premature_registration_fields is missing required registration steps")
    for step in _REQUIRED_PREMATURE_STEPS:
        rule = premature[step]
        if not isinstance(rule, dict) or not isinstance(rule.get("pattern"), str) or not rule["pattern"]:
            raise PolicyError(f"premature_registration_fields.{step}.pattern must be a non-empty string")
        if len(rule["pattern"]) > 2000:
            raise PolicyError(f"premature_registration_fields.{step}.pattern is too long")
        if not isinstance(rule.get("problem"), str) or not rule["problem"].strip():
            raise PolicyError(f"premature_registration_fields.{step}.problem must be a non-empty string")
        try:
            re.compile(rule["pattern"], re.IGNORECASE)
        except re.error as exc:
            raise PolicyError(f"premature_registration_fields.{step}.pattern is invalid: {exc}") from exc

    extractor = policy.get("legacy_booking_extractor")
    if not isinstance(extractor, dict) or not isinstance(extractor.get("identity"), str) or not extractor["identity"].strip():
        raise PolicyError("legacy_booking_extractor.identity must be a non-empty string")
    for key in ("output_rules", "field_definitions"):
        _strings(extractor.get(key), f"legacy_booking_extractor.{key}")
    if not isinstance(extractor.get("menu_instruction"), str) or not extractor["menu_instruction"].strip():
        raise PolicyError("legacy_booking_extractor.menu_instruction must be a non-empty string")
    for key in ("date_terms", "time_windows", "swahili_time_guide"):
        mapping = extractor.get(key)
        if not isinstance(mapping, dict) or not all(isinstance(k, str) and isinstance(v, str) for k, v in mapping.items()):
            raise PolicyError(f"legacy_booking_extractor.{key} must map strings to strings")

    vision = policy.get("vision_matcher")
    if not isinstance(vision, dict) or not isinstance(vision.get("identity"), str) or not vision["identity"].strip():
        raise PolicyError("vision_matcher.identity must be a non-empty string")
    _strings(vision.get("output_rules"), "vision_matcher.output_rules")
    payment_vision = vision.get("payment_screenshot")
    if not isinstance(payment_vision, dict) or not isinstance(payment_vision.get("identity"), str) or not payment_vision["identity"].strip():
        raise PolicyError("vision_matcher.payment_screenshot.identity must be a non-empty string")
    _strings(payment_vision.get("output_rules"), "vision_matcher.payment_screenshot.output_rules")

    protocol = policy.get("tool_protocol")
    if not isinstance(protocol, dict) or not isinstance(protocol.get("reply_without_tools"), str):
        raise PolicyError("tool_protocol.reply_without_tools must be a string")
    _strings(protocol.get("tool_steps"), "tool_protocol.tool_steps")
    diagnostics = policy.get("diagnostics")
    if not isinstance(diagnostics, dict) or not isinstance(diagnostics.get("vision_color_test"), str):
        raise PolicyError("diagnostics.vision_color_test must be a string")
    tool_descriptions = policy.get("tool_descriptions")
    required_tool_groups = {"booking_receptionist", "salon_owner_assistant", "platform_admin", "registration"}
    if not isinstance(tool_descriptions, dict) or not required_tool_groups.issubset(tool_descriptions):
        raise PolicyError("tool_descriptions is missing an assistant group")
    for group in required_tool_groups:
        specs = tool_descriptions[group]
        if not isinstance(specs, dict) or not specs:
            raise PolicyError(f"tool_descriptions.{group} must be an object")
        for name, spec in specs.items():
            if (not isinstance(spec, dict) or not isinstance(spec.get("description"), str)
                    or not spec["description"].strip() or not isinstance(spec.get("parameters"), dict)):
                raise PolicyError(f"tool_descriptions.{group}.{name} is invalid")
            if not all(isinstance(k, str) and isinstance(v, str) for k, v in spec["parameters"].items()):
                raise PolicyError(f"tool_descriptions.{group}.{name}.parameters must map names to descriptions")
    repair_prompts = policy.get("repair_prompts")
    required_repairs = {"booking_receptionist", "salon_owner_assistant", "platform_admin", "bare_assistant", "registration"}
    if not isinstance(repair_prompts, dict) or not required_repairs.issubset(repair_prompts) or not all(
        isinstance(repair_prompts.get(group), str) and "{problems}" in repair_prompts[group]
        for group in required_repairs
    ):
        raise PolicyError("repair_prompts is missing an assistant template")
    return policy


def _default_path() -> Path:
    return Path(__file__).resolve().parents[2] / "config" / "remboai_agent_policy.json"


def load_policy(path: str | Path | None = None) -> dict:
    """Return the current policy, checking the file contents on every call.

    The small JSON file is read each turn, while parsing and schema validation
    happen only when its content changes. This also detects atomic file swaps
    and edits that preserve the file's timestamp and size.
    """
    configured = str(path or "").strip()
    source = Path(configured).expanduser() if configured else _default_path()
    source = source.resolve()
    key = str(source)

    with _LOCK:
        try:
            raw = source.read_bytes()
        except OSError as exc:
            cached = _POLICY_CACHE.get(key)
            if cached:
                issue = f"read:{exc.__class__.__name__}:{exc}"
                if _BAD_CONTENT.get(key) != issue:
                    log.error("could not read agent policy %s, retaining last valid policy: %s", source, exc)
                    _BAD_CONTENT[key] = issue
                return cached[1]
            raise PolicyError(f"cannot read agent policy {source}: {exc}") from exc

        digest = hashlib.sha256(raw).hexdigest()
        cached = _POLICY_CACHE.get(key)
        if cached and cached[0] == digest:
            return cached[1]
        if cached and _BAD_CONTENT.get(key) == digest:
            return cached[1]

        try:
            decoded = json.loads(raw)
            policy = _validate(decoded)
        except (UnicodeDecodeError, json.JSONDecodeError, PolicyError) as exc:
            if cached:
                log.error("invalid agent policy %s, retaining last valid policy: %s", source, exc)
                _BAD_CONTENT[key] = digest
                return cached[1]
            raise PolicyError(f"invalid agent policy {source}: {exc}") from exc

        _POLICY_CACHE[key] = (digest, policy)
        _BAD_CONTENT.pop(key, None)
        log.info("loaded agent policy version %d from %s", POLICY_VERSION, source)
        return policy


def render_assistant_prompt(policy: dict, section: str) -> str:
    """Render a system prompt from one editable assistant policy section."""
    configured = policy[section]
    return (
        f"{configured['identity']}\n"
        f"{configured['purpose']}\n\n"
        "\n".join(policy["prompt_wrapper"]) + "\n"
        f"CONFIGURED POLICY\n{json.dumps(configured, ensure_ascii=False, indent=2)}"
    )


def reply_check(policy: dict, name: str, reply: str, step: str | None = None) -> str | None:
    """Return the configured rejection reason when a response violates a rule."""
    checks = policy["registration"]["reply_checks"]
    rule = checks.get(name)
    if rule is None:
        return None
    if name == "premature_registration_fields":
        rule = rule.get(step or "")
    elif rule.get("steps") is not None and step not in rule["steps"]:
        return None
    if not rule:
        return None
    if re.search(rule["pattern"], reply or "", re.IGNORECASE):
        return rule["problem"]
    return None


def matches_any(patterns: list[str], text: str) -> bool:
    """Apply validated policy patterns to a request or proposed response."""
    return any(re.search(pattern, text or "", re.IGNORECASE) for pattern in patterns)


def bare_request_is_out_of_scope(policy: dict, text: str, *, has_context: bool = False) -> bool:
    """Fail closed, except for configured follow-ups anchored in a recent allowed turn."""
    rules = policy["bare_assistant"]
    if matches_any(rules["out_of_scope_patterns"], text):
        return True
    if matches_any(rules["in_scope_patterns"], text):
        return False
    follow_ups = rules.get("follow_up_patterns", [])
    return not (has_context and matches_any(follow_ups, text))


def with_tool_descriptions(tools: list[dict], policy: dict, group: str) -> list[dict]:
    """Apply editable descriptions while keeping function names and schemas in code."""
    configured = policy["tool_descriptions"][group]
    result = copy.deepcopy(tools)
    for tool in result:
        function = tool["function"]
        name = function["name"]
        spec = configured.get(name)
        if spec is None:
            raise PolicyError(f"no configured tool description for {group}.{name}")
        function["description"] = spec["description"]
        descriptions = spec["parameters"]

        def apply(node):
            if not isinstance(node, dict):
                return
            for property_name, value in node.get("properties", {}).items():
                if property_name in descriptions:
                    value["description"] = descriptions[property_name]
                apply(value)
                if isinstance(value, dict) and isinstance(value.get("items"), dict):
                    apply(value["items"])

        apply(function.get("parameters", {}))
    return result
