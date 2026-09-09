import pytest

from tradebot.config import ConfigError, load_settings


def valid_env(**overrides):
    env = {
        "DATABASE_URL": "postgresql://app:pass@example.com/db",
        "SCANNER_LOCK_DATABASE_URL": "postgresql://app:pass@example.com/db",
        "APP_ENV": "test",
        "RESEARCH_EXECUTION_ENABLED": "false",
        "DEXSCREENER_MAX_CONCURRENCY": "2",
        "DEXSCREENER_TIMEOUT_SECONDS": "10",
        "DEXSCREENER_MAX_RETRIES": "2",
        "DEXSCREENER_RATE_BUDGET_PER_MINUTE": "60",
        "HELIUS_MAX_CONCURRENCY": "1",
        "HELIUS_TIMEOUT_SECONDS": "10",
        "HELIUS_MAX_RETRIES": "2",
        "HELIUS_RATE_BUDGET_PER_MINUTE": "30",
        "JUPITER_MAX_CONCURRENCY": "1",
        "JUPITER_TIMEOUT_SECONDS": "10",
        "JUPITER_MAX_RETRIES": "2",
        "JUPITER_RATE_BUDGET_PER_MINUTE": "30",
        "RUGCHECK_MAX_CONCURRENCY": "1",
        "RUGCHECK_TIMEOUT_SECONDS": "10",
        "RUGCHECK_MAX_RETRIES": "2",
        "RUGCHECK_RATE_BUDGET_PER_MINUTE": "20",
    }
    env.update(overrides)
    return env


def test_load_settings_accepts_research_only_values():
    settings = load_settings(valid_env())

    assert settings.research_execution_enabled is False
    assert settings.dexscreener.budget.max_concurrency == 2
    assert settings.helius.budget.timeout_seconds == 10
    assert settings.rugcheck.credential_state == "unavailable"


@pytest.mark.parametrize("value", ["true", "1", "yes", ""])
def test_research_execution_guard_rejects_anything_except_false(value):
    with pytest.raises(ConfigError, match="RESEARCH_EXECUTION_ENABLED must be false"):
        load_settings(valid_env(RESEARCH_EXECUTION_ENABLED=value))


def test_private_key_is_rejected():
    with pytest.raises(ConfigError, match="SOLANA_PRIVATE_KEY"):
        load_settings(valid_env(SOLANA_PRIVATE_KEY="secret"))


@pytest.mark.parametrize("name", ["DEXSCREENER", "HELIUS", "JUPITER", "RUGCHECK"])
def test_provider_budget_requires_positive_concurrency(name):
    with pytest.raises(ConfigError, match=f"{name}_MAX_CONCURRENCY"):
        load_settings(valid_env(**{f"{name}_MAX_CONCURRENCY": "0"}))


@pytest.mark.parametrize("bad_value", ["0", "-1", "inf", "nan"])
def test_provider_budget_requires_finite_positive_timeout(bad_value):
    with pytest.raises(ConfigError, match="DEXSCREENER_TIMEOUT_SECONDS"):
        load_settings(valid_env(DEXSCREENER_TIMEOUT_SECONDS=bad_value))


def test_provider_budget_requires_non_negative_retries():
    with pytest.raises(ConfigError, match="DEXSCREENER_MAX_RETRIES"):
        load_settings(valid_env(DEXSCREENER_MAX_RETRIES="-1"))


def test_provider_budget_requires_positive_rate_budget():
    with pytest.raises(ConfigError, match="DEXSCREENER_RATE_BUDGET_PER_MINUTE"):
        load_settings(valid_env(DEXSCREENER_RATE_BUDGET_PER_MINUTE="0"))
