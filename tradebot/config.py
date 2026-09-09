from __future__ import annotations

import math
import os
from collections.abc import Mapping
from dataclasses import dataclass
from enum import Enum
from urllib.parse import urlsplit


class ConfigError(ValueError):
    """Raised when managed-service configuration is unsafe or incomplete."""


class ProviderCredentialState(str, Enum):
    CONFIGURED = "configured"
    UNAVAILABLE = "unavailable"


@dataclass(frozen=True)
class ProviderBudget:
    max_concurrency: int
    timeout_seconds: float
    max_retries: int
    rate_budget_per_minute: int


@dataclass(frozen=True)
class ProviderConfig:
    budget: ProviderBudget
    credential_state: ProviderCredentialState = ProviderCredentialState.UNAVAILABLE


@dataclass(frozen=True)
class Settings:
    database_url: str
    scanner_lock_database_url: str
    app_env: str
    research_execution_enabled: bool
    dexscreener: ProviderConfig
    helius: ProviderConfig
    jupiter: ProviderConfig
    rugcheck: ProviderConfig
    git_commit_sha: str
    scanner_lock_key: int = 7_341_628_430_911

    @property
    def providers_configured(self) -> dict[str, str]:
        return {
            "dexscreener": self.dexscreener.credential_state.value,
            "helius": self.helius.credential_state.value,
            "jupiter": self.jupiter.credential_state.value,
            "rugcheck": self.rugcheck.credential_state.value,
        }


def _get_required(env: Mapping[str, str], name: str) -> str:
    value = env.get(name, "").strip()
    if not value:
        raise ConfigError(f"{name} is required")
    return value


def _parse_false_guard(env: Mapping[str, str]) -> bool:
    raw = env.get("RESEARCH_EXECUTION_ENABLED", "false").strip().lower()
    if raw != "false":
        raise ConfigError("RESEARCH_EXECUTION_ENABLED must be false")
    return False


def _reject_private_key(env: Mapping[str, str]) -> None:
    if env.get("SOLANA_PRIVATE_KEY", "").strip():
        raise ConfigError("SOLANA_PRIVATE_KEY is not accepted by the research service")


def _positive_int(env: Mapping[str, str], name: str) -> int:
    raw = _get_required(env, name)
    try:
        value = int(raw)
    except ValueError as exc:
        raise ConfigError(f"{name} must be a positive integer") from exc
    if value <= 0:
        raise ConfigError(f"{name} must be a positive integer")
    return value


def _positive_float(env: Mapping[str, str], name: str) -> float:
    raw = _get_required(env, name)
    try:
        value = float(raw)
    except ValueError as exc:
        raise ConfigError(f"{name} must be a finite positive value") from exc
    if value <= 0 or not math.isfinite(value):
        raise ConfigError(f"{name} must be a finite positive value")
    return value


def _non_negative_int(env: Mapping[str, str], name: str) -> int:
    raw = _get_required(env, name)
    try:
        value = int(raw)
    except ValueError as exc:
        raise ConfigError(f"{name} must be a non-negative integer") from exc
    if value < 0:
        raise ConfigError(f"{name} must be a non-negative integer")
    return value


def _provider_config(env: Mapping[str, str], prefix: str, credential_env: str | None = None) -> ProviderConfig:
    state = (
        ProviderCredentialState.CONFIGURED
        if credential_env and env.get(credential_env, "").strip()
        else ProviderCredentialState.UNAVAILABLE
    )
    return ProviderConfig(
        budget=ProviderBudget(
            max_concurrency=_positive_int(env, f"{prefix}_MAX_CONCURRENCY"),
            timeout_seconds=_positive_float(env, f"{prefix}_TIMEOUT_SECONDS"),
            max_retries=_non_negative_int(env, f"{prefix}_MAX_RETRIES"),
            rate_budget_per_minute=_positive_int(env, f"{prefix}_RATE_BUDGET_PER_MINUTE"),
        ),
        credential_state=state,
    )


def redacted_url_label(url: str) -> str:
    parsed = urlsplit(url)
    host = parsed.hostname or "unknown-host"
    return f"{parsed.scheme or 'unknown'}://{host}/..."


def load_settings(env: Mapping[str, str] | None = None) -> Settings:
    source = env if env is not None else os.environ
    _reject_private_key(source)
    return Settings(
        database_url=_get_required(source, "DATABASE_URL"),
        scanner_lock_database_url=_get_required(source, "SCANNER_LOCK_DATABASE_URL"),
        app_env=_get_required(source, "APP_ENV"),
        research_execution_enabled=_parse_false_guard(source),
        dexscreener=_provider_config(source, "DEXSCREENER"),
        helius=_provider_config(source, "HELIUS", "HELIUS_API_KEY"),
        jupiter=_provider_config(source, "JUPITER"),
        rugcheck=_provider_config(source, "RUGCHECK", "RUGCHECK_API_KEY"),
        git_commit_sha=source.get("GIT_COMMIT_SHA", "unknown").strip() or "unknown",
    )
