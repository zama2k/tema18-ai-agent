"""Unit tests for LLM providers (no real network: respx, botocore Stubber)."""

from __future__ import annotations

import io
import json
from typing import Any

import httpx2
import openai
import pytest
from botocore.client import BaseClient
from botocore.response import StreamingBody
from botocore.stub import ANY, Stubber

from src.bedrock_format import build_bedrock_request
from src.config import Settings
from src.exceptions import ConfigurationError, ProviderError, ProviderTimeoutError
from src.providers import (
    AzureOpenAIProvider,
    BedrockProvider,
    MockProvider,
    create_provider,
    is_reasoning_model,
)

BASE = "https://example.test/openai/v1"
CHAT_URL = f"{BASE}/chat/completions"
SEARCH_TOOL = {
    "type": "function",
    "function": {
        "name": "web_search",
        "description": "Search",
        "parameters": {"type": "object", "properties": {"query": {"type": "string"}}},
    },
}


def completion_json(
    content: str | None = "hola", tool_calls: list[dict[str, Any]] | None = None
) -> dict[str, Any]:
    message: dict[str, Any] = {"role": "assistant", "content": content}
    if tool_calls:
        message["tool_calls"] = tool_calls
    return {
        "id": "chatcmpl-1",
        "object": "chat.completion",
        "created": 1,
        "model": "gpt-4o-mini-2024-07-18",
        "choices": [{"index": 0, "message": message, "finish_reason": "stop"}],
        "usage": {"prompt_tokens": 11, "completion_tokens": 7, "total_tokens": 18},
    }


class FakeAzure:
    """In-process Azure endpoint: records requests and answers with a canned response.

    openai>=3 is built on ``httpx2`` (not ``httpx``), so respx cannot intercept it; a
    ``MockTransport`` injected as the HTTP client is the supported way to fake the server.
    """

    def __init__(self, response: httpx2.Response | Exception) -> None:
        self.response = response
        self.requests: list[httpx2.Request] = []

    def handler(self, request: httpx2.Request) -> httpx2.Response:
        self.requests.append(request)
        if isinstance(self.response, Exception):
            raise self.response
        return self.response

    @property
    def last_body(self) -> dict[str, Any]:
        return json.loads(self.requests[-1].content)

    def provider(self, headroom: int = 4000) -> AzureOpenAIProvider:
        http = httpx2.AsyncClient(transport=httpx2.MockTransport(self.handler))
        client = openai.AsyncOpenAI(
            base_url=BASE, api_key="test-key", max_retries=0, http_client=http
        )
        return AzureOpenAIProvider(client, "gpt-4o-mini", headroom)


def ok(content: str | None = "hola", tool_calls: list[dict[str, Any]] | None = None) -> FakeAzure:
    return FakeAzure(httpx2.Response(200, json=completion_json(content, tool_calls)))


class TestReasoningDetection:
    @pytest.mark.parametrize("model", ["gpt-5.6-sol", "o3-mini", "GPT-5-mini"])
    def test_reasoning_models(self, model: str) -> None:
        assert is_reasoning_model(model)

    @pytest.mark.parametrize("model", ["gpt-4o-mini", "gpt-4.1", "claude-3-haiku"])
    def test_regular_models(self, model: str) -> None:
        assert not is_reasoning_model(model)


