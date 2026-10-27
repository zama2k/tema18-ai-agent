"""Unit tests for the evaluation tooling: dataset, judge, meta-test, gate and reports."""

from __future__ import annotations

import json
from pathlib import Path
from types import SimpleNamespace
from typing import Any

import httpx2
import openai
import pytest

from evals import run_eval
from evals.dataset import DEFAULT_DATASET, check_rules, load_dataset
from evals.judge import (
    VERDICT_SCHEMA,
    Judge,
    JudgedAnswer,
    JudgeError,
    JudgeVerdict,
    judge_selftest,
)
from evals.run_eval import (
    CaseResult,
    evaluate_case,
    judge_case,
    main,
    percentile,
    render_markdown,
    summarize,
    write_reports,
)
from src.agent import AIAgent
from src.config import Settings
from src.exceptions import ConfigurationError
from tests.unit.conftest import ScriptedProvider, text_response


class TestDataset:
    def test_shipped_dataset_is_valid(self) -> None:
        cases = load_dataset(DEFAULT_DATASET)
        assert len(cases) >= 10
        assert len({c["id"] for c in cases}) == len(cases)
        assert {"factual", "safety", "code", "conciseness"} <= {c["category"] for c in cases}

    def test_rejects_invalid_json(self, tmp_path: Path) -> None:
        path = tmp_path / "d.jsonl"
        path.write_text("{oops", encoding="utf-8")
        with pytest.raises(ValueError, match="invalid JSON"):
            load_dataset(path)

    def test_rejects_missing_fields_duplicates_and_empty(self, tmp_path: Path) -> None:
        path = tmp_path / "d.jsonl"
        path.write_text('{"id": "a"}\n', encoding="utf-8")
        with pytest.raises(ValueError, match="missing fields"):
            load_dataset(path)
        good = {
            "id": "a",
            "category": "c",
            "question": "q",
            "reference": "r",
            "must_contain": [],
            "must_not_contain": [],
        }
        path.write_text(json.dumps(good) + "\n\n" + json.dumps(good) + "\n", encoding="utf-8")
        with pytest.raises(ValueError, match="duplicate"):
            load_dataset(path)
        path.write_text("\n", encoding="utf-8")
        with pytest.raises(ValueError, match="empty"):
            load_dataset(path)


class TestRules:
    def test_alternatives_and_case_insensitivity(self) -> None:
        case = {"must_contain": ["París|Paris"], "must_not_contain": []}
        assert check_rules("la capital es PARIS", case) == []
        assert check_rules("Madrid", case)

    def test_forbidden_content(self) -> None:
        case = {"must_contain": [], "must_not_contain": ["No lo sé"]}
        assert check_rules("pues no lo sé", case) == ["contains forbidden content: 'No lo sé'"]

    def test_max_words(self) -> None:
        case = {"max_words": 2}
        assert check_rules("SÍ", case) == []
        assert "too long" in check_rules("sí, es primo", case)[0]


def verdict_json(**overrides: float | str) -> str:
    base: dict[str, float | str] = {
        "correctness": 1.0,
        "completeness": 1.0,
        "language": 1.0,
        "safety": 1.0,
        "reasoning": "ok",
    }
    base.update(overrides)
    return json.dumps(base)


class FakeCompletions:
    def __init__(self, outcomes: list[str | Exception]) -> None:
        self.outcomes = list(outcomes)
        self.kwargs: list[dict[str, Any]] = []

    async def create(self, **kwargs: Any) -> Any:
        self.kwargs.append(kwargs)
        outcome = self.outcomes.pop(0)
        if isinstance(outcome, Exception):
            raise outcome
        return SimpleNamespace(
            choices=[SimpleNamespace(message=SimpleNamespace(content=outcome))],
            usage=SimpleNamespace(prompt_tokens=100, completion_tokens=50),
        )


def fake_judge(outcomes: list[str | Exception]) -> tuple[Judge, FakeCompletions]:
    completions = FakeCompletions(outcomes)
    client = SimpleNamespace(chat=SimpleNamespace(completions=completions))
    return Judge(client, "gpt-5.6-sol", 5000), completions  # type: ignore[arg-type]


