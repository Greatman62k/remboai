import re
from functools import lru_cache

from pydantic import field_validator
from pydantic_settings import BaseSettings, SettingsConfigDict

_INLINE_COMMENT = re.compile(r"\s+#.*$")


class Settings(BaseSettings):
    model_config = SettingsConfigDict(env_file=".env", extra="ignore")

    @field_validator("*", mode="before")
    @classmethod
    def _no_inline_comments(cls, v):
        """`KEY=value  # note` is a comment for python-dotenv but part of the value for Docker's env_file. Treat both the same.
        A value that is only a comment (`KEY=  # note`) becomes empty."""
        if isinstance(v, str):
            v = v.strip()
            if v.startswith("#"):
                return ""
            return _INLINE_COMMENT.sub("", v)
        return v

    database_url: str
    redis_url: str = "redis://localhost:6379/0"
    wa_app_secret: str = ""
    wa_verify_token: str = ""
    wa_access_token: str = ""
    graph_version: str = "v25.0"

    # Language model: any OpenAI-compatible endpoint. Primary first, then the optional failover.
    llm_base_url: str = "https://api.meta.ai/v1"
    llm_api_key: str = ""
    llm_model: str = "muse-spark-1.3"
    llm_json_mode: str = "none"          # none | json_object | json_schema
    llm_reasoning_effort: str = "minimal"  # "" to omit
    llm_system_role: str = "developer"
    llm_timeout_s: float = 8.0
    # Daily ceilings on metered calls (0 = no limit). Past a ceiling the feature degrades instead of spending more.
    llm_daily_limit: int = 3000
    vision_daily_limit: int = 300
    voice_daily_limit: int = 500
    per_sender_daily_limit: int = 300     # messages one person can send in a day
    strike_limit: int = 3                 # abuse signals in 24 hours before a sender is ignored (0 = never)
    max_active_bookings: int = 3          # upcoming bookings and holds one customer can have at once
    llm_fallback_base_url: str = ""
    llm_fallback_api_key: str = ""
    llm_fallback_model: str = ""

    hold_minutes: int = 10
    asr_url: str = ""
    asr_token: str = ""
    asr_timeout_s: float = 45.0
    vision_enabled: bool = True
    rate_limit_per_min: int = 20
    rate_burst: int = 10
    rate_window_s: int = 60
    rate_daily: int = 300
    llm_daily_per_salon: int = 5000
    ingress_rate_burst: int = 120
    ingress_rate_global: int = 600
    ingress_rate_window_s: int = 60

    # Telegram: a second channel for the same receptionist (no business verification needed). Empty token = off.
    telegram_bot_token: str = ""        # from @BotFather
    telegram_mode: str = "auto"         # auto = webhook when PUBLIC_BASE_URL is https, otherwise long polling | poll | webhook
    telegram_webhook_secret: str = ""    # random string Telegram echoes back in a header

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
    handoff_hours: float = 4.0
    confirm_exact_booking: bool = True
    unpaid_hold_limit: int = 3
    webhook_workers: int = 4
    db_pool_max: int = 10
    salon_setup_invite_hours: int = 168
    trusted_proxy_cidrs: str = ""
    metrics_enabled: bool = True
    reminder_enabled: bool = True
    reminder_template_24h: str = "reminder_24h"
    reminder_template_2h: str = "reminder_2h"


@lru_cache
def get_settings() -> Settings:
    return Settings()  # type: ignore[call-arg]
