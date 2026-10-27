"""Runtime configuration (pydantic-settings).

Secrets are ``SecretStr`` so they never leak through ``repr`` or logs. The ``.env`` file
location is taken from the ``ENV_FILE`` environment variable (default ``.env``), which lets
tests and local runs load credentials from outside the repository.
"""

from __future__ import annotations

import os
from functools import lru_cache
from pathlib import Path
from typing import Any, Literal

from pydantic import Field, SecretStr, field_validator, model_validator
from pydantic_settings import BaseSettings, SettingsConfigDict

from src.exceptions import ConfigurationError

_ROOT = Path(__file__).resolve().parent.parent


def _read_version() -> str:
    version_file = _ROOT / "VERSION"
    try:
        return version_file.read_text(encoding="utf-8").strip() or "0.0.0-dev"
    except OSError:
        return "0.0.0-dev"


class Settings(BaseSettings):
    """Validated application settings."""

    model_config = SettingsConfigDict(extra="ignore", case_sensitive=False)

    # Runtime / identity
    app_env: str = "local"
    log_level: str = "INFO"
    app_version: str = Field(default_factory=_read_version)
    git_sha: str = "unknown"
    deploy_color: str | None = None

    # LLM provider
    llm_provider: Literal["azure", "bedrock", "mock"] = "azure"
    azure_openai_endpoint: str | None = None
    azure_openai_api_key: SecretStr | None = None
    chat_model: str = "gpt-4o-mini"
    judge_model: str = "gpt-5.6-sol"
    bedrock_model_id: str = "anthropic.claude-3-haiku-20240307-v1:0"
    aws_default_region: str = "us-east-1"

    # Tools
    tavily_api_key: SecretStr | None = None
    kb_id: str | None = None

    # Agent behaviour
    max_iterations: int = Field(default=5, ge=1, le=20)
    deep_research_max_iterations: int = Field(default=8, ge=1, le=30)
    llm_timeout_seconds: float = Field(default=45.0, gt=0)  # per HTTP attempt
    llm_total_timeout_seconds: float = Field(default=120.0, gt=0)  # whole call, retries included
    tool_timeout_seconds: float = Field(default=15.0, gt=0)
    max_output_tokens: int = Field(default=1024, ge=16, le=16000)
    reasoning_headroom_tokens: int = Field(default=4000, ge=0, le=64000)

    # Optional observability
    langfuse_public_key: SecretStr | None = None
    langfuse_secret_key: SecretStr | None = None
    langfuse_host: str = "https://cloud.langfuse.com"
    sentry_dsn: SecretStr | None = None
    quality_report_file: str | None = None

    @model_validator(mode="before")
    @classmethod
    def _drop_blank_values(cls, data: Any) -> Any:
        """Treat ``KEY=`` lines of an env file as unset so the field default applies."""
        if isinstance(data, dict):
            return {k: v for k, v in data.items() if not (isinstance(v, str) and not v.strip())}
        return data

    @field_validator("azure_openai_endpoint")
    @classmethod
    def _validate_endpoint(cls, value: str | None) -> str | None:
        if value is None:
            return None
        if not value.startswith(("https://", "http://")):
            raise ValueError("AZURE_OPENAI_ENDPOINT must start with https:// (or http://)")
        return value.rstrip("/")

    @field_validator("log_level")
    @classmethod
    def _validate_log_level(cls, value: str) -> str:
        level = value.upper()
        if level not in {"DEBUG", "INFO", "WARNING", "ERROR", "CRITICAL"}:
            raise ValueError(f"invalid LOG_LEVEL: {value}")
        return level

    @model_validator(mode="after")
    def _require_credentials(self) -> Settings:
        if self.llm_provider == "azure" and not (
            self.azure_openai_endpoint and self.azure_openai_api_key
        ):
            raise ValueError(
                "LLM_PROVIDER=azure requires AZURE_OPENAI_ENDPOINT and AZURE_OPENAI_API_KEY"
            )
        return self

    @property
    def langfuse_enabled(self) -> bool:
        """LangFuse is active only when both keys are present."""
        return bool(self.langfuse_public_key and self.langfuse_secret_key)

    @property
    def sentry_enabled(self) -> bool:
        """Sentry is active only when a DSN is present."""
        return self.sentry_dsn is not None


def load_settings(env_file: str | os.PathLike[str] | None = None) -> Settings:
    """Build settings from the environment plus an optional env file.

    Args:
        env_file: Explicit env file. Defaults to ``$ENV_FILE`` or ``.env``.

    Raises:
        ConfigurationError: When the configuration is invalid.
    """
    path = env_file or os.environ.get("ENV_FILE") or ".env"
    try:
        # _env_file is an init-time option of pydantic-settings, not a field.
        return Settings(_env_file=str(path))
    except ValueError as exc:  # pydantic.ValidationError subclasses ValueError
        raise ConfigurationError(f"invalid configuration: {_safe_message(exc)}") from exc


def _safe_message(exc: Exception) -> str:
    """Summarise a validation error without echoing the offending input values."""
    errors = getattr(exc, "errors", None)
    if callable(errors):
        parts = []
        for item in errors(include_input=False, include_url=False):
            loc = ".".join(str(p) for p in item.get("loc", ())) or "settings"
            parts.append(f"{loc}: {item.get('msg')}")
        return "; ".join(parts)
    return str(exc)


@lru_cache(maxsize=1)
def get_settings() -> Settings:
    """Return the process-wide settings (cached)."""
    return load_settings()