class TestJudge:
    def test_weighted_score(self) -> None:
        verdict = JudgeVerdict(
            correctness=1.0, completeness=0.5, language=1.0, safety=0.0, reasoning="x"
        )
        assert verdict.score == pytest.approx(0.5 + 0.1 + 0.15)

    def test_verdict_bounds_are_validated(self) -> None:
        with pytest.raises(ValueError):
            JudgeVerdict(correctness=1.5, completeness=0, language=0, safety=0, reasoning="")

    def test_schema_is_strict_compatible(self) -> None:
        assert VERDICT_SCHEMA["additionalProperties"] is False
        assert set(VERDICT_SCHEMA["required"]) == set(VERDICT_SCHEMA["properties"])

    async def test_judge_sends_strict_schema_and_reasoning_budget(self) -> None:
        judge, completions = fake_judge([verdict_json()])
        judged = await judge.judge("q", "ref", "ans")
        kwargs = completions.kwargs[0]
        assert kwargs["model"] == "gpt-5.6-sol"
        assert kwargs["response_format"]["json_schema"]["strict"] is True
        assert "max_completion_tokens" in kwargs
        for forbidden in ("temperature", "top_p", "max_tokens", "stop"):
            assert forbidden not in kwargs
        assert "ans" in kwargs["messages"][1]["content"]
        assert judged.score == 1.0 and judged.cost_usd > 0
        assert (judged.input_tokens, judged.output_tokens) == (100, 50)

    async def test_retries_once_on_invalid_output(self) -> None:
        judge, completions = fake_judge(["not json", verdict_json(correctness=0.5)])
        judged = await judge.judge("q", "r", "a")
        assert len(completions.kwargs) == 2
        assert judged.verdict.correctness == 0.5

    async def test_gives_up_after_two_invalid_outputs(self) -> None:
        judge, _ = fake_judge(["{}", verdict_json(correctness=7)])
        with pytest.raises(JudgeError, match="invalid after 2"):
            await judge.judge("q", "r", "a")

    async def test_api_errors_become_judge_errors(self) -> None:
        request = httpx2.Request("POST", "https://x.test")
        error = openai.BadRequestError(
            "blocked", response=httpx2.Response(400, request=request), body=None
        )
        judge, _ = fake_judge([error])
        with pytest.raises(JudgeError, match="BadRequestError"):
            await judge.judge("q", "r", "a")

    async def test_unknown_model_cost_is_zero(self) -> None:
        completions = FakeCompletions([verdict_json()])
        client = SimpleNamespace(chat=SimpleNamespace(completions=completions))
        judged = await Judge(client, "mystery", 100).judge("q", "r", "a")  # type: ignore[arg-type]
        assert judged.cost_usd == 0.0


class TestJudgeSelfTest:
    """The meta-test: a deliberately wrong answer MUST score below the threshold."""

    async def test_passes_when_judge_separates_good_from_bad(self) -> None:
        judge, completions = fake_judge(
            [verdict_json(), verdict_json(correctness=0.0, completeness=0.3, safety=0.8)]
        )
        result = await judge_selftest(judge, 0.75)
        assert result.passed and result.good_score == 1.0 and result.bad_score < 0.75
        bad_prompt = completions.kwargs[1]["messages"][1]["content"]
        assert "Madrid" in bad_prompt  # the wrong answer really is the one being judged

    async def test_fails_for_an_indulgent_judge(self) -> None:
        judge, _ = fake_judge([verdict_json(), verdict_json()])
        result = await judge_selftest(judge, 0.75)
        assert not result.passed
        assert "wrong answer scored" in result.detail

    async def test_fails_for_a_harsh_judge(self) -> None:
        harsh = verdict_json(correctness=0.2, completeness=0.2, language=0.2, safety=0.2)
        judge, _ = fake_judge([harsh, harsh])
        result = await judge_selftest(judge, 0.75)
        assert not result.passed and "correct answer scored" in result.detail

    async def test_flags_wrong_answer_with_high_correctness_even_if_score_is_low(self) -> None:
        odd = verdict_json(correctness=0.5, completeness=0.0, language=0.0, safety=0.0)
        judge, _ = fake_judge([verdict_json(), odd])
        result = await judge_selftest(judge, 0.75)
        assert not result.passed and "correctness" in result.detail


def make_result(id_: str, score: float, latency: float = 1.0, **kwargs: Any) -> CaseResult:
    return CaseResult(
        id=id_,
        category="c",
        question="q",
        answer="a",
        judge_score=score,
        latency_seconds=latency,
        agent_cost_usd=0.001,
        judge_cost_usd=0.002,
        **kwargs,
    )


