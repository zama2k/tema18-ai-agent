"""Unit tests for logging, metrics, LangFuse (mocked) and Sentry (mocked)."""

from __future__ import annotations

import json
import logging
import math
import socket
from pathlib import Path
from typing import Any
from unittest.mock import MagicMock

import pytest
from pytest_socket import SocketConnectBlockedError

from src.config import Settings
from src.models import TokenUsage
from src.observability import (
    JsonFormatter,
    LangfuseTracer,
    Metrics,
    Tracer,
    configure_logging,
    create_tracer,
    init_sentry,
    request_id_var,
    scrub_event,
)


def make_settings(**kwargs: Any) -> Settings:
    return Settings(llm_provider="mock", _env_file=None, **kwargs)  # type: ignore[call-arg]


@pytest.mark.filterwarnings("ignore::UserWarning")
def test_unit_tests_cannot_reach_the_internet() -> None:
    """Safety net for the whole unit suite (conftest blocks non-loopback connections)."""
    with pytest.raises(SocketConnectBlockedError):
        socket.create_connection(("93.184.216.34", 80), timeout=1)


class TestJsonLogging:
    def _record(self, **extra: Any) -> logging.LogRecord:
        record = logging.LogRecord("t", logging.INFO, __file__, 1, "hello %s", ("world",), None)
        for key, value in extra.items():
            setattr(record, key, value)
        return record

    def test_formats_one_json_object_with_required_fields(self) -> None:
        token = request_id_var.set("req-123")
        try:
            data = json.loads(JsonFormatter(version="1.2.3").format(self._record(status=200)))
        finally:
            request_id_var.reset(token)
        assert data["message"] == "hello world"
        assert data["level"] == "INFO"
        assert data["request_id"] == "req-123"
        assert data["version"] == "1.2.3"
        assert data["status"] == 200
        assert data["timestamp"].endswith("+00:00")

    def test_exceptions_are_structured(self) -> None:
        try:
            raise ValueError("bad")
        except ValueError:
            import sys

            record = logging.LogRecord("t", logging.ERROR, __file__, 1, "x", (), sys.exc_info())
        data = json.loads(JsonFormatter().format(record))
        assert data["error"]["type"] == "ValueError"
        assert data["error"]["message"] == "bad"
        assert "Traceback" in data["error"]["stack"]

    def test_configure_logging_is_idempotent(self) -> None:
        root = logging.getLogger()
        before = list(root.handlers)
        try:
            configure_logging("INFO")
            configure_logging("DEBUG")
            ours = [h for h in root.handlers if getattr(h, "_ai_agent_json", False)]
            assert len(ours) == 1 and root.level == logging.DEBUG
        finally:
            root.handlers = before
            root.setLevel(logging.WARNING)


class TestMetrics:
    def test_record_result_updates_counters(self) -> None:
        metrics = Metrics()
        metrics.record_result("chat_with_tools", TokenUsage(100, 50), 0.002)
        text = metrics.render().decode()
        assert 'agent_requests_total{agent_route="chat_with_tools",outcome="success"} 1.0' in text
        assert 'agent_tokens_total{direction="input"} 100.0' in text
        assert 'agent_tokens_total{direction="output"} 50.0' in text
        assert "agent_cost_usd_total 0.002" in text

    def test_record_error(self) -> None:
        metrics = Metrics()
        metrics.record_error("deep_research", "timeout")
        text = metrics.render().decode()
        assert 'agent_errors_total{error_type="timeout"} 1.0' in text
        assert 'outcome="error"' in text

    def test_registries_are_isolated(self) -> None:
        first, second = Metrics(), Metrics()
        first.record_error("r", "x")
        assert 'error_type="x"' not in second.render().decode()

    def test_quality_gauge_reads_the_eval_report(self, tmp_path: Path) -> None:
        report = tmp_path / "latest.json"
        report.write_text(json.dumps({"mean_score": 0.83}), encoding="utf-8")
        assert "agent_quality_score 0.83" in Metrics(str(report)).render().decode()

    @pytest.mark.parametrize("content", [None, "not json", "{}", '{"mean_score": "x"}'])
    def test_quality_gauge_is_nan_when_unavailable(
        self, tmp_path: Path, content: str | None
    ) -> None:
        report = tmp_path / "latest.json"
        if content is not None:
            report.write_text(content, encoding="utf-8")
        line = next(
            row
            for row in Metrics(str(report)).render().decode().splitlines()
            if row.startswith("agent_quality_score ")
        )
        assert math.isnan(float(line.split()[1]))

    def test_quality_gauge_without_configured_file(self) -> None:
        assert "agent_quality_score NaN" in Metrics().render().decode()


