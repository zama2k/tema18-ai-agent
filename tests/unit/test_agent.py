"""Unit tests for the agent core: routing, prompting, response parsing, costing and the loop.

Everything is deterministic and offline: providers are scripted and no socket is opened.
"""

from __future__ import annotations

import asyncio
from typing import Any

import pytest

from src.agent import (
    AIAgent,
    build_system_prompt,
    calculate_cost,
    parse_bedrock_response,
    route_message,
)
from src.config import Settings
from src.exceptions import MaxIterationsError, ProviderError, ProviderTimeoutError, ToolError
from src.models import LLMResponse
from src.observability import Tracer
from src.tools import Tool
from tests.unit.conftest import EchoTool, ScriptedProvider, text_response, tool_response


class TestRouter:
    """``route_message`` decides between deep research and normal chat."""

    @pytest.mark.parametrize(
        "text",
        [
            "Necesito un informe sobre IA",
            "Write a report about CI/CD",
            "investiga a fondo el tema del RAG",
            "Do a deep research on vector databases",
        ],
    )
    def test_research_keywords_route_to_deep_research(self, text: str) -> None:
        """Each documented keyword must trigger the deep research flow."""
        assert route_message(text) == "deep_research"

    @pytest.mark.parametrize("text", ["INFORME trimestral", "Deep Research please", "REPORT"])
    def test_keyword_matching_is_case_insensitive(self, text: str) -> None:
        """Users do not respect casing."""
        assert route_message(text) == "deep_research"

    @pytest.mark.parametrize("text", ["Hello", "How are you?", "Que hora es", "2 + 2"])
    def test_general_queries_use_chat_with_tools(self, text: str) -> None:
        """Anything without a keyword stays on the default flow."""
        assert route_message(text) == "chat_with_tools"

    @pytest.mark.parametrize("text", ["", "   ", None, "?!", "...", "¿¡¿!?", "\n\t"])
    def test_empty_and_punctuation_default_to_chat(self, text: str | None) -> None:
        """Edge cases must not crash and must not look like research requests."""
        assert route_message(text) == "chat_with_tools"

    def test_keyword_inside_punctuation_still_matches(self) -> None:
        """Punctuation around the keyword must not hide it."""
        assert route_message("¡¡Informe!!") == "deep_research"


class TestSystemPrompt:
    """``build_system_prompt`` must always set role and language."""

    def test_contains_assistant_role_and_spanish(self) -> None:
        prompt = build_system_prompt().lower()
        assert "assistant" in prompt or "asistente" in prompt
        assert "spanish" in prompt or "español" in prompt

    def test_lists_available_tools(self) -> None:
        prompt = build_system_prompt(tool_names=["web_search", "knowledge_base"])
        assert "web_search" in prompt and "knowledge_base" in prompt

    def test_warns_about_untrusted_tool_output(self) -> None:
        """Prompt-injection hardening is part of the contract."""
        assert "untrusted" in build_system_prompt().lower()

    def test_deep_research_adds_report_instructions(self) -> None:
        assert "report" in build_system_prompt("deep_research").lower()
        assert "report" not in build_system_prompt("chat_with_tools").lower()


