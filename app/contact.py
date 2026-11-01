"""Human-readable customer contacts for owner-facing replies."""
from __future__ import annotations

from .mpesa import normalize_msisdn


def display_contact(identifier: str | None) -> str:
    """Format a channel identifier without pretending every customer is a phone number."""
    if not identifier:
        return "contact unavailable"
    if identifier.startswith("tg:"):
        return f"Telegram ID {identifier[3:]}"
    phone = normalize_msisdn(identifier)
    return f"+{phone}" if phone else identifier
