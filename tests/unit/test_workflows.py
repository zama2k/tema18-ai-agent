"""Structural checks of the workflows and the integration suite (cheap regression guards)."""

from __future__ import annotations

import re
from pathlib import Path
from typing import Any

import pytest
import yaml

ROOT = Path(__file__).resolve().parents[2]
WORKFLOWS = ROOT / ".github" / "workflows"
SHA_PINNED = re.compile(r"^[\w.-]+/[\w./-]+@[0-9a-f]{40}$")


def load(name: str) -> dict[str, Any]:
    return yaml.safe_load((WORKFLOWS / name).read_text(encoding="utf-8"))


def steps(workflow: dict[str, Any], job: str) -> list[dict[str, Any]]:
    return list(workflow["jobs"][job]["steps"])


class TestAllWorkflows:
    @pytest.mark.parametrize("name", ["ci.yml", "cd.yml", "llm-eval-nightly.yml"])
    def test_least_privilege_timeouts_and_pinned_actions(self, name: str) -> None:
        workflow = load(name)
        assert workflow["permissions"] == {"contents": "read"}
        for job_name, job in workflow["jobs"].items():
            assert "timeout-minutes" in job, f"{name}:{job_name} has no timeout"
            for step in job["steps"]:
                if "uses" in step:
                    assert SHA_PINNED.match(step["uses"]), f"{name}:{job_name} {step['uses']}"


class TestCi:
    def test_eval_step_does_not_repeat_the_judge_self_test(self) -> None:
        """The pytest step already ran (and paid for) the meta-test."""
        job = steps(load("ci.yml"), "llm-quality-tests")
        eval_step = next(s for s in job if "evals.run_eval" in s.get("run", ""))
        assert "--skip-selftest" in eval_step["run"]
        pytest_step = next(s for s in job if "test_llm_quality.py" in s.get("run", ""))
        assert "--timeout=60" in pytest_step["run"] and "-x" in pytest_step["run"]

    def test_nightly_still_runs_the_self_test(self) -> None:
        job = steps(load("llm-eval-nightly.yml"), "evaluate")
        eval_step = next(s for s in job if "evals.run_eval" in s.get("run", ""))
        assert "--skip-selftest" not in eval_step["run"]

    def test_meta_test_has_its_own_longer_timeout_and_single_retry(self) -> None:
        source = (ROOT / "tests/integration/test_llm_quality.py").read_text(encoding="utf-8")
        assert "@pytest.mark.timeout(180)" in source
        assert "max_retries=1" in source

    def test_llm_jobs_run_only_on_push_to_main(self) -> None:
        ci = load("ci.yml")
        for job in ("integration-tests", "llm-quality-tests"):
            condition = ci["jobs"][job]["if"]
            assert "push" in condition and "refs/heads/main" in condition


class TestCd:
    def test_deployments_never_cancel_a_running_job(self) -> None:
        cd = load("cd.yml")
        for job in ("deploy-staging", "deploy-production"):
            concurrency = cd["jobs"][job]["concurrency"]
            assert concurrency["cancel-in-progress"] is False

    def test_concurrency_semantics_are_documented(self) -> None:
        """One running + one pending per group; a third arrival replaces the pending one."""
        text = (WORKFLOWS / "cd.yml").read_text(encoding="utf-8")
        assert "pending" in text and "most recent" in text
        decisions = (ROOT / "docs/decisiones.md").read_text(encoding="utf-8")
        assert "pendiente" in decisions and "más reciente" in decisions

    def test_environment_url_is_never_empty(self) -> None:
        """GitHub does not define an empty environment url: fall back to the run page."""
        cd = load("cd.yml")
        for job in ("deploy-staging", "deploy-production"):
            url = cd["jobs"][job]["environment"]["url"]
            assert "||" in url and "github.run_id" in url, url

    def test_checkouts_use_the_commit_that_passed_ci(self) -> None:
        cd = load("cd.yml")
        for job in ("failure-drill", "deploy-staging", "deploy-production"):
            checkout = next(s for s in steps(cd, job) if "checkout" in s.get("uses", ""))
            assert "needs.prepare.outputs.sha" in checkout["with"]["ref"]
        assert cd["env"]["RELEASE_SHA"] == "${{ github.event.workflow_run.head_sha }}"
