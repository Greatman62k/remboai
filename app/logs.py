"""Logging that cannot leak secrets. Tokens turn up in places nobody wrote on purpose: httpx logs every request URL (and a Telegram
bot token is part of its URL), uvicorn logs the M-Pesa callback path (which contains the callback secret), exceptions quote URLs.
Every handler gets a formatter that redacts them from the final text, tracebacks included."""
from __future__ import annotations

import logging
import re

_PATTERNS = [
    (re.compile(r"/bot\d+:[A-Za-z0-9_-]{20,}"), "/bot<redacted>"),                       # Telegram bot token in a URL
    (re.compile(r"\b\d{6,}:[A-Za-z0-9_-]{30,}\b"), "<telegram-token>"),                    # ... or on its own
    (re.compile(r"(?i)\b(bearer|basic)\s+[A-Za-z0-9._~+/=-]{8,}"), r"\1 <redacted>"),      # Authorization headers
    (re.compile(r"\bEAA[A-Za-z0-9]{20,}"), "<meta-token>"),                                # Meta access tokens
    (re.compile(r"\bsk-[A-Za-z0-9_-]{16,}"), "<api-key>"),
    (re.compile(r"(?i)(access_token|api[_-]?key|client_secret|app_secret|hub\.verify_token|password|passkey)=[^&\s\"']+"), r"\1=<redacted>"),
    (re.compile(r"/mpesa/callback/[^\s\"'?]+"), "/mpesa/callback/<redacted>"),             # the callback secret is the path
    (re.compile(r"/owner/login/[^\s\"'?]+"), "/owner/login/<redacted>"),                   # single-use sign-in token
]


def redact(text: str) -> str:
    for pattern, repl in _PATTERNS:
        text = pattern.sub(repl, text)
    return text


class RedactingFormatter(logging.Formatter):
    def format(self, record: logging.LogRecord) -> str:
        return redact(super().format(record))


def setup(level: int = logging.INFO) -> None:
    fmt = "%(asctime)s %(levelname)s %(name)s %(message)s"
    logging.basicConfig(level=level, format=fmt)
    for name in ("", "uvicorn", "uvicorn.access", "uvicorn.error"):
        for handler in logging.getLogger(name).handlers:
            handler.setFormatter(RedactingFormatter(fmt))
    for noisy in ("httpx", "httpcore"):        # they print every URL they request
        logging.getLogger(noisy).setLevel(logging.WARNING)

