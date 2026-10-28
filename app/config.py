from functools import lru_cache

from pydantic_settings import BaseSettings, SettingsConfigDict


class Settings(BaseSettings):
    model_config = SettingsConfigDict(env_file=".env", extra="ignore")

    database_url: str
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
    llm_fallback_base_url: str = ""
    llm_fallback_api_key: str = ""
    llm_fallback_model: str = ""

    hold_minutes: int = 10
    # Voice notes: Meta's Omnilingual ASR (open source, 1600+ languages incl. Swahili) served from our own Modal GPU app, see deploy/modal_asr.py
    asr_url: str = ""                    # https://<workspace>--rembo-asr-asr-web.modal.run/transcribe ; empty = voice notes not supported
    asr_token: str = ""
    asr_timeout_s: float = 45.0          # covers a cold start of the GPU container
    vision_enabled: bool = True          # photo matching uses the same Meta endpoints as the text model
    rate_limit_per_min: int = 20         # messages per sender per minute; beyond this they are ignored (protects model credits)

    # Telegram: a second channel for the same receptionist (no business verification needed). Empty token = off.
    telegram_bot_token: str = ""        # from @BotFather
    telegram_mode: str = "auto"         # auto = webhook when PUBLIC_BASE_URL is https, otherwise long polling | poll | webhook
    telegram_webhook_secret: str = ""    # random string Telegram echoes back in a header

    # M-Pesa (Daraja). Sandbox moves no real money; production needs Safaricom's Go Live approval for the salon's till.
    mpesa_env: str = "sandbox"           # sandbox | production
    mpesa_consumer_key: str = ""
    mpesa_consumer_secret: str = ""
    mpesa_passkey: str = ""
    mpesa_callback_secret: str = ""      # random string that becomes part of the callback URL path
    public_base_url: str = ""            # https://your-domain, where Safaricom can reach /mpesa/callback/<secret>


@lru_cache
def get_settings() -> Settings:
    return Settings()  # type: ignore[call-arg]
