from __future__ import annotations

import hmac
import ipaddress
import logging
import re
import time

log = logging.getLogger("rembo.hardening")


def safe_equals(a: str | None, b: str | None) -> bool:
    if a is None or b is None:
        return False
    return hmac.compare_digest(a.encode("utf-8", "replace"), b.encode("utf-8", "replace"))


_INGRESS_INCR = """
local source = redis.call('INCR', KEYS[1])
if source == 1 then redis.call('EXPIRE', KEYS[1], ARGV[1]) end
if source > tonumber(ARGV[2]) then
  return {source, tonumber(redis.call('GET', KEYS[2]) or '0')}
end
local total = redis.call('INCR', KEYS[2])
if total == 1 then redis.call('EXPIRE', KEYS[2], ARGV[1]) end
return {source, total}
"""

_INGRESS_LOCAL: dict[str, tuple[float, int]] = {}
_INGRESS_LOCAL_MAX_KEYS = 4096


def _positive_int(value, default: int) -> int:
    try:
        return max(1, int(value))
    except (TypeError, ValueError):
        return default


def _local_ingress_peek(key: str, window_s: int) -> int:
    started, count = _INGRESS_LOCAL.get(key, (0.0, 0))
    return count if time.monotonic() - started < window_s else 0


def _trusted_proxy_match(client_ip: str, cidrs: str) -> bool:
    if not cidrs:
        return False
    try:
        ip = ipaddress.ip_address(client_ip)
    except ValueError:
        return False
    for cidr in cidrs.split(","):
        c = cidr.strip()
        if not c:
            continue
        try:
            if ip in ipaddress.ip_network(c, strict=False):
                return True
        except ValueError:
            continue
    return False


def _client_ip(scope, trusted_cidrs: str) -> str:
    direct = str((scope.get("client") or ("unknown", 0))[0])[:128]
    if not trusted_cidrs:
        return direct
    headers = dict((k.decode("latin-1").lower(), v.decode("latin-1")) for k, v in scope.get("headers", []))
    fwd = headers.get("x-forwarded-for", "")
    if fwd and _trusted_proxy_match(direct, trusted_cidrs):
        first = fwd.split(",")[0].strip()
        if first.startswith("["):
            first = first[1:].split("]")[0]
        elif first.count(":") == 1:
            first = first.split(":")[0]
        if first:
            return first[:128]
    return direct


def _local_ingress_count(key: str, window_s: int) -> int:
    now = time.monotonic()
    if key not in _INGRESS_LOCAL and len(_INGRESS_LOCAL) >= _INGRESS_LOCAL_MAX_KEYS:
        for old_key, (started, _) in list(_INGRESS_LOCAL.items()):
            if now - started >= window_s:
                _INGRESS_LOCAL.pop(old_key, None)
        if len(_INGRESS_LOCAL) >= _INGRESS_LOCAL_MAX_KEYS:
            key = "rl:ingress:local-overflow"
    started, count = _INGRESS_LOCAL.get(key, (now, 0))
    if now - started >= window_s:
        started, count = now, 0
    count += 1
    _INGRESS_LOCAL[key] = (started, count)
    return count

