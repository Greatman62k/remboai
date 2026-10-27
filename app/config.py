from functools import lru_cache

from pydantic_settings import BaseSettings, SettingsConfigDict


class Settings(BaseSettings):
    model_config = SettingsConfigDict(env_file=".env", extra="ignore")

    database_url: str
    wa_app_secret: str
    wa_verify_token: str
    wa_access_token: str = ""
    graph_version: str = "v25.0"


@lru_cache
def get_settings() -> Settings:
    return Settings()  # type: ignore[call-arg]
