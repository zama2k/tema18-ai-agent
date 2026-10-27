"""LLM providers behind one interface: Azure OpenAI, Amazon Bedrock and a deterministic mock."""

from __future__ import annotations

import asyncio
import json
import logging
import re
from abc import ABC, abstractmethod
from typing import Any

import openai
from botocore.exceptions import BotoCoreError, ClientError

from src.bedrock_format import build_bedrock_request, parse_bedrock_response
from src.config import Settings
from src.exceptions import (
    ConfigurationError,
    ContentFilteredError,
    ProviderError,
    ProviderTimeoutError,
)
from src.models import LLMResponse, Message, TokenUsage, ToolCall, ToolSchema

logger = logging.getLogger(__name__)

_REASONING_MODEL = re.compile(r"^(o\d|gpt-5)", re.IGNORECASE)


def is_reasoning_model(model: str) -> bool:
    """Return True for reasoning deployments, which reject temperature/top_p/max_tokens."""
    return bool(_REASONING_MODEL.match(model))


class LLMProvider(ABC):
    """Common interface of every LLM backend."""

    name: str = "base"

    @abstractmethod
    async def complete(
        self,
        messages: list[Message],
        tools: list[ToolSchema] | None = None,
        *,
        model: str | None = None,
        max_tokens: int = 1024,
        temperature: float = 0.2,
    ) -> LLMResponse:
        """Run one chat completion.

        Args:
            messages: OpenAI-style chat messages.
            tools: OpenAI-style tool schemas, or ``None`` to disable tool calling.
            model: Model/deployment override.
            max_tokens: Maximum visible output tokens.
            temperature: Sampling temperature (ignored by reasoning models).

        Raises:
            ProviderTimeoutError: If the backend times out.
            ProviderError: For any other backend failure.
        """


class AzureOpenAIProvider(LLMProvider):
    """Azure OpenAI through its OpenAI-compatible v1 endpoint."""

    name = "azure"

    def __init__(
        self,
        client: openai.AsyncOpenAI,
        default_model: str,
        reasoning_headroom: int = 4000,
    ) -> None:
        self._client = client
        self._default_model = default_model
        self._headroom = reasoning_headroom

    @classmethod
    def from_settings(cls, settings: Settings) -> AzureOpenAIProvider:
        """Build the provider from validated settings."""
        if not (settings.azure_openai_endpoint and settings.azure_openai_api_key):
            raise ConfigurationError("Azure OpenAI endpoint and API key are required")
        client = openai.AsyncOpenAI(
            base_url=settings.azure_openai_endpoint,
            api_key=settings.azure_openai_api_key.get_secret_value(),
            timeout=settings.llm_timeout_seconds,
            max_retries=2,
        )
        return cls(client, settings.chat_model, settings.reasoning_headroom_tokens)

    async def complete(
        self,
        messages: list[Message],
        tools: list[ToolSchema] | None = None,
        *,
        model: str | None = None,
        max_tokens: int = 1024,
        temperature: float = 0.2,
    ) -> LLMResponse:
        """See :meth:`LLMProvider.complete`."""
        deployment = model or self._default_model
        kwargs: dict[str, Any] = {"model": deployment, "messages": messages}
        if is_reasoning_model(deployment):
            # Reasoning tokens count against the cap: leave generous headroom.
            kwargs["max_completion_tokens"] = max_tokens + self._headroom
        else:
            kwargs["max_completion_tokens"] = max_tokens
            kwargs["temperature"] = temperature
        if tools:
            kwargs["tools"] = tools
        try:
            completion = await self._client.chat.completions.create(**kwargs)
        except openai.APITimeoutError as exc:
            raise ProviderTimeoutError("Azure OpenAI request timed out") from exc
        except openai.APIConnectionError as exc:
            raise ProviderError("could not reach Azure OpenAI") from exc
        except openai.APIStatusError as exc:
            if exc.status_code == 400 and getattr(exc, "code", None) == "content_filter":
                raise ContentFilteredError("request blocked by the content filter") from exc
            raise ProviderError(
                f"Azure OpenAI returned HTTP {exc.status_code}: {exc.message}"
            ) from exc
        except openai.OpenAIError as exc:
            raise ProviderError(f"Azure OpenAI client error: {type(exc).__name__}") from exc

        if not completion.choices:
            raise ProviderError("Azure OpenAI returned no choices")
        choice = completion.choices[0]
        if choice.finish_reason == "content_filter":
            # The platform filter cut or withheld the completion: same outcome as a blocked prompt.
            raise ContentFilteredError("completion blocked by the content filter")
        if choice.finish_reason == "length":
            logger.warning(
                "completion truncated by the token limit",
                extra={
                    "model": deployment,
                    "max_completion_tokens": kwargs["max_completion_tokens"],
                },
            )
        message = choice.message
        calls = [_parse_openai_tool_call(c) for c in (message.tool_calls or [])]
        usage = completion.usage
        return LLMResponse(
            text=(message.content or "").strip(),
            tool_calls=calls,
            usage=TokenUsage(
                int(getattr(usage, "prompt_tokens", 0) or 0),
                int(getattr(usage, "completion_tokens", 0) or 0),
            ),
            model=completion.model or deployment,
            finish_reason=choice.finish_reason,
        )


