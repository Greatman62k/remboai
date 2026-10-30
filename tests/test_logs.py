"""Logging redaction: secrets never reach the log text, and a mistyped level says so."""
import logging


def test_an_unknown_log_level_warns_and_uses_info(capsys):
    from app.logs import _level, redact
    assert _level("VERBOSE") == logging.INFO
    assert "VERBOSE" in capsys.readouterr().err
    assert _level("debug") == logging.DEBUG


def test_bare_secrets_and_tokens_are_redacted():
    from app.logs import redact
    assert redact("callback secret=abc123 path") == "callback secret=<redacted> path"
    assert redact("using token=xyz789 here") == "using token=<redacted> here"
    assert redact("webhook_secret=hunter2 hi") == "webhook_secret=<redacted> hi"
    assert redact("nothing sensitive here") == "nothing sensitive here"


def test_compound_secret_names_are_redacted_by_the_log_filter():
    from app.hardening import redact_log_secrets
    assert redact_log_secrets("webhook_secret=abc123") == "webhook_secret=[REDACTED]"
    assert redact_log_secrets("verify_token=abc123") == "verify_token=[REDACTED]"
    assert redact_log_secrets("app_secret=abc123") == "app_secret=[REDACTED]"
    assert redact_log_secrets("passkey=abc123") == "passkey=[REDACTED]"
    assert redact_log_secrets("it's a secret= kept") == "it's a secret= [REDACTED]"