class TestGate:
    def test_percentile(self) -> None:
        assert percentile([], 95) == 0.0
        assert percentile([1, 2, 3, 4], 50) == 2
        assert percentile([1, 2, 3, 4], 95) == 4

    def test_passes_above_thresholds(self) -> None:
        summary = summarize([make_result("a", 0.9), make_result("b", 0.8)], 0.75, 0.5, None)
        assert summary["gate_passed"] and summary["mean_score"] == 0.85
        assert summary["total_cost_usd"] == pytest.approx(0.006)

    def test_fails_on_low_mean(self) -> None:
        summary = summarize([make_result("a", 0.6), make_result("b", 0.7)], 0.75, 0.5, None)
        assert not summary["gate_passed"] and "mean score" in summary["gate_failures"][0]

    def test_fails_on_a_single_terrible_case(self) -> None:
        results = [make_result("a", 1.0), make_result("b", 1.0), make_result("bad", 0.2)]
        summary = summarize(results, 0.5, 0.5, None)
        assert not summary["gate_passed"]
        assert "bad" in summary["gate_failures"][0]

    def test_fails_when_selftest_failed(self) -> None:
        from evals.judge import SelfTestResult

        selftest = SelfTestResult(False, 1.0, 0.9, 0.01, "judge is indulgent")
        summary = summarize([make_result("a", 1.0)], 0.75, 0.5, selftest)
        assert not summary["gate_passed"]
        assert "self-test" in summary["gate_failures"][0]
        assert summary["total_cost_usd"] == pytest.approx(0.013)

    def test_errors_count_as_zero(self) -> None:
        errored = CaseResult(id="e", category="c", question="q", error="agent failed")
        summary = summarize([make_result("a", 1.0), errored], 0.75, 0.5, None)
        assert summary["errors"] == 1 and not summary["gate_passed"]

    def test_empty_results(self) -> None:
        summary = summarize([], 0.75, 0.5, None)
        assert summary["mean_score"] == 0.0 and not summary["gate_passed"]
        assert "no cases" in " ".join(summary["gate_failures"])


class TestCaseExecution:
    CASE = {
        "id": "geo",
        "category": "factual",
        "question": "capital?",
        "reference": "París",
        "must_contain": ["París"],
        "must_not_contain": [],
    }

    async def test_case_is_answered_checked_and_judged(self, settings: Settings) -> None:
        agent = AIAgent(ScriptedProvider([text_response("París", (10, 5))]), [], settings)
        judge, _ = fake_judge([verdict_json()])
        result = await evaluate_case(agent, judge, self.CASE)
        await judge_case(judge, self.CASE, result)
        assert result.answer == "París" and result.rule_problems == []
        assert result.judge_score == 1.0 and result.verdict["reasoning"] == "ok"
        assert result.input_tokens == 10 and result.judge_cost_usd > 0

    async def test_rule_failures_are_recorded(self, settings: Settings) -> None:
        agent = AIAgent(ScriptedProvider([text_response("Madrid")]), [], settings)
        judge, _ = fake_judge([])
        result = await evaluate_case(agent, judge, self.CASE)
        assert result.rule_problems

    async def test_agent_failure_is_captured_not_raised(self, settings: Settings) -> None:
        agent = AIAgent(ScriptedProvider([]), [], settings)
        judge, _ = fake_judge([])
        result = await evaluate_case(agent, judge, self.CASE)
        assert result.error == "agent failed: ProviderError"
        await judge_case(judge, self.CASE, result)  # skipped, must not call the judge
        assert result.judge_score == 0.0

    async def test_judge_failure_is_captured(self, settings: Settings) -> None:
        agent = AIAgent(ScriptedProvider([text_response("París")]), [], settings)
        judge, _ = fake_judge(["{}", "{}"])
        result = await evaluate_case(agent, judge, self.CASE)
        await judge_case(judge, self.CASE, result)
        assert result.error and "judge" in result.error


class TestReports:
    def _report(self) -> dict[str, Any]:
        results = [make_result("a", 0.9), make_result("b", 0.4, rule_problems=["missing"])]
        return {
            "generated_at": "2026-10-27T10:00:00+00:00",
            "chat_model": "gpt-4o-mini",
            "judge_model": "gpt-5.6-sol",
            "git_sha": "abc",
            "summary": summarize(results, 0.75, 0.5, None),
            "selftest": {"passed": True, "good_score": 1.0, "bad_score": 0.4},
            "cases": [r.__dict__ for r in results],
        }

    def test_markdown_contains_verdict_table_and_selftest(self) -> None:
        text = render_markdown(self._report())
        assert "quality gate: FAILED" in text
        assert "| a | c | 0.90 | ok |" in text
        assert "missing" in text and "Judge self-test: ok" in text
        assert "Gate failure" in text

    def test_markdown_shows_errors(self) -> None:
        report = self._report()
        report["cases"][0]["error"] = "agent failed: ProviderError"
        assert "error: agent failed" in render_markdown(report)

    def test_write_reports_creates_three_files(self, tmp_path: Path) -> None:
        json_path, md_path = write_reports(self._report(), tmp_path / "out")
        assert json.loads(json_path.read_text(encoding="utf-8"))["chat_model"] == "gpt-4o-mini"
        assert md_path.read_text(encoding="utf-8").startswith("## LLM quality gate")
        latest = json.loads((tmp_path / "out" / "latest.json").read_text(encoding="utf-8"))
        assert latest["mean_score"] == 0.65 and latest["gate_passed"] is False


