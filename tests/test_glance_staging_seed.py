from types import SimpleNamespace

import pytest

from scripts.seed_glance_staging import SEED_CONFIRMATION, validate_seed_target


HMAC_KEY = "0123456789abcdef" * 4


def _settings(**overrides):
    values = {
        "glance_enabled": True,
        "glance_pairing_hmac_keyring": '{"1":"' + HMAC_KEY + '"}',
        "glance_pairing_hmac_active_version": 1,
        "glance_session_ttl_seconds": 604800,
        "glance_exchange_window_seconds": 900,
        "glance_exchange_source_limit": 12,
        "glance_exchange_owner_limit": 10,
        "glance_pairing_generation_window_seconds": 3600,
        "glance_pairing_generation_limit": 5,
        "glance_read_limit_per_minute": 120,
        "glance_cors_origins": "https://synthetic-glance.example.test",
        "database_url": "postgresql://rembo_stage:unused@stage-db:5432/remboai_glance_staging",
        "redis_url": "redis://:unused@stage-redis:6379/0",
        "telegram_bot_token": "123456:synthetic-test-token",
    }
    values.update(overrides)
    return SimpleNamespace(**values)


def _environment(**overrides):
    values = {
        "GLANCE_STAGING_CONFIRM": SEED_CONFIRMATION,
        "GLANCE_STAGING_DATABASE_NAME": "remboai_glance_staging",
        "GLANCE_STAGING_DATABASE_HOST": "stage-db",
        "GLANCE_STAGING_REDIS_HOST": "stage-redis",
        "GLANCE_STAGING_OWNER_CHAT_ID": "700000001",
    }
    values.update(overrides)
    return values


def test_seed_target_requires_explicit_confirmation_and_isolated_database_identity():
    with pytest.raises(ValueError, match="explicit isolated staging confirmation"):
        validate_seed_target(_settings(), _environment(GLANCE_STAGING_CONFIRM=""))
    with pytest.raises(ValueError, match="dedicated Glance staging database and user"):
        validate_seed_target(
            _settings(database_url="postgresql://rembo:unused@stage-db:5432/rembo"), _environment())


def test_seed_target_requires_enabled_glance_security_and_a_staging_telegram_owner():
    with pytest.raises(ValueError, match="GLANCE_ENABLED"):
        validate_seed_target(_settings(glance_enabled=False), _environment())
    with pytest.raises(ValueError, match="synthetic Telegram owner chat ID"):
        validate_seed_target(_settings(), _environment(GLANCE_STAGING_OWNER_CHAT_ID="owner"))


def test_seed_target_returns_only_validated_non_secret_identities():
    owner, bot_key, db_host, redis_host = validate_seed_target(_settings(), _environment())
    assert (owner, bot_key, db_host, redis_host) == ("tg:700000001", "tg:123456", "stage-db", "stage-redis")
