"""Pairing challenges and revocable bearer sessions for RemboAI Glance."""
from __future__ import annotations

import hashlib
import hmac
import json
import re
import secrets
from dataclasses import dataclass
from datetime import datetime, timezone
from uuid import UUID


CODE_ALPHABET = "0123456789ABCDEFGHJKMNPQRSTVWXYZ"
CODE_LENGTH = 12
_HEX_32 = re.compile(r"^[0-9a-fA-F]{64}$")
_DEVICE_BAD = re.compile(r"[\x00-\x1f\x7f]")


class GlanceAuthError(RuntimeError):
    def __init__(self, code: str, status_code: int, message: str, retry_after: int | None = None):
        super().__init__(code)
        self.code = code
        self.status_code = status_code
        self.message = message
        self.retry_after = retry_after


@dataclass(frozen=True)
class GlanceSession:
    id: str
    salon_id: str
    owner_identity: str
    device_label: str
    salon_name: str
    expires_at: datetime
    created_at: datetime
    last_used_at: datetime | None
    revoked_at: datetime | None


def enabled(settings) -> bool:
    return bool(getattr(settings, "glance_enabled", False))


def require_enabled(settings) -> None:
    if not enabled(settings):
        raise GlanceAuthError("not_found", 404, "Not found")


def pairing_keys(settings) -> tuple[dict[int, bytes], int]:
    """Load a versioned HMAC keyring. Missing, malformed or weak keys fail closed."""
    raw = (getattr(settings, "glance_pairing_hmac_keyring", "") or "").strip()
    active = int(getattr(settings, "glance_pairing_hmac_active_version", 0) or 0)
    try:
        parsed = json.loads(raw)
    except (TypeError, ValueError):
        parsed = None
    if not isinstance(parsed, dict) or not parsed or active <= 0:
        raise GlanceAuthError("security_configuration", 503, "Glance pairing is unavailable")
    keys: dict[int, bytes] = {}
    for version, value in parsed.items():
        try:
            key_version = int(version)
        except (TypeError, ValueError):
            raise GlanceAuthError("security_configuration", 503, "Glance pairing is unavailable")
        if key_version <= 0 or not isinstance(value, str) or not _HEX_32.fullmatch(value):
            raise GlanceAuthError("security_configuration", 503, "Glance pairing is unavailable")
        secret = bytes.fromhex(value)
        if len(secret) != 32 or len(set(secret)) < 8:
            raise GlanceAuthError("security_configuration", 503, "Glance pairing is unavailable")
        keys[key_version] = secret
    if active not in keys:
        raise GlanceAuthError("security_configuration", 503, "Glance pairing is unavailable")
    return keys, active


def generate_pairing_code() -> str:
    """Generate exactly 60 random bits as twelve Crockford Base32 characters."""
    value = secrets.randbits(60)
    chars = ["0"] * CODE_LENGTH
    for index in range(CODE_LENGTH - 1, -1, -1):
        chars[index] = CODE_ALPHABET[value & 31]
        value >>= 5
    return "".join(chars)


def normalize_pairing_code(code: str) -> str:
    if not isinstance(code, str):
        return ""
    value = code.strip().upper()
    if len(value) != CODE_LENGTH or any(char not in CODE_ALPHABET for char in value):
        return ""
    return value


def pairing_digest(code: str, key: bytes) -> bytes:
    normalized = normalize_pairing_code(code)
    if not normalized:
        raise ValueError("malformed pairing code")
    return hmac.new(key, normalized.encode("ascii"), hashlib.sha256).digest()


def _subject_hash(key: bytes, scope: str, subject: str) -> bytes:
    payload = f"remboai:glance-rate:v1:{scope}:{subject}".encode("utf-8")
    return hmac.new(key, payload, hashlib.sha256).digest()


def _positive(settings, field: str, default: int) -> int:
    try:
        value = int(getattr(settings, field, default))
    except (TypeError, ValueError):
        value = 0
    if value <= 0:
        raise GlanceAuthError("security_configuration", 503, "Glance security configuration is unavailable")
    return value


def validate_security_settings(settings) -> tuple[dict[int, bytes], int, int]:
    keys, active = pairing_keys(settings)
    ttl = _positive(settings, "glance_session_ttl_seconds", 604800)
    if ttl < 60 or ttl > 30 * 24 * 60 * 60:
        raise GlanceAuthError("security_configuration", 503, "Glance security configuration is unavailable")
    read_limit = _positive(settings, "glance_read_limit_per_minute", 120)
    if read_limit < 90:
        raise GlanceAuthError("security_configuration", 503, "Glance security configuration is unavailable")
    _positive(settings, "glance_write_limit_per_minute", 20)
    for name, default in (
        ("glance_exchange_window_seconds", 900),
        ("glance_exchange_source_limit", 12),
        ("glance_exchange_owner_limit", 10),
        ("glance_pairing_generation_window_seconds", 3600),
        ("glance_pairing_generation_limit", 5),
    ):
        _positive(settings, name, default)
    return keys, active, ttl


