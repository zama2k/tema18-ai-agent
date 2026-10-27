"""Unit tests for settings loading and validation."""

from __future__ import annotations

from pathlib import Path

import pytest

from src.config import Settings, get_settings, load_settings
from src.exceptions import ConfigurationError

ENDPOINT = "https://example.test/openai/v1/"


def build(**kwargs: object) -> Settings:
    return Settings(_env_file=None, **kwargs)  # type: ignore[arg-type]


class TestValidation:
    def test_defaults_for_mock_provider(self) -> None:
        cfg = build(llm_provider="mock")
        assert cfg.chat_model == "gpt-4o-mini"
        assert cfg.judge_model == "gpt-5.6-sol"
        assert cfg.max_iterations == 5
        assert not cfg.langfuse_enabled and not cfg.sentry_enabled

    def test_azure_requires_endpoint_and_key(self) -> None:
        with pytest.raises(ValueError, match="AZURE_OPENAI_ENDPOINT"):
            build(llm_provider="azure")

    def test_endpoint_is_normalised(self) -> None:
        cfg = build(azure_openai_endpoint=ENDPOINT, azure_openai_api_key="k")
        assert cfg.azure_openai_endpoint == "https://example.test/openai/v1"

    def test_endpoint_must_be_a_url(self) -> None:
        with pytest.raises(ValueError, match="https"):
            build(llm_provider="mock", azure_openai_endpoint="example.test")

    def test_invalid_log_level_rejected(self) -> None:
        with pytest.raises(ValueError, match="LOG_LEVEL"):
            build(llm_provider="mock", log_level="LOUD")

    def test_log_level_is_normalised(self) -> None:
        assert build(llm_provider="mock", log_level="debug").log_level == "DEBUG"

    @pytest.mark.parametrize(
        "field", ["max_iterations", "llm_timeout_seconds", "tool_timeout_seconds"]
    )
    def test_numeric_bounds(self, field: str) -> None:
        with pytest.raises(ValueError):
            build(llm_provider="mock", **{field: 0})

    def test_secrets_never_appear_in_repr(self) -> None:
        cfg = build(
            azure_openai_endpoint=ENDPOINT,
            azure_openai_api_key="super-secret-key",
            tavily_api_key="tvly-secret",
        )
        text = repr(cfg) + str(cfg)
        assert "super-secret-key" not in text and "tvly-secret" not in text
        assert cfg.azure_openai_api_key is not None
        assert cfg.azure_openai_api_key.get_secret_value() == "super-secret-key"

    def test_langfuse_needs_both_keys(self) -> None:
        assert not build(llm_provider="mock", langfuse_public_key="pk").langfuse_enabled
        both = build(llm_provider="mock", langfuse_public_key="pk", langfuse_secret_key="sk")
        assert both.langfuse_enabled

    def test_sentry_enabled_with_dsn(self) -> None:
        assert build(
            llm_provider="mock", sentry_dsn="https://k@o0.ingest.sentry.io/1"
        ).sentry_enabled


class TestLoading:
    def test_loads_from_env_file_path(self, tmp_path: Path) -> None:
        env_file = tmp_path / "custom.env"
        env_file.write_text(
            "LLM_PROVIDER=azure\n"
            f"AZURE_OPENAI_ENDPOINT={ENDPOINT}\n"
            "AZURE_OPENAI_API_KEY=abc\n"
            "CHAT_MODEL=my-deployment\n"
            "UNRELATED_VARIABLE=ignored\n",
            encoding="utf-8",
        )
        cfg = load_settings(env_file)
        assert cfg.chat_model == "my-deployment"
        assert cfg.llm_provider == "azure"

    def test_env_file_variable_is_honoured(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        env_file = tmp_path / "x.env"
        env_file.write_text("LLM_PROVIDER=mock\nCHAT_MODEL=from-file\n", encoding="utf-8")
        monkeypatch.setenv("ENV_FILE", str(env_file))
        assert load_settings().chat_model == "from-file"

    def test_blank_values_use_defaults(self, tmp_path: Path) -> None:
        env_file = tmp_path / "blank.env"
        env_file.write_text(
            "LLM_PROVIDER=mock\nTAVILY_API_KEY=\nCHAT_MODEL=\nLOG_LEVEL=\n", encoding="utf-8"
        )
        cfg = load_settings(env_file)
        assert cfg.tavily_api_key is None and cfg.chat_model == "gpt-4o-mini"

    def test_process_environment_overrides_nothing_when_unset(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        monkeypatch.setenv("LLM_PROVIDER", "mock")
        monkeypatch.setenv("CHAT_MODEL", "from-env")
        assert load_settings("missing.env").chat_model == "from-env"

    def test_invalid_configuration_raises_without_leaking_values(self, tmp_path: Path) -> None:
        env_file = tmp_path / "bad.env"
        env_file.write_text(
            "LLM_PROVIDER=azure\nAZURE_OPENAI_API_KEY=leaky-secret-value\n", encoding="utf-8"
        )
        with pytest.raises(ConfigurationError) as info:
            load_settings(env_file)
        assert "leaky-secret-value" not in str(info.value)
        assert "AZURE_OPENAI_ENDPOINT" in str(info.value)

    def test_get_settings_is_cached(self, monkeypatch: pytest.MonkeyPatch) -> None:
        monkeypatch.setenv("LLM_PROVIDER", "mock")
        get_settings.cache_clear()
        assert get_settings() is get_settings()
        get_settings.cache_clear()

    def test_version_comes_from_version_file(self) -> None:
        version = build(llm_provider="mock").app_version
        assert version and version != "unknown"
