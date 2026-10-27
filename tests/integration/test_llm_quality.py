"""LLM quality tests against the real model: language, accuracy, concision, latency, regression.

Run with ``pytest tests/integration/test_llm_quality.py -m integration -x --timeout=60``.
Prompts are deliberately short to keep CI cost low.
"""

from __future__ import annotations

from typing import Any

import openai
import pytest

from evals.dataset import check_rules, load_dataset
from evals.judge import Judge, judge_selftest
from src.agent import AIAgent
from src.config import Settings
from src.providers import AzureOpenAIProvider
from src.tools import RAGTool

pytestmark = pytest.mark.integration

SPANISH_STOPWORDS = {"el", "la", "los", "las", "de", "que", "en", "es", "un", "una", "y", "con"}

# Data-driven regression suite: edit evals/dataset.jsonl to add cases.
EVAL_DATASET: list[dict[str, Any]] = load_dataset()

MAX_SIMPLE_SECONDS = 10.0
MAX_COMPLEX_SECONDS = 30.0


class TestLLMQuality:
    """Properties every answer of the production prompt must satisfy."""

    async def test_responds_in_spanish(self, ask) -> None:  # type: ignore[no-untyped-def]
        from src.agent import build_system_prompt

        answer = await ask(
            "Explain what an API is in one sentence.", system=build_system_prompt(), max_tokens=120
        )
        words = set(answer.text.lower().replace(",", " ").replace(".", " ").split())
        assert len(words & SPANISH_STOPWORDS) >= 2, f"answer does not look Spanish: {answer.text!r}"

    async def test_factual_accuracy(self, ask) -> None:  # type: ignore[no-untyped-def]
        answer = await ask("What is the capital of France? Answer in one word.", max_tokens=20)
        assert "paris" in answer.text.lower().replace("í", "i"), f"wrong answer: {answer.text!r}"

    async def test_follows_length_constraints(self, ask) -> None:  # type: ignore[no-untyped-def]
        answer = await ask("Is the Earth round? Answer only with YES or NO.", max_tokens=10)
        cleaned = answer.text.strip().strip(".!").upper()
        assert cleaned in {"YES", "NO", "SÍ", "SI"}, f"not concise: {answer.text!r}"
        assert answer.output_tokens <= 5, f"too many output tokens: {answer.output_tokens}"


class TestLatency:
    """Round-trip time budgets, measured with the real endpoint."""

    async def test_simple_query_latency(self, ask) -> None:  # type: ignore[no-untyped-def]
        answer = await ask("Say hello in one word.", max_tokens=10)
        assert answer.elapsed < MAX_SIMPLE_SECONDS, f"Latency too high: {answer.elapsed:.2f}s"

    @pytest.mark.slow
    async def test_complex_comparative_query_latency(self, ask) -> None:  # type: ignore[no-untyped-def]
        answer = await ask(
            "Compare blue/green and rolling deployments in four short bullet points.",
            max_tokens=300,
        )
        assert answer.elapsed < MAX_COMPLEX_SECONDS, f"Latency too high: {answer.elapsed:.2f}s"
        assert answer.text


class TestRegression:
    """Golden dataset: deterministic must_contain / must_not_contain checks."""

    @pytest.mark.parametrize("case", EVAL_DATASET, ids=[c["id"] for c in EVAL_DATASET])
    async def test_dataset_case(self, case: dict[str, Any], live_settings: Settings) -> None:
        agent = AIAgent(
            AzureOpenAIProvider.from_settings(live_settings), [RAGTool()], live_settings
        )
        result = await agent.process_query(case["question"])
        problems = check_rules(result.response, case)
        assert not problems, f"{case['id']}: {problems}\nanswer: {result.response!r}"


class TestJudgeMetaTest:
    """The judge itself must be trustworthy before its scores are used as a gate."""

    # Two sequential calls to a reasoning model: the global --timeout=60 is too tight for them.
    @pytest.mark.timeout(180)
    async def test_wrong_answer_scores_below_the_threshold(self, live_settings: Settings) -> None:
        assert live_settings.azure_openai_endpoint and live_settings.azure_openai_api_key
        client = openai.AsyncOpenAI(
            base_url=live_settings.azure_openai_endpoint,
            api_key=live_settings.azure_openai_api_key.get_secret_value(),
            timeout=40,
            max_retries=1,
        )
        judge = Judge(client, live_settings.judge_model, live_settings.reasoning_headroom_tokens)
        result = await judge_selftest(judge, threshold=0.75)
        assert result.passed, (
            f"judge is not reliable: {result.detail} "
            f"(good={result.good_score:.2f}, bad={result.bad_score:.2f})"
        )
