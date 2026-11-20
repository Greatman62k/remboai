"""Structured provider submission outcomes for durable outbound work."""
from __future__ import annotations

from dataclasses import dataclass


@dataclass(frozen=True, slots=True)
class SendResult:
    accepted: bool
    provider_message_id: str | None = None
    error_category: str | None = None
    error_code: str | None = None
    retryable: bool = False
    ambiguous: bool = False