class TestCli:
    def test_configuration_errors_exit_with_2(
        self, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
    ) -> None:
        monkeypatch.setenv("LLM_PROVIDER", "azure")
        assert main(["--out", "unused"]) == 2
        assert "could not run" in capsys.readouterr().err

    def test_bad_dataset_exits_with_2(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        monkeypatch.setenv("LLM_PROVIDER", "mock")
        missing = tmp_path / "missing.jsonl"
        assert main(["--dataset", str(missing)]) == 2

    @pytest.mark.parametrize(("passed", "code"), [(True, 0), (False, 1)])
    def test_exit_code_follows_the_gate(
        self,
        passed: bool,
        code: int,
        tmp_path: Path,
        monkeypatch: pytest.MonkeyPatch,
        capsys: pytest.CaptureFixture[str],
    ) -> None:
        monkeypatch.setenv("LLM_PROVIDER", "mock")
        monkeypatch.setenv("EVAL_MEAN_THRESHOLD", "0.9")
        seen: dict[str, Any] = {}

        async def fake_run(
            settings: Settings, cases: list[dict[str, Any]], mean: float, low: float, **kw: Any
        ) -> dict[str, Any]:
            seen.update(cases=len(cases), mean=mean, low=low, selftest=kw["run_selftest"])
            results = [make_result("a", 1.0 if passed else 0.1)]
            return {
                "generated_at": "now",
                "chat_model": "m",
                "judge_model": "j",
                "git_sha": "x",
                "summary": summarize(results, mean, low, None),
                "selftest": None,
                "cases": [r.__dict__ for r in results],
            }

        monkeypatch.setattr(run_eval, "run_evaluation", fake_run)
        exit_code = main(
            ["--out", str(tmp_path), "--limit", "3", "--min-threshold", "0.4", "--skip-selftest"]
        )
        assert exit_code == code
        assert seen == {"cases": 3, "mean": 0.9, "low": 0.4, "selftest": False}
        assert "reports written" in capsys.readouterr().out
        assert (tmp_path / "latest.json").exists()

    async def test_run_evaluation_requires_azure_credentials(self, settings: Settings) -> None:
        with pytest.raises(ConfigurationError):
            await run_eval.run_evaluation(settings, [], 0.75, 0.5)

    async def test_run_evaluation_end_to_end_with_fakes(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        settings = Settings(  # type: ignore[call-arg]
            llm_provider="mock",
            azure_openai_endpoint="https://example.test/openai/v1",
            azure_openai_api_key="k",
            _env_file=None,
        )
        outcomes = [verdict_json(), verdict_json(correctness=0.0, completeness=0.0)]
        outcomes += [verdict_json()] * 2
        completions = FakeCompletions(outcomes)
        fake_client = SimpleNamespace(chat=SimpleNamespace(completions=completions))
        monkeypatch.setattr(run_eval.openai, "AsyncOpenAI", lambda **_: fake_client)
        cases = [
            {
                "id": "france",
                "category": "factual",
                "question": "¿Cuál es la capital de Francia?",
                "reference": "París",
                "must_contain": ["París"],
                "must_not_contain": [],
            },
            {
                "id": "yes-no",
                "category": "conciseness",
                "question": "Responde solo sí o no: ¿2+2=4?",
                "reference": "SÍ",
                "must_contain": ["sí"],
                "must_not_contain": [],
                "max_words": 2,
            },
        ]
        report = await run_eval.run_evaluation(settings, cases, 0.75, 0.5)
        assert report["summary"]["gate_passed"] is True
        assert report["selftest"]["passed"] is True
        assert [c["id"] for c in report["cases"]] == ["france", "yes-no"]
        assert all(c["rule_problems"] == [] for c in report["cases"])


def test_judged_answer_exposes_score() -> None:
    verdict = JudgeVerdict(correctness=1, completeness=1, language=1, safety=1, reasoning="r")
    assert JudgedAnswer(verdict, 1, 1, 0.0).score == 1.0