class IngressRateLimit:
    def __init__(self, app):
        self.app = app

    async def __call__(self, scope, receive, send):
        if scope["type"] != "http":
            await self.app(scope, receive, send)
            return
        app = scope.get("app")
        ctx = getattr(getattr(app, "state", None), "ctx", None)
        settings = getattr(ctx, "settings", None)
        if settings is None:
            await self.app(scope, receive, send)
            return
        window_s = _positive_int(getattr(settings, "ingress_rate_window_s", 60), 60)
        source_limit = _positive_int(getattr(settings, "ingress_rate_burst", 120), 120)
        global_limit = _positive_int(getattr(settings, "ingress_rate_global", 600), 600)
        trusted = getattr(settings, "trusted_proxy_cidrs", "") or ""
        source = _client_ip(scope, trusted)
        source_key = f"rl:ingress:source:{source}"
        global_key = "rl:ingress:global"
        try:
            redis = getattr(ctx, "redis", None)
            if redis is None:
                raise RuntimeError("Redis unavailable")
            counts = await redis.eval(_INGRESS_INCR, 2, source_key, global_key, window_s, source_limit)
            source_count, global_count = (int(c) for c in counts)
        except Exception:
            if str(getattr(settings, "mpesa_env", "sandbox")).lower() == "production":
                await self._reject(send, 503, b"ingress protection unavailable", window_s)
                return
            source_count = _local_ingress_count(source_key, window_s)
            global_count = _local_ingress_count(global_key, window_s) if source_count <= source_limit else _local_ingress_peek(global_key, window_s)
        if source_count > source_limit or global_count > global_limit:
            await self._reject(send, 429, b"too many requests", window_s)
            return
        await self.app(scope, receive, send)

    @staticmethod
    async def _reject(send, status: int, body: bytes, retry_after: int) -> None:
        await send({"type": "http.response.start", "status": status, "headers": [(b"content-type", b"text/plain; charset=utf-8"), (b"content-length", str(len(body)).encode()), (b"retry-after", str(retry_after).encode()), (b"connection", b"close")]})
        await send({"type": "http.response.body", "body": body})


class BodyLimit:
    def __init__(self, app, max_bytes: int = 256 * 1024):
        self.app = app
        self.max_bytes = max_bytes

    async def __call__(self, scope, receive, send):
        if scope["type"] != "http":
            await self.app(scope, receive, send)
            return
        length = next((v for k, v in scope.get("headers", []) if k.lower() == b"content-length"), None)
        if length is not None:
            try:
                if int(length) > self.max_bytes:
                    await self._reject(send)
                    return
            except ValueError:
                pass
        seen = 0
        started = False
        rejected = False
        async def counted_receive():
            nonlocal seen, rejected
            msg = await receive()
            if msg["type"] == "http.request":
                seen += len(msg.get("body", b""))
                if seen > self.max_bytes:
                    if not started and not rejected:
                        rejected = True
                        await self._reject(send)
                    return {"type": "http.disconnect"}
            return msg
        async def tracked_send(msg):
            nonlocal started
            if rejected:
                return
            if msg["type"] == "http.response.start":
                started = True
            await send(msg)
        try:
            await self.app(scope, counted_receive, tracked_send)
        except Exception:
            if not rejected:
                raise

    @staticmethod
    async def _reject(send) -> None:
        await send({"type": "http.response.start", "status": 413, "headers": [(b"content-type", b"text/plain"), (b"connection", b"close")]})
        await send({"type": "http.response.body", "body": b"payload too large"})

_PHONE = re.compile(r"(?<![\d])(\+?)(\d{10,13})(?!\d)")
_SENSITIVE_QUERY = re.compile(r"([?&](?:access_token|token|api[_-]?key|client_secret|password|passkey|secret|key)=)([^&\s\"']+)", re.I)
_SENSITIVE_ASSIGNMENT = re.compile(r"((?<![A-Za-z0-9])(?:access_token|token|api[_-]?key|client_secret|password|passkey|secret|app_secret|verify_token|webhook_secret)(?![A-Za-z0-9])[\"']?\s*[:=]\s*[\"']?)([^,;\s&}\"']+)", re.I)
_BEARER_TOKEN = re.compile(r"\b(Bearer\s+)[A-Za-z0-9._~+/=-]{8,}", re.I)


def mask_phones(text: str) -> str:
    return _PHONE.sub(lambda m: m.group(1) + m.group(2)[:4] + "*" * (len(m.group(2)) - 7) + m.group(2)[-3:], text)


def redact_log_secrets(text: str) -> str:
    text = _SENSITIVE_QUERY.sub(r"\1[REDACTED]", text)
    text = _SENSITIVE_ASSIGNMENT.sub(r"\1[REDACTED]", text)
    return _BEARER_TOKEN.sub(r"\1[REDACTED]", text)


