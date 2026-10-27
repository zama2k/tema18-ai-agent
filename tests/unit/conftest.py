"""Fixtures for unit tests: no network, scripted providers, default settings."""

from __future__ import annotations

from collections.abc import Iterator
from typing import Any

import pytest
from pytest_socket import enable_socket, socket_allow_hosts

from src.config import Settings
from src.exceptions import ProviderError
from src.models import LLMResponse, Message, TokenUsage, ToolCall, ToolSchema
from src.providers import LLMProvider
from src.tools import Tool


@pytest.fixture(autouse=True)
def _no_network() -> Iterator[None]:
    """Unit tests must be hermetic: connecting anywhere but loopback raises SocketBlockedError."""
    socket_allow_hosts(["127.0.0.1", "::1"], allow_unix_socket=True)
    yield
    enable_socket()


@pytest.fixture
def settings() -> Settings:
    """Settings backed by the mock provider, ignoring any env file."""
    return Settings(llm_provider="mock", _env_file=None)  # type: ignore[call-arg]


class ScriptedProvider(LLMProvider):
    """Provider that replays a list of responses (or raises exceptions)."""

    name = "scripted"

    def __init__(self, script: list[LLMResponse | Exception]) -> None:
        self.script = list(script)
        self.calls: list[dict[str, Any]] = []

    async def complete(
        self,
        messages: list[Message],
        tools: list[ToolSchema] | None = None,
        *,
        model: str | None = None,
        max_tokens: int = 1024,
        temperature: float = 0.2,
    ) -> LLMResponse:
        self.calls.append({"messages": [dict(m) for m in messages], "tools": tools, "model": model})
        if not self.script:
            raise ProviderError("script exhausted")
        item = self.script.pop(0)
        if isinstance(item, Exception):
            raise item
        return item


class EchoTool(Tool):
    """Tool returning its input, used to exercise the agent loop."""

    name = "echo"
    description = "Echo the text."
    parameters = {
        "type": "object",
        "properties": {"text": {"type": "string"}},
        "required": ["text"],
    }

    async def run(self, **kwargs: Any) -> str:
        return f"echo:{kwargs['text']}"


def text_response(text: str, tokens: tuple[int, int] = (10, 5)) -> LLMResponse:
    """Build a plain text LLM response."""
    return LLMResponse(text=text, usage=TokenUsage(*tokens), model="gpt-4o-mini")


def tool_response(*calls: tuple[str, str, dict[str, Any] | None]) -> LLMResponse:
    """Build a response that requests tool calls: (id, name, arguments)."""
    return LLMResponse(
        text="",
        tool_calls=[ToolCall(i, n, a, "{}" if a is None else str(a)) for i, n, a in calls],
        usage=TokenUsage(20, 8),
        model="gpt-4o-mini",
    )