async def _count(con, scope: str, subject_hash: bytes, window_seconds: int) -> tuple[int, datetime | None]:
    row = await con.fetchrow(
        "SELECT count,window_started_at FROM glance_rate_limits "
        "WHERE scope=$1 AND subject_hash=$2 AND window_started_at > now()-($3::double precision * interval '1 second')",
        scope, subject_hash, window_seconds)
    return (int(row["count"]), row["window_started_at"]) if row else (0, None)


async def _increment(con, scope: str, subject_hash: bytes, window_seconds: int) -> tuple[int, datetime]:
    row = await con.fetchrow(
        "INSERT INTO glance_rate_limits (scope,subject_hash,window_started_at,count) VALUES ($1,$2,now(),1) "
        "ON CONFLICT (scope,subject_hash) DO UPDATE SET "
        "count=CASE WHEN glance_rate_limits.window_started_at <= now()-($3::double precision * interval '1 second') "
        "THEN 1 ELSE glance_rate_limits.count+1 END, "
        "window_started_at=CASE WHEN glance_rate_limits.window_started_at <= now()-($3::double precision * interval '1 second') "
        "THEN now() ELSE glance_rate_limits.window_started_at END "
        "RETURNING count,window_started_at", scope, subject_hash, window_seconds)
    return int(row["count"]), row["window_started_at"]


def _retry_after(window_started_at: datetime | None, window_seconds: int) -> int:
    if window_started_at is None:
        return window_seconds
    if window_started_at.tzinfo is None:
        window_started_at = window_started_at.replace(tzinfo=timezone.utc)
    remaining = window_seconds - (datetime.now(timezone.utc) - window_started_at).total_seconds()
    return max(1, int(remaining + 0.999))


def _device_label(value: str) -> str:
    if not isinstance(value, str):
        raise GlanceAuthError("invalid_request", 422, "Device label is invalid")
    label = " ".join(value.strip().split())
    if not label or len(label) > 40 or _DEVICE_BAD.search(label):
        raise GlanceAuthError("invalid_request", 422, "Device label is invalid")
    return label


async def eligible_owner_salons(pool, owner_identity: str, bot_id: str) -> list[dict]:
    rows = await pool.fetch(
        "SELECT id::text,name FROM salons WHERE id IS NOT NULL AND active AND owner_wa_id=$1 AND tg_bot_id=$2 "
        "ORDER BY created_at,id", owner_identity, bot_id)
    return [dict(row) for row in rows]


async def issue_pairing_code(pool, settings, owner_identity: str, salon_id: str, bot_id: str) -> dict:
    require_enabled(settings)
    keys, active, _ = validate_security_settings(settings)
    if not owner_identity.startswith("tg:"):
        raise GlanceAuthError("owner_channel_required", 403, "Pairing is available through Telegram")
    label = _device_label("RemboAI Glance")
    owner_key = _subject_hash(keys[active], "pair_generation_owner", owner_identity)
    window = _positive(settings, "glance_pairing_generation_window_seconds", 3600)
    limit = _positive(settings, "glance_pairing_generation_limit", 5)
    code = generate_pairing_code()
    digest = pairing_digest(code, keys[active])
    async with pool.acquire() as con:
        async with con.transaction():
            salon = await con.fetchrow(
                "SELECT id::text,name FROM salons WHERE id=$1::uuid AND active AND owner_wa_id=$2 AND tg_bot_id=$3 FOR SHARE",
                salon_id, owner_identity, bot_id)
            if salon is None:
                raise GlanceAuthError("owner_not_authorized", 403, "You no longer own this salon")
            count, started = await _increment(con, "pair_generation_owner", owner_key, window)
            if count > limit:
                blocked = True
                expires_at = None
            else:
                blocked = False
                await con.execute(
                    "UPDATE glance_pairing_codes SET revoked_at=now() WHERE salon_id=$1::uuid AND owner_identity=$2 "
                    "AND consumed_at IS NULL AND revoked_at IS NULL AND expires_at>now()", salon_id, owner_identity)
                row = await con.fetchrow(
                    "INSERT INTO glance_pairing_codes (salon_id,owner_identity,code_digest,key_version,expires_at) "
                    "VALUES ($1::uuid,$2,$3,$4,now()+interval '5 minutes') RETURNING expires_at",
                    salon_id, owner_identity, digest, active)
                expires_at = row["expires_at"]
    if blocked:
        raise GlanceAuthError("rate_limited", 429, "Please wait before creating another pairing code",
                              _retry_after(started, window))
    return {"code": code, "expires_at": expires_at, "salon_name": salon["name"], "device_label": label}


