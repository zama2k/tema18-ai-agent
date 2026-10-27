"""Plain data types shared by the agent, providers and tools."""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any

Message = dict[str, Any]
ToolSchema = dict[str, Any]


@dataclass
class ToolCall:
    """A tool invocation requested by the model."""

    id: str
    name: str
    arguments: dict[str, Any] | None
    raw_arguments: str = ""


@dataclass
class TokenUsage:
    """Token accounting for one or more LLM calls."""

    input_tokens: int = 0
    output_tokens: int = 0

    def __add__(self, other: TokenUsage) -> TokenUsage:
        return TokenUsage(
            self.input_tokens + other.input_tokens, self.output_tokens + other.output_tokens
        )

    @property
    def total(self) -> int:
        """Total tokens."""
        return self.input_tokens + self.output_tokens


@dataclass
class LLMResponse:
    """Provider-agnostic answer of one LLM call."""

    text: str
    tool_calls: list[ToolCall] = field(default_factory=list)
    usage: TokenUsage = field(default_factory=TokenUsage)
    model: str = ""
    finish_reason: str | None = None

    @property
    def truncated(self) -> bool:
        """True when the model stopped because it hit the output token limit."""
        return self.finish_reason in {"length", "max_tokens"}


@dataclass
class AgentResult:
    """Final result of ``AIAgent.process_query``."""

    response: str
    route: str
    usage: TokenUsage
    cost_usd: float
    model: str
    iterations: int
    tools_used: list[str] = field(default_factory=list)
    tool_failures: int = 0
    truncated: bool = False
    latency_seconds: float = 0.0