def _parse_openai_tool_call(call: Any) -> ToolCall:
    raw = call.function.arguments or ""
    try:
        parsed = json.loads(raw) if raw else {}
        arguments = parsed if isinstance(parsed, dict) else None
    except json.JSONDecodeError:
        arguments = None
    return ToolCall(id=call.id, name=call.function.name, arguments=arguments, raw_arguments=raw)


class BedrockProvider(LLMProvider):
    """Anthropic models on Amazon Bedrock (``invoke_model``, Messages format).

    The blocking boto3 call runs in a worker thread so the event loop stays free.
    """

    name = "bedrock"

    def __init__(self, client: Any, model_id: str) -> None:
        self._client = client
        self._model_id = model_id

    @classmethod
    def from_settings(cls, settings: Settings) -> BedrockProvider:
        """Build a boto3 ``bedrock-runtime`` client from settings."""
        import boto3
        from botocore.config import Config

        client = boto3.client(
            "bedrock-runtime",
            region_name=settings.aws_default_region,
            config=Config(
                read_timeout=settings.llm_timeout_seconds,
                connect_timeout=10,
                retries={"max_attempts": 3, "mode": "standard"},
            ),
        )
        return cls(client, settings.bedrock_model_id)

    async def complete(
        self,
        messages: list[Message],
        tools: list[ToolSchema] | None = None,
        *,
        model: str | None = None,
        max_tokens: int = 1024,
        temperature: float = 0.2,
    ) -> LLMResponse:
        """See :meth:`LLMProvider.complete`."""
        model_id = model or self._model_id
        body = build_bedrock_request(messages, tools, max_tokens, temperature)
        try:
            raw = await asyncio.to_thread(self._invoke, model_id, body)
            parsed = parse_bedrock_response(raw)
        except ClientError as exc:
            code = exc.response.get("Error", {}).get("Code", "ClientError")
            if code in {"ModelTimeoutException", "RequestTimeout"}:
                raise ProviderTimeoutError(f"Bedrock timeout ({code})") from exc
            raise ProviderError(f"Bedrock call failed ({code})") from exc
        except BotoCoreError as exc:
            raise ProviderError(f"Bedrock transport error: {type(exc).__name__}") from exc
        except (ValueError, KeyError) as exc:
            raise ProviderError(f"Bedrock returned an unusable response: {exc}") from exc
        parsed.model = model_id
        if parsed.truncated:
            logger.warning("completion truncated by the token limit", extra={"model": model_id})
        return parsed

    def _invoke(self, model_id: str, body: dict[str, Any]) -> dict[str, Any]:
        response = self._client.invoke_model(
            modelId=model_id,
            body=json.dumps(body),
            contentType="application/json",
            accept="application/json",
        )
        payload = json.loads(response["body"].read())
        if not isinstance(payload, dict):
            raise ValueError("Bedrock body is not a JSON object")
        return payload


class MockProvider(LLMProvider):
    """Deterministic offline provider for CI without secrets and simulated hosts.

    Behaviour (rule based, no randomness):

    * ``__fail__`` in the user text raises a provider error (error-path testing).
    * A ``search``/``busca`` request triggers a ``web_search`` tool call when available.
    * After a tool result, it summarises that result.
    * A few canned answers keep smoke tests meaningful.
    """

    name = "mock"

    async def complete(
        self,
        messages: list[Message],
        tools: list[ToolSchema] | None = None,
        *,
        model: str | None = None,
        max_tokens: int = 1024,
        temperature: float = 0.2,
    ) -> LLMResponse:
        """See :meth:`LLMProvider.complete`."""
        last = messages[-1] if messages else {"role": "user", "content": ""}
        user_text = next((str(m["content"]) for m in reversed(messages) if m["role"] == "user"), "")
        lowered = user_text.lower()
        prompt_tokens = sum(len(str(m.get("content") or "")) for m in messages) // 4 + 1

        if "__fail__" in lowered:
            raise ProviderError("mock failure injected")

        tool_names = {t["function"]["name"] for t in (tools or [])}
        calls: list[ToolCall] = []
        if last["role"] == "tool":
            text = f"Según la herramienta: {str(last['content'])[:200]}"
        elif ("search" in lowered or "busca" in lowered) and "web_search" in tool_names:
            args = {"query": user_text}
            calls = [ToolCall("call_mock_1", "web_search", args, json.dumps(args))]
            text = ""
        elif "capital" in lowered and ("francia" in lowered or "france" in lowered):
            text = "La capital de Francia es París."
        elif "solo sí o no" in lowered or "only yes or no" in lowered:
            text = "SÍ"
        else:
            text = f"Hola, soy un asistente de pruebas. Has dicho: {user_text[:200]}"

        return LLMResponse(
            text=text,
            tool_calls=calls,
            usage=TokenUsage(prompt_tokens, max(1, len(text) // 4)),
            model=model or "mock-model",
            finish_reason="tool_calls" if calls else "stop",
        )


def create_provider(settings: Settings) -> LLMProvider:
    """Instantiate the provider selected by ``LLM_PROVIDER``."""
    if settings.llm_provider == "mock":
        return MockProvider()
    if settings.llm_provider == "bedrock":
        return BedrockProvider.from_settings(settings)
    return AzureOpenAIProvider.from_settings(settings)
