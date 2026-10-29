from __future__ import annotations

import base64
import re
import secrets

from cryptography.exceptions import InvalidTag
from cryptography.hazmat.primitives.ciphers.aead import AESGCM

PREFIX = "enc:v1:"
_AAD = b"remboai/provider-token/v1"
_KEY_RE = re.compile(r"^[0-9a-fA-F]{64}$")


def valid_encryption_key(value: str | None) -> bool:
    return bool(value and _KEY_RE.fullmatch(value))


def _cipher(key: str | None) -> AESGCM:
    if not valid_encryption_key(key):
        raise RuntimeError("TOKEN_ENCRYPTION_KEY must be 64 hex characters (32 random bytes)")
    return AESGCM(bytes.fromhex(key))


def encrypt_token(value: str | None, key: str | None) -> str | None:
    if value is None or value == "":
        return value
    cipher = _cipher(key)
    if value.startswith(PREFIX):
        decrypt_token(value, key, require_encryption=True)
        return value
    nonce = secrets.token_bytes(12)
    ciphertext = cipher.encrypt(nonce, value.encode("utf-8"), _AAD)
    encoded = base64.urlsafe_b64encode(nonce + ciphertext).decode("ascii").rstrip("=")
    return PREFIX + encoded


def decrypt_token(value: str | None, key: str | None, *, require_encryption: bool) -> str | None:
    if value is None or value == "":
        return value
    if not value.startswith(PREFIX):
        if require_encryption:
            raise RuntimeError("plaintext provider token found in Postgres; encrypt stored tokens before startup")
        return value
    cipher = _cipher(key)
    encoded = value[len(PREFIX):]
    try:
        blob = base64.urlsafe_b64decode(encoded + "=" * (-len(encoded) % 4))
        if len(blob) < 12 + 16:
            raise ValueError("encrypted token is truncated")
        clear = cipher.decrypt(blob[:12], blob[12:], _AAD)
        return clear.decode("utf-8")
    except (ValueError, InvalidTag, UnicodeDecodeError) as exc:
        raise RuntimeError("stored provider token could not be decrypted; check TOKEN_ENCRYPTION_KEY") from exc


def decrypt_salon_tokens(salon: dict, settings) -> dict:
    require = str(getattr(settings, "mpesa_env", "sandbox")).lower() == "production"
    key = getattr(settings, "token_encryption_key", "") or None
    for field in ("wa_access_token", "ig_access_token"):
        if field in salon:
            salon[field] = decrypt_token(salon[field], key, require_encryption=require or bool(key))
    return salon


async def verify_stored_tokens(pool, settings) -> None:
    key = getattr(settings, "token_encryption_key", "") or None
    require = str(getattr(settings, "mpesa_env", "sandbox")).lower() == "production" or bool(key)
    if not require:
        return
    rows = await pool.fetch("SELECT wa_access_token, ig_access_token FROM salons")
    for row in rows:
        decrypt_token(row["wa_access_token"], key, require_encryption=True)
        decrypt_token(row["ig_access_token"], key, require_encryption=True)
