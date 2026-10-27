"""Shared pytest configuration."""

from __future__ import annotations

import pytest

_ENV_KEYS = (
    "LLM_PROVIDER",
    "AZURE_OPENAI_ENDPOINT",
    "AZURE_OPENAI_API_KEY",
    "CHAT_MODEL",
    "JUDGE_MODEL",
    "TAVILY_API_KEY",
    "KB_ID",
    "LANGFUSE_PUBLIC_KEY",
    "LANGFUSE_SECRET_KEY",
    "SENTRY_DSN",
    "QUALITY_REPORT_FILE",
    "PRICE_OVERRIDES",
    "GIT_SHA",
    "DEPLOY_COLOR",
    "APP_VERSION",
    "APP_ENV",
)


@pytest.fixture(autouse=True)
def _clean_environment(request: pytest.FixtureRequest, monkeypatch: pytest.MonkeyPatch) -> None:
    """Unit tests never see the developer's real configuration."""
    if request.node.get_closest_marker("integration"):
        return
    for key in _ENV_KEYS:
        monkeypatch.delenv(key, raising=False)
    monkeypatch.setenv("ENV_FILE", "does-not-exist.env")
    # Never let boto3 look for real credentials or call the EC2 metadata endpoint.
    monkeypatch.setenv("AWS_ACCESS_KEY_ID", "test")
    monkeypatch.setenv("AWS_SECRET_ACCESS_KEY", "test")
    monkeypatch.setenv("AWS_EC2_METADATA_DISABLED", "true")
