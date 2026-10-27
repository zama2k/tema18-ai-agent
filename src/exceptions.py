"""Custom exception hierarchy for the agent."""

from __future__ import annotations


class AgentBaseException(Exception):  # noqa: N818 - name required by the project spec
    """Base class for every error raised on purpose by the agent."""


class ConfigurationError(AgentBaseException):
    """Raised when the runtime configuration is missing or invalid."""


class ProviderError(AgentBaseException):
    """Raised when the LLM provider fails (network, quota, malformed answer)."""


class ProviderTimeoutError(ProviderError):
    """Raised when the LLM provider does not answer within the configured timeout."""


class ToolError(AgentBaseException):
    """Base class for tool failures."""

    def __init__(self, tool: str, message: str) -> None:
        super().__init__(f"{tool}: {message}")
        self.tool = tool


class TavilySearchError(ToolError):
    """Raised when the Tavily web search fails."""

    def __init__(self, message: str) -> None:
        super().__init__("web_search", message)


class RAGRetrievalError(ToolError):
    """Raised when document retrieval fails."""

    def __init__(self, message: str) -> None:
        super().__init__("knowledge_base", message)


class MaxIterationsError(AgentBaseException):
    """Raised when the tool-calling loop cannot produce a final answer."""