class TestResponseParsing:
    """``parse_bedrock_response`` understands the Bedrock Messages format."""

    @staticmethod
    def _response(**overrides: Any) -> dict[str, Any]:
        body: dict[str, Any] = {
            "content": [{"type": "text", "text": "Hola"}],
            "usage": {"input_tokens": 12, "output_tokens": 34},
        }
        body.update(overrides)
        return body

    def test_parses_text_and_usage(self) -> None:
        parsed = parse_bedrock_response(self._response())
        assert parsed.text == "Hola"
        assert (parsed.usage.input_tokens, parsed.usage.output_tokens) == (12, 34)

    def test_joins_multiple_text_blocks(self) -> None:
        content = [{"type": "text", "text": "Hola "}, {"type": "text", "text": "mundo"}]
        assert parse_bedrock_response(self._response(content=content)).text == "Hola mundo"

    def test_parses_tool_use_blocks(self) -> None:
        content = [
            {"type": "tool_use", "id": "tu_1", "name": "web_search", "input": {"query": "x"}}
        ]
        parsed = parse_bedrock_response(self._response(content=content))
        assert parsed.text == ""
        assert parsed.tool_calls[0].name == "web_search"
        assert parsed.tool_calls[0].arguments == {"query": "x"}

    @pytest.mark.parametrize(
        "bad",
        [
            {"content": [], "usage": {"input_tokens": 1, "output_tokens": 1}},
            {"usage": {"input_tokens": 1, "output_tokens": 1}},
            {"content": "text", "usage": {"input_tokens": 1, "output_tokens": 1}},
            {
                "content": [{"type": "text", "text": "   "}],
                "usage": {"input_tokens": 1, "output_tokens": 1},
            },
            {"content": [{"type": "text"}], "usage": {"input_tokens": 1, "output_tokens": 1}},
            {"content": [{"text": "no type"}], "usage": {"input_tokens": 1, "output_tokens": 1}},
            {"content": ["not a dict"], "usage": {"input_tokens": 1, "output_tokens": 1}},
            {"content": [{"type": "text", "text": "ok"}]},
            {"content": [{"type": "text", "text": "ok"}], "usage": {"input_tokens": "1"}},
            {
                "content": [{"type": "tool_use", "input": {}}],
                "usage": {"input_tokens": 1, "output_tokens": 1},
            },
        ],
    )
    def test_empty_or_malformed_raises_value_error(self, bad: dict[str, Any]) -> None:
        with pytest.raises(ValueError):
            parse_bedrock_response(bad)

    def test_non_dict_raises_value_error(self) -> None:
        with pytest.raises(ValueError):
            parse_bedrock_response([])  # type: ignore[arg-type]


class TestCostCalculation:
    """``calculate_cost`` bills per million tokens, by model tier."""

    def test_haiku_cost(self) -> None:
        # 1M input at $0.25 + 1M output at $1.25
        assert calculate_cost("haiku", 1_000_000, 1_000_000) == pytest.approx(1.50, abs=1e-9)

    def test_small_call_precision(self) -> None:
        expected_cost = (1000 * 0.25 + 500 * 1.25) / 1_000_000
        assert calculate_cost("claude-3-haiku", 1000, 500) == pytest.approx(
            expected_cost, abs=1e-12
        )

    @pytest.mark.parametrize(
        ("model", "expected_per_million"),
        [
            ("gpt-4o-mini", 0.15 + 0.60),
            ("anthropic.claude-3-haiku-20240307-v1:0", 0.25 + 1.25),
            ("sonnet", 3.0 + 15.0),
            ("claude-3-opus", 15.0 + 75.0),
            ("gpt-5.6-sol", 4.0 + 20.0),
        ],
    )
    def test_model_tiers(self, model: str, expected_per_million: float) -> None:
        assert calculate_cost(model, 1_000_000, 1_000_000) == pytest.approx(
            expected_per_million, abs=1e-9
        )

    @pytest.mark.parametrize("model", ["haiku", "gpt-4o-mini", "gpt-5.6-sol"])
    def test_zero_tokens_cost_zero(self, model: str) -> None:
        assert calculate_cost(model, 0, 0) == 0.0

    def test_unknown_model_raises(self) -> None:
        with pytest.raises(ValueError, match="no price"):
            calculate_cost("mystery-model", 1, 1)

    def test_negative_tokens_raise(self) -> None:
        with pytest.raises(ValueError, match="non-negative"):
            calculate_cost("haiku", -1, 0)

    def test_price_overrides_from_environment(self, monkeypatch: pytest.MonkeyPatch) -> None:
        monkeypatch.setenv("PRICE_OVERRIDES", '{"gpt-5.6-sol": [1.0, 2.0]}')
        assert calculate_cost("gpt-5.6-sol", 1_000_000, 1_000_000) == pytest.approx(3.0)

    def test_invalid_price_overrides_raise(self, monkeypatch: pytest.MonkeyPatch) -> None:
        monkeypatch.setenv("PRICE_OVERRIDES", "not json")
        with pytest.raises(ValueError, match="PRICE_OVERRIDES"):
            calculate_cost("haiku", 1, 1)


def make_agent(
    provider: ScriptedProvider, settings: Settings, tools: list[Tool] | None = None, **kw: Any
) -> AIAgent:
    return AIAgent(provider, tools if tools is not None else [EchoTool()], settings, **kw)