async def exchange_pairing_code(pool, settings, code: str, device_label: str, source_ip: str) -> dict:
    require_enabled(settings)
    keys, active, ttl = validate_security_settings(settings)
    normalized = normalize_pairing_code(code)
    if not normalized:
        raise GlanceAuthError("invalid_pairing_code_format", 422, "Pairing code is malformed")
    device_label = _device_label(device_label)
    source_ip = (source_ip or "").strip()
    if not source_ip:
        raise GlanceAuthError("security_configuration", 503, "Pairing exchange is unavailable")
    source_key = _subject_hash(keys[active], "exchange_source", source_ip)
    window = _positive(settings, "glance_exchange_window_seconds", 900)
    source_limit = _positive(settings, "glance_exchange_source_limit", 12)
    owner_limit = _positive(settings, "glance_exchange_owner_limit", 10)
    # 256-bit token; the only returned copy is held for the response and never stored.
    token = secrets.token_urlsafe(32)
    token_hash = hashlib.sha256(token.encode("ascii")).digest()
    result = None
    limited_after_commit: tuple[datetime | None, int] | None = None
    async with pool.acquire() as con:
        async with con.transaction():
            candidate = None
            candidate_digest = None
            # Walk the complete configured keyring to preserve key-version support.
            for version, key in sorted(keys.items()):
                digest = hmac.new(key, normalized.encode("ascii"), hashlib.sha256).digest()
                row = await con.fetchrow(
                    "SELECT id::text,salon_id::text,owner_identity,key_version,code_digest,expires_at,consumed_at,revoked_at "
                    "FROM glance_pairing_codes WHERE key_version=$1 AND code_digest=$2 FOR UPDATE", version, digest)
                if row is not None and hmac.compare_digest(bytes(row["code_digest"]), digest):
                    candidate, candidate_digest = dict(row), digest
            owner_key = (_subject_hash(keys[active], "exchange_owner", candidate["owner_identity"])
                         if candidate is not None else None)
            source_count, source_started = await _count(con, "exchange_source", source_key, window)
            if source_count >= source_limit:
                limited_after_commit = (source_started, window)
            owner_count, owner_started = (await _count(con, "exchange_owner", owner_key, window)
                                          if owner_key is not None else (0, None))
            if owner_key is not None and owner_count >= owner_limit:
                limited_after_commit = (owner_started, window)

            now = await con.fetchval("SELECT now()")
            valid = bool(candidate is not None and candidate_digest is not None
                         and candidate["consumed_at"] is None and candidate["revoked_at"] is None
                         and candidate["expires_at"] > now)
            if valid:
                owner = await con.fetchrow(
                    "SELECT s.name FROM salons s WHERE s.id=$1::uuid AND s.active AND s.owner_wa_id=$2",
                    candidate["salon_id"], candidate["owner_identity"])
                valid = owner is not None
            if limited_after_commit is None and valid:
                consumed = await con.fetchval(
                    "UPDATE glance_pairing_codes SET consumed_at=now() WHERE id=$1::uuid AND consumed_at IS NULL "
                    "AND revoked_at IS NULL AND expires_at>now() RETURNING id", candidate["id"])
                if consumed is not None:
                    session = await con.fetchrow(
                        "INSERT INTO glance_sessions (salon_id,owner_identity,token_hash,device_label,expires_at) "
                        "VALUES ($1::uuid,$2,$3,$4,now()+($5::double precision * interval '1 second')) "
                        "RETURNING id::text,created_at,expires_at",
                        candidate["salon_id"], candidate["owner_identity"], token_hash, device_label, ttl)
                    result = {"access_token": token, "expires_at": session["expires_at"],
                              "salon": {"name": owner["name"]}, "session_id": session["id"]}
            if result is None and limited_after_commit is None:
                _, source_started = await _increment(con, "exchange_source", source_key, window)
                source_count, source_started = await _count(con, "exchange_source", source_key, window)
                if owner_key is not None:
                    _, owner_started = await _increment(con, "exchange_owner", owner_key, window)
                    owner_count, owner_started = await _count(con, "exchange_owner", owner_key, window)
                if source_count > source_limit:
                    limited_after_commit = (source_started, window)
                if owner_key is not None and owner_count > owner_limit:
                    limited_after_commit = (owner_started, window)
    if limited_after_commit is not None:
        started, limit_window = limited_after_commit
        raise GlanceAuthError("rate_limited", 429, "Too many pairing attempts", _retry_after(started, limit_window))
    if result is None:
        raise GlanceAuthError("invalid_pairing_code", 401, "Pairing code is invalid or expired")
    return result


