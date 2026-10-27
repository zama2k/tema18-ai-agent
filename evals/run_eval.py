"""Run the golden dataset through the agent and judge every answer (quality gate).

Usage::

    ENV_FILE=path/to/env python -m evals.run_eval [--limit N] [--out evals/reports]

Exit codes: 0 gate passed, 1 gate failed (quality), 2 configuration or infrastructure error.
Thresholds come from ``EVAL_MEAN_THRESHOLD`` (default 0.75) and ``EVAL_MIN_THRESHOLD``
(default 0.5), or the matching command line flags.
"""

from __future__ import annotations

import argparse
import asyncio
import json
import logging
import math
import os
import sys
from dataclasses import asdict, dataclass, field
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

import openai

from evals.dataset import DEFAULT_DATASET, check_rules, load_dataset
from evals.judge import Judge, JudgeError, SelfTestResult, judge_selftest
from src.agent import AIAgent
from src.config import Settings, load_settings
from src.exceptions import AgentBaseException, ConfigurationError
from src.providers import create_provider
from src.tools import RAGTool

logger = logging.getLogger("evals")

DEFAULT_MEAN_THRESHOLD = 0.75
DEFAULT_MIN_THRESHOLD = 0.5


@dataclass
class CaseResult:
    """Everything measured for one dataset case."""

    id: str
    category: str
    question: str
    answer: str = ""
    error: str | None = None
    rule_problems: list[str] = field(default_factory=list)
    judge_score: float = 0.0
    verdict: dict[str, Any] = field(default_factory=dict)
    latency_seconds: float = 0.0
    agent_cost_usd: float = 0.0
    judge_cost_usd: float = 0.0
    input_tokens: int = 0
    output_tokens: int = 0


def percentile(values: list[float], pct: float) -> float:
    """Nearest-rank percentile (0 for an empty list)."""
    if not values:
        return 0.0
    ordered = sorted(values)
    rank = max(1, math.ceil(pct / 100 * len(ordered)))
    return ordered[rank - 1]


def summarize(
    results: list[CaseResult],
    mean_threshold: float,
    min_threshold: float,
    selftest: SelfTestResult | None,
) -> dict[str, Any]:
    """Aggregate case results and evaluate the quality gate (pure function)."""
    scores = [r.judge_score for r in results]
    latencies = [r.latency_seconds for r in results if r.error is None]
    mean = round(sum(scores) / len(scores), 4) if scores else 0.0
    lowest = min(scores) if scores else 0.0
    reasons: list[str] = []
    if selftest is not None and not selftest.passed:
        reasons.append(f"judge self-test failed: {selftest.detail}")
    if mean < mean_threshold:
        reasons.append(f"mean score {mean:.3f} < {mean_threshold}")
    if not results:
        reasons.append("no cases were evaluated")
    elif lowest < min_threshold:
        worst = min(results, key=lambda r: r.judge_score).id
        reasons.append(f"lowest case score {lowest:.3f} < {min_threshold} ({worst})")
    total_cost = sum(r.agent_cost_usd + r.judge_cost_usd for r in results)
    total_cost += selftest.cost_usd if selftest else 0.0
    return {
        "cases": len(results),
        "mean_score": mean,
        "min_score": round(lowest, 4),
        "errors": sum(1 for r in results if r.error),
        "rule_failures": sum(1 for r in results if r.rule_problems),
        "latency_p50_seconds": round(percentile(latencies, 50), 3),
        "latency_p95_seconds": round(percentile(latencies, 95), 3),
        "latency_max_seconds": round(max(latencies, default=0.0), 3),
        "total_cost_usd": round(total_cost, 6),
        "thresholds": {"mean": mean_threshold, "min": min_threshold},
        "gate_passed": not reasons,
        "gate_failures": reasons,
    }


def render_markdown(report: dict[str, Any]) -> str:
    """Render the report as Markdown (used for the GitHub step summary)."""
    s = report["summary"]
    verdict = "PASSED" if s["gate_passed"] else "FAILED"
    lines = [
        f"## LLM quality gate: {verdict}",
        "",
        f"- Chat model: `{report['chat_model']}` | judge: `{report['judge_model']}`",
        f"- Mean score: **{s['mean_score']:.3f}** (threshold {s['thresholds']['mean']}) | "
        f"lowest: {s['min_score']:.3f} (threshold {s['thresholds']['min']})",
        f"- Latency p50 / p95 / max: {s['latency_p50_seconds']}s / "
        f"{s['latency_p95_seconds']}s / {s['latency_max_seconds']}s",
        f"- Estimated cost: ${s['total_cost_usd']:.4f} | errors: {s['errors']} | "
        f"rule failures: {s['rule_failures']}",
    ]
    st = report.get("selftest")
    if st:
        status = "ok" if st["passed"] else "FAILED"
        lines.append(
            f"- Judge self-test: {status} (right answer {st['good_score']:.2f}, "
            f"deliberately wrong answer {st['bad_score']:.2f})"
        )
    for reason in s["gate_failures"]:
        lines.append(f"- **Gate failure:** {reason}")
    lines += [
        "",
        "| Case | Category | Score | Rules | Latency | Cost |",
        "|---|---|---|---|---|---|",
    ]
    for c in report["cases"]:
        rules = "ok" if not c["rule_problems"] else "; ".join(c["rule_problems"])
        status = f"error: {c['error']}" if c["error"] else f"{c['judge_score']:.2f}"
        lines.append(
            f"| {c['id']} | {c['category']} | {status} | {rules} | "
            f"{c['latency_seconds']:.1f}s | ${c['agent_cost_usd'] + c['judge_cost_usd']:.5f} |"
        )
    return "\n".join(lines) + "\n"