class TestAgentLoop:
    """The tool-calling loop: happy path, limits and failure handling."""

    async def test_direct_answer_without_tools(self, settings: Settings) -> None:
        provider = ScriptedProvider([text_response("Hola", (100, 50))])
        result = await make_agent(provider, settings).process_query("Hello")
        assert result.response == "Hola"
        assert result.route == "chat_with_tools"
        assert result.iterations == 1
        assert result.usage.total == 150
        assert result.cost_usd == pytest.approx(calculate_cost("gpt-4o-mini", 100, 50))

    async def test_truncation_is_reported(self, settings: Settings) -> None:
        cut = LLMResponse(text="respuesta cort", finish_reason="length")
        result = await make_agent(ScriptedProvider([cut]), settings).process_query("Hello")
        assert result.truncated is True

    async def test_complete_answers_are_not_flagged(self, settings: Settings) -> None:
        result = await make_agent(ScriptedProvider([text_response("ok")]), settings).process_query(
            "x"
        )
        assert result.truncated is False

    async def test_system_prompt_and_tools_are_sent(self, settings: Settings) -> None:
        provider = ScriptedProvider([text_response("ok")])
        await make_agent(provider, settings).process_query("Hello")
        call = provider.calls[0]
        assert call["messages"][0]["role"] == "system"
        assert call["tools"][0]["function"]["name"] == "echo"
        assert call["model"] == settings.chat_model

    async def test_tool_call_then_final_answer(self, settings: Settings) -> None:
        provider = ScriptedProvider(
            [tool_response(("c1", "echo", {"text": "hi"})), text_response("done")]
        )
        agent = make_agent(provider, settings)
        result = await agent.process_query("use the tool")
        assert result.response == "done"
        assert result.tools_used == ["echo"]
        assert result.iterations == 2
        second_call_messages = provider.calls[1]["messages"]
        assert second_call_messages[-1] == {
            "role": "tool",
            "tool_call_id": "c1",
            "content": "echo:hi",
        }
        assert second_call_messages[-2]["tool_calls"][0]["function"]["name"] == "echo"

    async def test_parallel_tool_calls(self, settings: Settings) -> None:
        provider = ScriptedProvider(
            [
                tool_response(("a", "echo", {"text": "1"}), ("b", "echo", {"text": "2"})),
                text_response("both"),
            ]
        )
        result = await make_agent(provider, settings).process_query("go")
        assert result.tools_used == ["echo", "echo"]

    async def test_usage_and_cost_accumulate(self, settings: Settings) -> None:
        provider = ScriptedProvider(
            [tool_response(("c1", "echo", {"text": "x"})), text_response("done", (30, 10))]
        )
        agent = make_agent(provider, settings)
        result = await agent.process_query("go")
        assert result.usage.input_tokens == 50 and result.usage.output_tokens == 18
        assert agent.total_usage == result.usage
        assert agent.total_cost_usd == pytest.approx(result.cost_usd)

    async def test_lifetime_totals_span_queries(self, settings: Settings) -> None:
        provider = ScriptedProvider([text_response("a", (10, 5)), text_response("b", (20, 10))])
        agent = make_agent(provider, settings)
        await agent.process_query("one")
        await agent.process_query("two")
        assert (agent.total_usage.input_tokens, agent.total_usage.output_tokens) == (30, 15)

    async def test_deep_research_route_gets_larger_budget(self, settings: Settings) -> None:
        provider = ScriptedProvider([text_response("informe")])
        result = await make_agent(provider, settings).process_query("Dame un informe")
        assert result.route == "deep_research"
        assert "report" in provider.calls[0]["messages"][0]["content"].lower()

    async def test_unknown_tool_is_reported_to_the_model(self, settings: Settings) -> None:
        provider = ScriptedProvider([tool_response(("c1", "nope", {})), text_response("sorry")])
        result = await make_agent(provider, settings).process_query("go")
        assert result.tool_failures == 1
        assert "unknown tool" in provider.calls[1]["messages"][-1]["content"]

    async def test_invalid_arguments_are_reported(self, settings: Settings) -> None:
        provider = ScriptedProvider([tool_response(("c1", "echo", None)), text_response("sorry")])
        result = await make_agent(provider, settings).process_query("go")
        assert result.tool_failures == 1
        assert "valid JSON" in provider.calls[1]["messages"][-1]["content"]

    async def test_tool_error_is_not_fatal(self, settings: Settings) -> None:
        class Failing(EchoTool):
            name = "echo"

            async def run(self, **kwargs: Any) -> str:
                raise ToolError("echo", "boom")

        provider = ScriptedProvider(
            [tool_response(("c1", "echo", {"text": "x"})), text_response("ok")]
        )
        result = await make_agent(provider, settings, [Failing()]).process_query("go")
        assert result.response == "ok" and result.tool_failures == 1
        assert provider.calls[1]["messages"][-1]["content"] == "Error: echo: boom"

    async def test_tool_timeout_is_not_fatal(self) -> None:
        class Slow(EchoTool):
            async def run(self, **kwargs: Any) -> str:
                await asyncio.sleep(5)
                return "late"

        cfg = Settings(llm_provider="mock", tool_timeout_seconds=0.05, _env_file=None)  # type: ignore[call-arg]
        provider = ScriptedProvider(
            [tool_response(("c1", "echo", {"text": "x"})), text_response("ok")]
        )
        result = await make_agent(provider, cfg, [Slow()]).process_query("go")
        assert result.tool_failures == 1
        assert "timed out" in provider.calls[1]["messages"][-1]["content"]

    async def test_long_tool_output_is_truncated(self, settings: Settings) -> None:
        class Big(EchoTool):
            async def run(self, **kwargs: Any) -> str:
                return "x" * 10_000

        provider = ScriptedProvider(
            [tool_response(("c1", "echo", {"text": "x"})), text_response("ok")]
        )
        await make_agent(provider, settings, [Big()]).process_query("go")
        content = provider.calls[1]["messages"][-1]["content"]
        assert len(content) < 4100 and content.endswith("[truncated]")

    async def test_iteration_cap_forces_final_answer_without_tools(self) -> None:
        cfg = Settings(llm_provider="mock", max_iterations=2, _env_file=None)  # type: ignore[call-arg]
        loop = tool_response(("c", "echo", {"text": "x"}))
        provider = ScriptedProvider([loop, loop, text_response("best effort")])
        result = await make_agent(provider, cfg).process_query("go")
        assert result.response == "best effort"
        assert provider.calls[-1]["tools"] is None

    async def test_iteration_cap_without_answer_raises(self) -> None:
        cfg = Settings(llm_provider="mock", max_iterations=1, _env_file=None)  # type: ignore[call-arg]
        loop = tool_response(("c", "echo", {"text": "x"}))
        provider = ScriptedProvider([loop, loop])
        with pytest.raises(MaxIterationsError):
            await make_agent(provider, cfg).process_query("go")

    async def test_empty_model_answer_raises(self, settings: Settings) -> None:
        provider = ScriptedProvider([LLMResponse(text="")])
        with pytest.raises(ProviderError, match="empty"):
            await make_agent(provider, settings).process_query("go")

    @pytest.mark.parametrize("text", ["", "   "])
    async def test_empty_query_is_rejected(self, settings: Settings, text: str) -> None:
        with pytest.raises(ValueError, match="empty"):
            await make_agent(ScriptedProvider([]), settings).process_query(text)

    async def test_provider_errors_propagate(self, settings: Settings) -> None:
        provider = ScriptedProvider([ProviderError("down")])
        with pytest.raises(ProviderError, match="down"):
            await make_agent(provider, settings).process_query("go")

    async def test_llm_timeout_is_mapped(self) -> None:
        class Hang(ScriptedProvider):
            async def complete(self, *a: Any, **k: Any) -> LLMResponse:
                await asyncio.sleep(10)
                return text_response("never")

        cfg = Settings(llm_provider="mock", llm_total_timeout_seconds=0.05, _env_file=None)  # type: ignore[call-arg]
        agent = AIAgent(Hang([]), [], cfg)
        with pytest.raises(ProviderTimeoutError):
            await agent.process_query("go")

    async def test_unknown_model_price_reports_zero_cost(self, settings: Settings) -> None:
        provider = ScriptedProvider([text_response("hola")])
        agent = make_agent(provider, settings, model="mystery-model")
        result = await agent.process_query("hola")
        assert result.cost_usd == 0.0

    async def test_properties(self, settings: Settings) -> None:
        agent = make_agent(ScriptedProvider([]), settings)
        assert agent.model == settings.chat_model
        assert agent.provider_name == "scripted"

    async def test_tracer_receives_trace_and_generation(self, settings: Settings) -> None:
        events: list[tuple[str, str]] = []

        class SpyTracer(Tracer):
            def trace(self, name: str, **kw: Any) -> Any:
                events.append(("trace", name))
                return super().trace(name, **kw)

            def generation(self, name: str, **kw: Any) -> Any:
                events.append(("generation", name))
                return super().generation(name, **kw)

        provider = ScriptedProvider([text_response("ok")])
        await make_agent(provider, settings, tracer=SpyTracer()).process_query("hola")
        assert events == [("trace", "process_query"), ("generation", "llm-call")]