async def authenticate_session(pool, settings, token: str, *, allow_revoked: bool = False) -> GlanceSession:
    require_enabled(settings)
    validate_security_settings(settings)
    if not isinstance(token, str) or not token or len(token) > 256:
        raise GlanceAuthError("invalid_session", 401, "Session is invalid")
    digest = hashlib.sha256(token.encode("utf-8")).digest()
    async with pool.acquire() as con:
        row = await con.fetchrow(
            "SELECT gs.id::text,gs.salon_id::text,gs.owner_identity,gs.device_label,gs.created_at,gs.expires_at, "
            "gs.last_used_at,gs.revoked_at,s.name AS salon_name,s.active,s.owner_wa_id AS current_owner "
            "FROM glance_sessions gs LEFT JOIN salons s ON s.id=gs.salon_id WHERE gs.token_hash=$1", digest)
        if row is None:
            raise GlanceAuthError("invalid_session", 401, "Session is invalid")
        if row["expires_at"] <= await con.fetchval("SELECT now()"):
            raise GlanceAuthError("session_expired", 401, "Session has expired")
        if row["revoked_at"] is not None and not allow_revoked:
            raise GlanceAuthError("session_revoked", 403, "Session has been revoked")
        if (not row["active"] or not row["current_owner"] or
                row["owner_identity"].strip().lstrip("+") != row["current_owner"].strip().lstrip("+")):
            await con.execute("UPDATE glance_sessions SET revoked_at=COALESCE(revoked_at,now()) WHERE id=$1::uuid", row["id"])
            raise GlanceAuthError("owner_not_authorized", 403, "Owner no longer controls this salon")
        if row["revoked_at"] is None:
            await con.execute("UPDATE glance_sessions SET last_used_at=now() WHERE id=$1::uuid", row["id"])
    return GlanceSession(
        id=row["id"], salon_id=row["salon_id"], owner_identity=row["owner_identity"],
        device_label=row["device_label"], salon_name=row["salon_name"], expires_at=row["expires_at"],
        created_at=row["created_at"], last_used_at=row["last_used_at"], revoked_at=row["revoked_at"])


async def revoke_current_session(pool, session_id: str) -> None:
    await pool.execute(
        "UPDATE glance_sessions SET revoked_at=COALESCE(revoked_at,now()) WHERE id=$1::uuid", session_id)


async def check_read_quota(pool, settings, session: GlanceSession) -> None:
    keys, active, _ = validate_security_settings(settings)
    limit = _positive(settings, "glance_read_limit_per_minute", 120)
    subject = _subject_hash(keys[active], "read_session", session.id)
    async with pool.acquire() as con:
        async with con.transaction():
            count, started = await _increment(con, "read_session", subject, 60)
    if count > limit:
        raise GlanceAuthError("rate_limited", 429, "Read request limit reached", _retry_after(started, 60))


async def check_write_quota(pool, settings, session: GlanceSession) -> None:
    """Keep model-backed draft/revision calls on a tighter per-session quota."""
    keys, active, _ = validate_security_settings(settings)
    limit = _positive(settings, "glance_write_limit_per_minute", 20)
    subject = _subject_hash(keys[active], "write_session", session.id)
    async with pool.acquire() as con:
        async with con.transaction():
            count, started = await _increment(con, "write_session", subject, 60)
    if count > limit:
        raise GlanceAuthError("rate_limited", 429, "Draft request limit reached", _retry_after(started, 60))


async def list_owner_sessions(pool, owner_identity: str, bot_id: str) -> list[dict]:
    return [dict(row) for row in await pool.fetch(
        "SELECT gs.id::text AS session_id,gs.device_label,gs.created_at,gs.last_used_at,gs.expires_at, "
        "s.id::text AS salon_id,s.name AS salon_name FROM glance_sessions gs "
        "JOIN salons s ON s.id=gs.salon_id WHERE gs.owner_identity=$1 AND s.owner_wa_id=$1 AND s.active "
        "AND s.tg_bot_id=$2 AND gs.revoked_at IS NULL AND gs.expires_at>now() "
        "ORDER BY gs.created_at DESC,gs.id", owner_identity, bot_id)]


async def revoke_owner_session(pool, owner_identity: str, bot_id: str, session_id: str) -> dict | None:
    try:
        canonical_id = str(UUID(session_id))
    except (TypeError, ValueError, AttributeError):
        return None
    row = await pool.fetchrow(
        "UPDATE glance_sessions gs SET revoked_at=COALESCE(gs.revoked_at,now()) FROM salons s "
        "WHERE gs.id=$1::uuid AND gs.salon_id=s.id AND gs.owner_identity=$2 AND s.owner_wa_id=$2 "
        "AND s.tg_bot_id=$3 AND s.active "
        "RETURNING gs.id::text AS session_id,gs.device_label,s.name AS salon_name",
        canonical_id, owner_identity, bot_id)
    return dict(row) if row else None