async def evaluate_case(agent: AIAgent, judge: Judge, case: dict[str, Any]) -> CaseResult:
    """Run one case: ask the agent, check the rules, then score with the judge."""
    result = CaseResult(id=case["id"], category=case["category"], question=case["question"])
    try:
        answer = await agent.process_query(case["question"])
    except (AgentBaseException, ValueError) as exc:
        result.error = f"agent failed: {type(exc).__name__}"
        return result
    result.answer = answer.response
    result.latency_seconds = answer.latency_seconds
    result.agent_cost_usd = answer.cost_usd
    result.input_tokens = answer.usage.input_tokens
    result.output_tokens = answer.usage.output_tokens
    result.rule_problems = check_rules(answer.response, case)
    return result


async def judge_case(judge: Judge, case: dict[str, Any], result: CaseResult) -> None:
    """Score an already answered case in place."""
    if result.error:
        return
    try:
        judged = await judge.judge(case["question"], case["reference"], result.answer)
    except JudgeError as exc:
        result.error = str(exc)
        return
    result.judge_score = judged.score
    result.verdict = judged.verdict.model_dump()
    result.judge_cost_usd = judged.cost_usd


async def run_evaluation(
    settings: Settings,
    cases: list[dict[str, Any]],
    mean_threshold: float,
    min_threshold: float,
    run_selftest: bool = True,
    judge_concurrency: int = 4,
) -> dict[str, Any]:
    """Run the full evaluation and return the report dictionary."""
    if not (settings.azure_openai_endpoint and settings.azure_openai_api_key):
        raise ConfigurationError("the judge needs AZURE_OPENAI_ENDPOINT and AZURE_OPENAI_API_KEY")
    judge_client = openai.AsyncOpenAI(
        base_url=settings.azure_openai_endpoint,
        api_key=settings.azure_openai_api_key.get_secret_value(),
        timeout=120,
        max_retries=2,
    )
    judge = Judge(judge_client, settings.judge_model, settings.reasoning_headroom_tokens + 2000)
    # Deterministic tools only: the local RAG corpus. No web search in an evaluation.
    agent = AIAgent(create_provider(settings), [RAGTool()], settings)

    selftest: SelfTestResult | None = None
    if run_selftest:
        selftest = await judge_selftest(judge, mean_threshold)
        logger.info("judge self-test: %s", selftest.detail)

    # Agent calls run one by one so the measured latency is not inflated by contention.
    results = [await evaluate_case(agent, judge, case) for case in cases]
    semaphore = asyncio.Semaphore(judge_concurrency)

    async def guarded(case: dict[str, Any], res: CaseResult) -> None:
        async with semaphore:
            await judge_case(judge, case, res)

    await asyncio.gather(*(guarded(c, r) for c, r in zip(cases, results, strict=True)))

    return {
        "generated_at": datetime.now(UTC).isoformat(timespec="seconds"),
        "chat_model": settings.chat_model,
        "judge_model": settings.judge_model,
        "git_sha": settings.git_sha,
        "summary": summarize(results, mean_threshold, min_threshold, selftest),
        "selftest": asdict(selftest) if selftest else None,
        "cases": [asdict(r) for r in results],
    }


def write_reports(report: dict[str, Any], out_dir: Path) -> tuple[Path, Path]:
    """Write JSON, Markdown and the small ``latest.json`` read by the Prometheus gauge."""
    out_dir.mkdir(parents=True, exist_ok=True)
    json_path = out_dir / "eval-report.json"
    md_path = out_dir / "eval-report.md"
    json_path.write_text(json.dumps(report, ensure_ascii=False, indent=2), encoding="utf-8")
    md_path.write_text(render_markdown(report), encoding="utf-8")
    latest = {
        "mean_score": report["summary"]["mean_score"],
        "generated_at": report["generated_at"],
        "gate_passed": report["summary"]["gate_passed"],
    }
    (out_dir / "latest.json").write_text(json.dumps(latest, indent=2), encoding="utf-8")
    return json_path, md_path


def _threshold(flag: float | None, env_name: str, default: float) -> float:
    if flag is not None:
        return flag
    return float(os.environ.get(env_name, default))


def main(argv: list[str] | None = None) -> int:
    """CLI entry point."""
    parser = argparse.ArgumentParser(description="LLM-as-a-judge quality gate")
    parser.add_argument("--dataset", type=Path, default=DEFAULT_DATASET)
    parser.add_argument("--out", type=Path, default=Path("evals/reports"))
    parser.add_argument("--limit", type=int, default=None, help="only the first N cases")
    parser.add_argument("--mean-threshold", type=float, default=None)
    parser.add_argument("--min-threshold", type=float, default=None)
    parser.add_argument("--skip-selftest", action="store_true")
    args = parser.parse_args(argv)

    logging.basicConfig(level=logging.INFO, format="%(levelname)s %(name)s: %(message)s")
    try:
        settings = load_settings()
        cases = load_dataset(args.dataset)
        if args.limit:
            cases = cases[: args.limit]
        report = asyncio.run(
            run_evaluation(
                settings,
                cases,
                _threshold(args.mean_threshold, "EVAL_MEAN_THRESHOLD", DEFAULT_MEAN_THRESHOLD),
                _threshold(args.min_threshold, "EVAL_MIN_THRESHOLD", DEFAULT_MIN_THRESHOLD),
                run_selftest=not args.skip_selftest,
            )
        )
    except (ConfigurationError, ValueError, JudgeError, AgentBaseException, OSError) as exc:
        print(f"evaluation could not run: {exc}", file=sys.stderr)
        return 2

    json_path, md_path = write_reports(report, args.out)
    print(render_markdown(report))
    print(f"reports written to {json_path} and {md_path}")
    return 0 if report["summary"]["gate_passed"] else 1


if __name__ == "__main__":
    raise SystemExit(main())
