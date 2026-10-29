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


def _level(name: str) -> int:
    return {"DEBUG": logging.DEBUG, "INFO": logging.INFO, "WARNING": logging.WARNING, "ERROR": logging.ERROR}.get((name or "").upper(), logging.INFO)


class JsonFormatter(logging.Formatter):
    def format(self, record: logging.LogRecord) -> str:
        import json as _json
        payload = {
            "ts": self.formatTime(record, "%Y-%m-%dT%H:%M:%S%z"),
            "level": record.levelname,
            "logger": record.name,
            "msg": redact(record.getMessage()),
        }
        if record.exc_info and record.exc_info[0] is not None:
            payload["exc"] = redact(self.formatException(record.exc_info))
        return _json.dumps(payload, ensure_ascii=False)


def setup(level: int | str = logging.INFO, fmt: str = "text", log_file: str = "") -> None:
    if isinstance(level, str):
        level = _level(level)
    text_fmt = "%(asctime)s %(levelname)s %(name)s %(message)s"
    use_json = (fmt or "text").lower() == "json"
    handlers: list[logging.Handler] = [logging.StreamHandler()]
    if log_file:
        try:
            from logging.handlers import RotatingFileHandler
            fh = RotatingFileHandler(log_file, maxBytes=10 * 1024 * 1024, backupCount=5, encoding="utf-8")
            handlers.append(fh)
        except Exception:
            pass
    formatter: logging.Formatter = JsonFormatter() if use_json else RedactingFormatter(text_fmt)
    for h in handlers:
        h.setFormatter(formatter)
    root = logging.getLogger()
    root.handlers.clear()
    for h in handlers:
        root.addHandler(h)
    root.setLevel(level)
    for name in ("uvicorn", "uvicorn.access", "uvicorn.error"):
        lg = logging.getLogger(name)
        lg.handlers.clear()
        for h in handlers:
            lg.addHandler(h)
        lg.setLevel(level)
        lg.propagate = False
    for noisy in ("httpx", "httpcore"):        # they print every URL they request
        logging.getLogger(noisy).setLevel(logging.WARNING)

