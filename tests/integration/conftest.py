"""Fixtures for integration tests: real Azure OpenAI (and Tavily when a key is present).

Credentials come from ``ENV_FILE`` (or ``.env``) / the process environment. When they are
missing the whole module is skipped with an explicit reason instead of failing.
"""

from __future__ import annotations

import time
from dataclasses import dataclass

import pytest

from src.config import Settings, load_settings
from src.exceptions import ConfigurationError
from src.providers import AzureOpenAIProvider
from src.tools import RAGTool


@pytest.fixture(scope="session")
def live_settings() -> Settings:
    """Settings with real Azure credentials, or skip."""
    try:
        cfg = load_settings()
    except ConfigurationError as exc:
        pytest.skip(f"Azure OpenAI credentials not available: {exc}")
    if cfg.llm_provider != "azure":
        pytest.skip("integration tests need LLM_PROVIDER=azure")
    return cfg


@pytest.fixture
def provider(live_settings: Settings) -> AzureOpenAIProvider:
    """A real Azure provider (function scoped: the HTTP client is bound to the event loop)."""
    return AzureOpenAIProvider.from_settings(live_settings)


@dataclass
class Answer:
    """Result of one timed LLM call."""

    text: str
    input_tokens: int
    output_tokens: int
    elapsed: float


@pytest.fixture
def ask(provider: AzureOpenAIProvider):  # type: ignore[no-untyped-def]
    """Return ``ask(message, system="", max_tokens=...)`` that times a real LLM call."""

    async def _ask(message: str, system: str = "", max_tokens: int = 200) -> Answer:
        messages = ([{"role": "system", "content": system}] if system else []) + [
            {"role": "user", "content": message}
        ]
        started = time.perf_counter()
        result = await provider.complete(messages, max_tokens=max_tokens, temperature=0.0)
        elapsed = time.perf_counter() - started
        return Answer(result.text, result.usage.input_tokens, result.usage.output_tokens, elapsed)

    return _ask


@pytest.fixture
def local_rag() -> RAGTool:
    """Deterministic local RAG tool (no AWS)."""
    return RAGTool()
