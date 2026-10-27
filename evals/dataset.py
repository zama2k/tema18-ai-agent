"""Golden dataset loading and deterministic rule checks (shared by pytest and the eval runner)."""

from __future__ import annotations

import json
from pathlib import Path
from typing import Any

DEFAULT_DATASET = Path(__file__).with_name("dataset.jsonl")
REQUIRED_FIELDS = ("id", "category", "question", "reference", "must_contain", "must_not_contain")


def load_dataset(path: Path | str = DEFAULT_DATASET) -> list[dict[str, Any]]:
    """Load and validate a JSONL golden dataset.

    Raises:
        ValueError: On malformed lines, missing fields or duplicate ids.
    """
    cases: list[dict[str, Any]] = []
    seen: set[str] = set()
    for number, line in enumerate(Path(path).read_text(encoding="utf-8").splitlines(), start=1):
        if not line.strip():
            continue
        try:
            case = json.loads(line)
        except json.JSONDecodeError as exc:
            raise ValueError(f"{path}:{number}: invalid JSON ({exc.msg})") from exc
        missing = [f for f in REQUIRED_FIELDS if f not in case]
        if missing:
            raise ValueError(f"{path}:{number}: missing fields {missing}")
        if case["id"] in seen:
            raise ValueError(f"{path}:{number}: duplicate id {case['id']!r}")
        seen.add(case["id"])
        cases.append(case)
    if not cases:
        raise ValueError(f"{path}: dataset is empty")
    return cases


def _matches(answer_lower: str, pattern: str) -> bool:
    """A pattern is a set of ``|`` separated alternatives; any alternative matches."""
    return any(option.strip().lower() in answer_lower for option in pattern.split("|") if option)


def check_rules(answer: str, case: dict[str, Any]) -> list[str]:
    """Return the list of violated deterministic rules (empty means the answer passes).

    ``must_contain`` entries are case-insensitive and accept ``a|b`` alternatives;
    ``must_not_contain`` entries are forbidden substrings; ``max_words`` caps the length.
    """
    lowered = answer.lower()
    problems: list[str] = []
    for pattern in case.get("must_contain", []):
        if not _matches(lowered, pattern):
            problems.append(f"missing expected content: {pattern!r}")
    for pattern in case.get("must_not_contain", []):
        if pattern.lower() in lowered:
            problems.append(f"contains forbidden content: {pattern!r}")
    max_words = case.get("max_words")
    if max_words is not None and len(answer.split()) > max_words:
        problems.append(f"too long: {len(answer.split())} words (max {max_words})")
    return problems