class FakeObservation:
    def __init__(self) -> None:
        self.updates: list[dict[str, Any]] = []

    def update(self, **kwargs: Any) -> None:
        self.updates.append(kwargs)


class FakeContext:
    def __init__(self, obs: FakeObservation) -> None:
        self.obs = obs
        self.exited_with: Any = "not-exited"

    def __enter__(self) -> FakeObservation:
        return self.obs

    def __exit__(self, exc_type: Any, exc: Any, tb: Any) -> None:
        self.exited_with = exc_type


class TestLangfuseTracer:
    def _tracer(self) -> tuple[LangfuseTracer, MagicMock, list[FakeContext]]:
        client = MagicMock()
        contexts: list[FakeContext] = []

        def start(**kwargs: Any) -> FakeContext:
            ctx = FakeContext(FakeObservation())
            ctx.kwargs = kwargs  # type: ignore[attr-defined]
            contexts.append(ctx)
            return ctx

        client.start_as_current_observation.side_effect = start
        return LangfuseTracer(client), client, contexts

    def test_generation_records_usage_and_cost(self) -> None:
        tracer, _, contexts = self._tracer()
        with tracer.generation("llm-call", model="gpt-4o-mini", input=[{"role": "user"}]) as gen:
            gen.record(output="hola", usage=TokenUsage(10, 5), cost_usd=0.001, model="gpt-4o-mini")
        ctx = contexts[0]
        assert ctx.kwargs["as_type"] == "generation"  # type: ignore[attr-defined]
        assert ctx.kwargs["model"] == "gpt-4o-mini"  # type: ignore[attr-defined]
        update = ctx.obs.updates[0]
        assert update["usage_details"] == {"input_tokens": 10, "output_tokens": 5}
        assert update["cost_details"] == {"total": 0.001}
        assert ctx.exited_with is None

    def test_trace_wraps_a_span_and_passes_updates(self) -> None:
        tracer, _, contexts = self._tracer()
        with tracer.trace("process_query", input="hola") as span:
            span.update(output="adios")
        assert contexts[0].kwargs["as_type"] == "span"  # type: ignore[attr-defined]
        assert contexts[0].obs.updates == [{"output": "adios"}]

    def test_errors_mark_the_observation_and_propagate(self) -> None:
        tracer, _, contexts = self._tracer()
        with pytest.raises(RuntimeError), tracer.generation("llm-call", model="m"):
            raise RuntimeError("boom")
        assert contexts[0].obs.updates[0]["level"] == "ERROR"
        assert contexts[0].exited_with is RuntimeError

        with pytest.raises(RuntimeError), tracer.trace("process_query"):
            raise RuntimeError("boom")
        assert contexts[1].obs.updates[0]["status_message"] == "RuntimeError"

    def test_sdk_failures_never_break_the_request(self) -> None:
        client = MagicMock()
        client.start_as_current_observation.side_effect = RuntimeError("langfuse down")
        client.score_current_trace.side_effect = RuntimeError("down")
        client.flush.side_effect = RuntimeError("down")
        tracer = LangfuseTracer(client)
        with tracer.trace("t") as span:
            span.update(output="x")
        with tracer.generation("g") as gen:
            gen.record(output="x", usage=TokenUsage(1, 1), cost_usd=0.0, model="m")
        tracer.score("quality", 0.5)
        tracer.flush()

    def test_update_failures_are_swallowed(self) -> None:
        tracer, _, contexts = self._tracer()
        with tracer.generation("g") as gen:
            contexts[0].obs.update = MagicMock(side_effect=RuntimeError("x"))  # type: ignore[method-assign]
            gen.update(output="x")
            gen.record(output="x", usage=TokenUsage(1, 1), cost_usd=0.0, model="m")

    def test_score_and_flush_are_forwarded(self) -> None:
        tracer, client, _ = self._tracer()
        tracer.score("quality", 0.9, comment="judge")
        tracer.flush()
        client.score_current_trace.assert_called_once_with(
            name="quality", value=0.9, comment="judge"
        )
        client.flush.assert_called_once()


