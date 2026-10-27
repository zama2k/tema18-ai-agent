"""Validate monitoring/cloudwatch/alarms.json against the course table and the real AWS API shape.

botocore validates the request parameters client side, so a Stubber is enough to prove that every
alarm is a well-formed ``PutMetricAlarm`` call without touching AWS.
"""

from __future__ import annotations

import json
from pathlib import Path
from typing import Any

import boto3
import pytest
from botocore.stub import Stubber

ALARMS_FILE = Path(__file__).resolve().parents[2] / "monitoring" / "cloudwatch" / "alarms.json"


@pytest.fixture(scope="module")
def alarms() -> list[dict[str, Any]]:
    document = json.loads(ALARMS_FILE.read_text(encoding="utf-8"))
    return list(document["Alarms"])


def metric_names(alarm: dict[str, Any]) -> set[str]:
    if "MetricName" in alarm:
        return {alarm["MetricName"]}
    return {m["MetricStat"]["Metric"]["MetricName"] for m in alarm["Metrics"] if "MetricStat" in m}


def by_metric(alarms: list[dict[str, Any]], name: str) -> dict[str, Any]:
    matches = [a for a in alarms if name in metric_names(a)]
    assert matches, f"no alarm covers {name}"
    return matches[0]


class TestAlarmsFile:
    def test_names_are_unique(self, alarms: list[dict[str, Any]]) -> None:
        names = [a["AlarmName"] for a in alarms]
        assert len(names) == len(set(names))

    def test_covers_the_five_lambda_metrics_of_the_course(
        self, alarms: list[dict[str, Any]]
    ) -> None:
        covered = set().union(*(metric_names(a) for a in alarms))
        assert {"Invocations", "Errors", "Duration", "Throttles", "ConcurrentExecutions"} <= covered

    def test_thresholds_match_the_course_table(self, alarms: list[dict[str, Any]]) -> None:
        assert by_metric(alarms, "Invocations")["Threshold"] == 10000
        assert by_metric(alarms, "Errors")["Threshold"] == 5  # percent
        duration = by_metric(alarms, "Duration")
        assert duration["Threshold"] == 10000  # milliseconds = 10 s
        assert duration["ExtendedStatistic"] == "p95"
        assert by_metric(alarms, "Throttles")["Threshold"] == 0
        assert by_metric(alarms, "ConcurrentExecutions")["Threshold"] == 800

    def test_error_alarm_uses_a_rate_not_a_raw_count(self, alarms: list[dict[str, Any]]) -> None:
        metrics = by_metric(alarms, "Errors")["Metrics"]
        expressions = [m["Expression"] for m in metrics if "Expression" in m]
        assert expressions and "invocations" in expressions[0]
        assert sum(1 for m in metrics if m.get("ReturnData")) == 1

    def test_every_alarm_notifies_and_handles_missing_data(
        self, alarms: list[dict[str, Any]]
    ) -> None:
        for alarm in alarms:
            assert alarm["AlarmActions"], alarm["AlarmName"]
            assert alarm["TreatMissingData"] == "notBreaching"
            assert alarm["DatapointsToAlarm"] <= alarm["EvaluationPeriods"]

    def test_every_alarm_targets_the_agent_function(self, alarms: list[dict[str, Any]]) -> None:
        for alarm in alarms:
            dimensions = alarm.get("Dimensions") or [
                d
                for m in alarm["Metrics"]
                if "MetricStat" in m
                for d in m["MetricStat"]["Metric"]["Dimensions"]
            ]
            assert {"Name": "FunctionName", "Value": "ai-agent"} in dimensions


class TestAwsApiShape:
    @pytest.fixture
    def client(self) -> Any:
        return boto3.client(
            "cloudwatch",
            region_name="us-east-1",
            aws_access_key_id="test",
            aws_secret_access_key="test",
        )

    def test_each_alarm_is_a_valid_put_metric_alarm_request(
        self, client: Any, alarms: list[dict[str, Any]]
    ) -> None:
        stubber = Stubber(client)
        for alarm in alarms:
            stubber.add_response("put_metric_alarm", {}, alarm)
        stubber.activate()
        for alarm in alarms:
            client.put_metric_alarm(**alarm)  # raises ParamValidationError if malformed
        stubber.assert_no_pending_responses()

    def test_a_malformed_alarm_is_rejected(self, client: Any, alarms: list[dict[str, Any]]) -> None:
        """Guards the guard: the validation above really rejects bad input."""
        from botocore.exceptions import ParamValidationError

        broken = {**alarms[0], "Period": "one hour"}
        with pytest.raises(ParamValidationError):
            client.put_metric_alarm(**broken)