class TestAzureProvider:
    async def test_regular_model_payload_and_parsing(self) -> None:
        fake = ok()
        result = await fake.provider().complete([{"role": "user", "content": "hi"}], max_tokens=50)
        body = fake.last_body
        assert str(fake.requests[-1].url) == CHAT_URL
        assert body["model"] == "gpt-4o-mini"
        assert body["max_completion_tokens"] == 50
        assert "temperature" in body and "tools" not in body
        assert fake.requests[-1].headers["authorization"] == "Bearer test-key"
        assert result.text == "hola"
        assert (result.usage.input_tokens, result.usage.output_tokens) == (11, 7)
        assert result.model == "gpt-4o-mini-2024-07-18"

    async def test_reasoning_model_omits_sampling_params_and_adds_headroom(self) -> None:
        fake = ok()
        await fake.provider(4000).complete(
            [{"role": "user", "content": "hi"}], model="gpt-5.6-sol", max_tokens=100
        )
        body = fake.last_body
        assert body["max_completion_tokens"] == 4100
        for forbidden in ("temperature", "top_p", "stop", "max_tokens"):
            assert forbidden not in body

    async def test_tool_calls_are_parsed(self) -> None:
        calls = [
            {
                "id": "call_1",
                "type": "function",
                "function": {"name": "web_search", "arguments": '{"query": "iva"}'},
            },
            {
                "id": "call_2",
                "type": "function",
                "function": {"name": "web_search", "arguments": "{not json"},
            },
        ]
        fake = ok(None, calls)
        result = await fake.provider().complete([{"role": "user", "content": "hi"}], [SEARCH_TOOL])
        assert fake.last_body["tools"] == [SEARCH_TOOL]
        assert result.text == ""
        assert result.tool_calls[0].arguments == {"query": "iva"}
        assert result.tool_calls[1].arguments is None
        assert result.tool_calls[1].raw_arguments == "{not json"

    async def test_http_error_becomes_provider_error(self) -> None:
        fake = FakeAzure(httpx2.Response(429, json={"error": {"message": "slow down"}}))
        with pytest.raises(ProviderError, match="429"):
            await fake.provider().complete([{"role": "user", "content": "hi"}])

    async def test_timeout_becomes_provider_timeout(self) -> None:
        fake = FakeAzure(httpx2.ReadTimeout("slow"))
        with pytest.raises(ProviderTimeoutError):
            await fake.provider().complete([{"role": "user", "content": "hi"}])

    async def test_connection_error_becomes_provider_error(self) -> None:
        fake = FakeAzure(httpx2.ConnectError("refused"))
        with pytest.raises(ProviderError, match="reach"):
            await fake.provider().complete([{"role": "user", "content": "hi"}])

    async def test_no_choices_is_an_error(self) -> None:
        body = completion_json()
        body["choices"] = []
        fake = FakeAzure(httpx2.Response(200, json=body))
        with pytest.raises(ProviderError, match="no choices"):
            await fake.provider().complete([{"role": "user", "content": "hi"}])

    def test_from_settings(self) -> None:
        cfg = Settings(  # type: ignore[call-arg]
            azure_openai_endpoint=BASE, azure_openai_api_key="k", _env_file=None
        )
        provider = AzureOpenAIProvider.from_settings(cfg)
        assert provider.name == "azure"
        assert isinstance(create_provider(cfg), AzureOpenAIProvider)

    def test_from_settings_without_credentials_fails(self) -> None:
        cfg = Settings.model_construct(llm_provider="azure")
        with pytest.raises(ConfigurationError):
            AzureOpenAIProvider.from_settings(cfg)


def stubbed_bedrock(payload: Any, error: str | None = None) -> tuple[BedrockProvider, Stubber]:
    import boto3

    client = boto3.client(
        "bedrock-runtime",
        region_name="us-east-1",
        aws_access_key_id="test",
        aws_secret_access_key="test",
    )
    stubber = Stubber(client)
    model_id = "anthropic.claude-3-haiku-20240307-v1:0"
    expected = {
        "modelId": model_id,
        "body": ANY,
        "contentType": "application/json",
        "accept": "application/json",
    }
    if error:
        stubber.add_client_error("invoke_model", service_error_code=error, expected_params=expected)
    else:
        raw = json.dumps(payload).encode()
        stubber.add_response(
            "invoke_model",
            {"body": StreamingBody(io.BytesIO(raw), len(raw)), "contentType": "application/json"},
            expected,
        )
    stubber.activate()
    return BedrockProvider(client, model_id), stubber


class TestBedrockProvider:
    OK = {
        "content": [{"type": "text", "text": "Hola desde Bedrock"}],
        "usage": {"input_tokens": 9, "output_tokens": 4},
        "stop_reason": "end_turn",
    }

    async def test_invoke_model_roundtrip(self) -> None:
        provider, stubber = stubbed_bedrock(self.OK)
        result = await provider.complete([{"role": "user", "content": "hola"}])
        stubber.assert_no_pending_responses()
        assert result.text == "Hola desde Bedrock"
        assert result.usage.total == 13
        assert result.model == "anthropic.claude-3-haiku-20240307-v1:0"
        assert result.finish_reason == "end_turn"

    @pytest.mark.parametrize(
        ("code", "exc"),
        [
            ("ThrottlingException", ProviderError),
            ("ModelTimeoutException", ProviderTimeoutError),
            ("AccessDeniedException", ProviderError),
        ],
    )
    async def test_client_errors_are_mapped(self, code: str, exc: type[Exception]) -> None:
        provider, _ = stubbed_bedrock(None, error=code)
        with pytest.raises(exc, match=code):
            await provider.complete([{"role": "user", "content": "hola"}])

    async def test_malformed_body_becomes_provider_error(self) -> None:
        provider, _ = stubbed_bedrock({"content": [], "usage": {}})
        with pytest.raises(ProviderError, match="unusable"):
            await provider.complete([{"role": "user", "content": "hola"}])

    async def test_non_object_body_becomes_provider_error(self) -> None:
        provider, _ = stubbed_bedrock([1, 2])
        with pytest.raises(ProviderError, match="unusable"):
            await provider.complete([{"role": "user", "content": "hola"}])

    def test_from_settings_builds_a_client(self) -> None:
        cfg = Settings(llm_provider="bedrock", _env_file=None)  # type: ignore[call-arg]
        provider = create_provider(cfg)
        assert isinstance(provider, BedrockProvider)
        assert isinstance(provider._client, BaseClient)