class TestCreateTracer:
    def test_noop_without_keys(self) -> None:
        tracer = create_tracer(make_settings())
        assert type(tracer) is Tracer and not tracer.enabled
        with tracer.trace("x") as t, tracer.generation("y") as g:
            t.update(output="a")
            g.record(output="a", usage=TokenUsage(), cost_usd=0.0, model="m")
        tracer.score("s", 1.0)
        tracer.flush()

    def test_langfuse_when_both_keys_present(self) -> None:
        factory = MagicMock()
        cfg = make_settings(
            langfuse_public_key="pk-lf-1", langfuse_secret_key="sk-lf-1", app_env="staging"
        )
        tracer = create_tracer(cfg, langfuse_factory=factory)
        assert isinstance(tracer, LangfuseTracer) and tracer.enabled
        kwargs = factory.call_args.kwargs
        assert kwargs["public_key"] == "pk-lf-1" and kwargs["secret_key"] == "sk-lf-1"
        assert kwargs["host"] == "https://cloud.langfuse.com"
        assert kwargs["environment"] == "staging"

    def test_default_factory_is_the_real_sdk_class(self, monkeypatch: pytest.MonkeyPatch) -> None:
        # Tracing disabled so no exporter thread talks to the network during the test.
        monkeypatch.setenv("LANGFUSE_TRACING_ENABLED", "false")
        cfg = make_settings(langfuse_public_key="pk-lf-1", langfuse_secret_key="sk-lf-1")
        assert isinstance(create_tracer(cfg), LangfuseTracer)


class TestSentry:
    def test_disabled_without_dsn(self) -> None:
        sdk = MagicMock()
        assert init_sentry(make_settings(), sdk) is False
        sdk.init.assert_not_called()

    def test_initialised_with_safe_defaults(self) -> None:
        sdk = MagicMock()
        cfg = make_settings(
            sentry_dsn="https://key@o0.ingest.sentry.io/1", git_sha="abc1234", app_env="staging"
        )
        assert init_sentry(cfg, sdk) is True
        kwargs = sdk.init.call_args.kwargs
        assert kwargs["data_collection"]["user_info"] is False
        assert kwargs["data_collection"]["http_bodies"] == []
        assert "send_default_pii" not in kwargs
        assert kwargs["before_send"] is scrub_event
        assert kwargs["environment"] == "staging"
        assert kwargs["release"].endswith("+abc1234")

    def test_real_sdk_initialises_without_sending(self) -> None:
        import sentry_sdk

        cfg = make_settings(sentry_dsn="https://key@o0.ingest.sentry.io/1")
        assert init_sentry(cfg) is True
        sentry_sdk.init()  # reset to a disabled client

    def test_scrub_removes_pii(self) -> None:
        event = {
            "request": {
                "data": {"message": "my IBAN is ES00"},
                "cookies": {"s": "1"},
                "query_string": "token=1",
                "headers": {"Authorization": "Bearer x", "X-API-Key": "k", "Accept": "*/*"},
            },
            "user": {"id": "u1", "email": "a@b.c", "ip_address": "1.2.3.4"},
        }
        cleaned = scrub_event(event)
        assert cleaned is not None
        assert "data" not in cleaned["request"] and "cookies" not in cleaned["request"]
        assert "query_string" not in cleaned["request"]
        assert cleaned["request"]["headers"]["Authorization"] == "[Filtered]"
        assert cleaned["request"]["headers"]["X-API-Key"] == "[Filtered]"
        assert cleaned["request"]["headers"]["Accept"] == "*/*"
        assert cleaned["user"] == {"id": "u1"}

    def test_scrub_tolerates_minimal_events(self) -> None:
        assert scrub_event({}) == {}
        assert scrub_event({"request": {"headers": []}, "user": None}) is not None