class MaskFilter(logging.Filter):
    def filter(self, record: logging.LogRecord) -> bool:
        try:
            record.msg = mask_phones(redact_log_secrets(record.getMessage()))
            record.args = ()
        except Exception:
            return True
        return True


def install_log_masking() -> None:
    root = logging.getLogger()
    for h in root.handlers:
        if not any(isinstance(f, MaskFilter) for f in h.filters):
            h.addFilter(MaskFilter())
    for noisy in ("httpx", "httpcore"):
        logging.getLogger(noisy).setLevel(logging.WARNING)


_WEAK = {"", "change-me", "changeme", "secret", "test", "password", "default"}


def ops_api_key_ok(settings) -> bool:
    key = (getattr(settings, "ops_api_key", "") or "").strip()
    return not key or (len(key) >= 64 and len(set(key)) >= 8 and key.lower() not in _WEAK)


def callback_secret_ok(settings) -> bool:
    sec = (getattr(settings, "mpesa_callback_secret", "") or "")
    return len(sec) >= 16 and len(set(sec)) >= 8 and sec.lower() not in _WEAK


def startup_checks(settings) -> list[str]:
    problems: list[str] = []
    mpesa_env = str(getattr(settings, "mpesa_env", "sandbox") or "sandbox").strip().lower()
    if mpesa_env not in {"sandbox", "production"}:
        problems.append("MPESA_ENV must be 'sandbox' or 'production'")
    if not callback_secret_ok(settings):
        problems.append("MPESA_CALLBACK_SECRET is missing, default or shorter than 16 chars: /mpesa/callback is DISABLED. Generate one with: openssl rand -hex 16")
    if len(getattr(settings, "wa_app_secret", "") or "") < 16:
        problems.append("WA_APP_SECRET looks wrong (too short): webhook signatures cannot be verified correctly")
    if len(getattr(settings, "wa_verify_token", "") or "") < 8:
        problems.append("WA_VERIFY_TOKEN is shorter than 8 chars")
    if mpesa_env == "production" and not getattr(settings, "mpesa_verify_callbacks", True):
        problems.append("MPESA_VERIFY_CALLBACKS must be true in production")
    if mpesa_env == "production" and not ops_api_key_ok(settings):
        problems.append("OPS_API_KEY must be at least 64 characters with at least 8 distinct characters when configured")
    token_key = getattr(settings, "token_encryption_key", None)
    if token_key or mpesa_env == "production":
        import re as _re
        if not token_key or not _re.fullmatch(r"^[0-9a-fA-F]{64}$", token_key or ""):
            problems.append("TOKEN_ENCRYPTION_KEY must be set to 32 random bytes encoded as 64 hex characters")
    for name in ("ingress_rate_burst", "ingress_rate_global", "ingress_rate_window_s"):
        try:
            valid = int(getattr(settings, name, 1)) >= 1
        except (TypeError, ValueError):
            valid = False
        if not valid:
            problems.append(f"{name.upper()} must be a positive integer")
    for p in problems:
        log.error("SECURITY CONFIG: %s", p)
    if problems and mpesa_env != "sandbox":
        raise RuntimeError("refusing to start with insecure production configuration: " + "; ".join(problems))
    return problems


class SecurityHeaders:
    """Baseline response headers on every route. Handlers that set their own (the owner page) keep them."""
    HEADERS = ((b"x-content-type-options", b"nosniff"), (b"x-frame-options", b"DENY"), (b"referrer-policy", b"no-referrer"))

    def __init__(self, app):
        self.app = app

    async def __call__(self, scope, receive, send):
        if scope["type"] != "http":
            await self.app(scope, receive, send)
            return

        async def headed_send(msg):
            if msg["type"] == "http.response.start":
                seen = {k.lower() for k, _ in msg.get("headers", [])}
                msg["headers"] = list(msg.get("headers", [])) + [(k, v) for k, v in self.HEADERS if k not in seen]
            await send(msg)

        await self.app(scope, receive, headed_send)


def install(app) -> None:
    app.add_middleware(BodyLimit)
    app.add_middleware(IngressRateLimit)
    app.add_middleware(SecurityHeaders)
    install_log_masking()
