import re
from functools import lru_cache

from pydantic import field_validator
from pydantic_settings import BaseSettings, SettingsConfigDict

_INLINE_COMMENT = re.compile(r"\s+#.*$")


class Settings(BaseSettings):
    model_config = SettingsConfigDict(env_file=".env", extra="ignore")

    @field_validator("*", mode="before")
    @classmethod
    def _no_inline_comments(cls, v, info):
        """`KEY=value  # note` is a comment for python-dotenv but part of the value for Docker's env_file. Treat both the same.
        A value that is only a comment (`KEY=  # note`) falls back to the field default instead of failing validation."""
        if isinstance(v, str):
            v = v.strip()
            if v.startswith("#"):
                v = ""
            else:
                v = _INLINE_COMMENT.sub("", v)
            if v == "" and info.field_name in cls.model_fields:
                default = cls.model_fields[info.field_name].get_default(call_default_factory=True)
                if default is not None:
                    return default
        return v

    database_url: str
    redis_url: str = "redis://localhost:6379/0"
    wa_app_secret: str = ""
    wa_verify_token: str = ""
    wa_access_token: str = ""
    graph_version: str = "v25.0"

    llm_base_url: str = "https://api.meta.ai/v1"
    llm_api_key: str = ""
    llm_model: str = "muse-spark-1.3"
    llm_json_mode: str = "none"
    llm_reasoning_effort: str = "minimal"
    llm_system_role: str = "developer"
    llm_timeout_s: float = 8.0
    llm_daily_limit: int = 3000
    vision_daily_limit: int = 300
    voice_daily_limit: int = 500
    per_sender_daily_limit: int = 300
    strike_limit: int = 3
    platform_owner_id: str = ""
    platform_owner_ids: str = ""
    max_active_bookings: int = 3
    llm_fallback_base_url: str = ""
    llm_fallback_api_key: str = ""
    llm_fallback_model: str = ""
    llm_backend: str = "meta_api"
    llm_fallback_backend: str = ""
    llm_quantization: str = ""
    llm_host: str = ""
    llm_breaker_trips: int = 3
    llm_breaker_cooldown_s: int = 60

    hold_minutes: int = 10
    asr_url: str = ""
    asr_token: str = ""
    asr_proxy_key: str = ""
    asr_proxy_secret: str = ""
    asr_timeout_s: float = 120.0
    vision_enabled: bool = True
    rate_limit_per_min: int = 20
    rate_burst: int = 10
    rate_window_s: int = 60
    rate_daily: int = 300
    llm_daily_per_salon: int = 5000
    bare_llm_daily_per_sender: int = 30
    # Agent mode: the Meta model runs the receptionist through tools; code validates and executes every action.
    agent_mode: str = "off"              # "on" or "off"; off keeps the keyword/extraction flow only
    agent_protocol: str = "auto"         # "auto" tries native tool calling and falls back to JSON steps; or force "tools" / "json"
    agent_max_steps: int = 5
    agent_call_timeout_s: float = 12.0
    agent_turn_timeout_s: float = 30.0
    agent_reasoning_effort: str = "low"
    agent_policy_path: str = ""
    agent_retention_days: int = 14
    ingress_rate_burst: int = 120
    ingress_rate_global: int = 600
    ingress_rate_window_s: int = 60

    telegram_bot_token: str = ""
    telegram_mode: str = "auto"
    telegram_webhook_secret: str = ""

    central_whatsapp_number: str = ""
    mpesa_env: str = "sandbox"
    mpesa_consumer_key: str = ""
    mpesa_consumer_secret: str = ""
    mpesa_passkey: str = ""
    mpesa_callback_secret: str = ""
    mpesa_verify_callbacks: bool = True
    public_base_url: str = ""
    ops_api_key: str = ""
    token_encryption_key: str = ""
    webhook_max_pending: int = 10000
    webhook_max_pending_per_sender: int = 100
    webhook_retention_days: int = 30
    inbound_retention_days: int = 90
    handoff_retention_days: int = 30
    mpesa_callback_retention_days: int = 90
    unmatched_raw_retention_days: int = 90
    handoff_hours: float = 4.0
    webhook_workers: int = 4
    db_pool_max: int = 10
    trusted_proxy_cidrs: str = ""
    metrics_enabled: bool = True
    reminder_enabled: bool = True
    reminder_template_24h: str = "reminder_24h"
    reminder_template_2h: str = "reminder_2h"

    log_level: str = "INFO"
    log_format: str = "text"
    log_file: str = ""


@lru_cache
def get_settings() -> Settings:
    return Settings()  # type: ignore[call-arg]