class TestBedrockRequest:
    def test_system_tools_and_tool_results_are_converted(self) -> None:
        messages = [
            {"role": "system", "content": "sys"},
            {"role": "user", "content": "q"},
            {
                "role": "assistant",
                "content": None,
                "tool_calls": [
                    {
                        "id": "t1",
                        "type": "function",
                        "function": {"name": "web_search", "arguments": '{"query": "x"}'},
                    }
                ],
            },
            {"role": "tool", "tool_call_id": "t1", "content": "result"},
        ]
        body = build_bedrock_request(messages, [SEARCH_TOOL], 100, 0.0)
        assert body["anthropic_version"] == "bedrock-2023-05-31"
        assert body["system"] == "sys"
        assert body["max_tokens"] == 100
        assert body["tools"][0]["input_schema"]["type"] == "object"
        assert [m["role"] for m in body["messages"]] == ["user", "assistant", "user"]
        assert body["messages"][1]["content"][0] == {
            "type": "tool_use",
            "id": "t1",
            "name": "web_search",
            "input": {"query": "x"},
        }
        assert body["messages"][2]["content"][0]["type"] == "tool_result"

    def test_consecutive_same_role_turns_are_merged(self) -> None:
        body = build_bedrock_request(
            [{"role": "user", "content": "a"}, {"role": "user", "content": "b"}], None, 10, 0.2
        )
        assert len(body["messages"]) == 1 and len(body["messages"][0]["content"]) == 2
        assert "tools" not in body and "system" not in body

    def test_unknown_role_is_rejected(self) -> None:
        with pytest.raises(ValueError, match="role"):
            build_bedrock_request([{"role": "alien", "content": "x"}], None, 10, 0.2)

    def test_assistant_text_content_is_kept(self) -> None:
        body = build_bedrock_request(
            [{"role": "user", "content": "q"}, {"role": "assistant", "content": "a"}], None, 10, 0.2
        )
        assert body["messages"][1]["content"] == [{"type": "text", "text": "a"}]


class TestMockProvider:
    async def test_is_deterministic(self) -> None:
        provider = MockProvider()
        msgs = [{"role": "user", "content": "hola"}]
        first = await provider.complete(msgs)
        second = await provider.complete(msgs)
        assert first.text == second.text and first.usage == second.usage

    async def test_capital_of_france(self) -> None:
        result = await MockProvider().complete(
            [{"role": "user", "content": "¿Cuál es la capital de Francia?"}]
        )
        assert "París" in result.text

    async def test_yes_no(self) -> None:
        result = await MockProvider().complete(
            [{"role": "user", "content": "Responde solo sí o no: ¿2+2=4?"}]
        )
        assert result.text == "SÍ"

    async def test_search_triggers_tool_call_then_summarises(self) -> None:
        provider = MockProvider()
        first = await provider.complete(
            [{"role": "user", "content": "busca noticias"}], [SEARCH_TOOL]
        )
        assert first.tool_calls and first.tool_calls[0].name == "web_search"
        second = await provider.complete(
            [
                {"role": "user", "content": "busca noticias"},
                {"role": "tool", "tool_call_id": "call_mock_1", "content": "dato"},
            ]
        )
        assert "dato" in second.text

    async def test_search_without_tool_answers_directly(self) -> None:
        result = await MockProvider().complete([{"role": "user", "content": "busca algo"}])
        assert not result.tool_calls

    async def test_failure_injection(self) -> None:
        with pytest.raises(ProviderError, match="injected"):
            await MockProvider().complete([{"role": "user", "content": "x __fail__"}])

    def test_factory_returns_mock(self) -> None:
        cfg = Settings(llm_provider="mock", _env_file=None)  # type: ignore[call-arg]
        assert isinstance(create_provider(cfg), MockProvider)
