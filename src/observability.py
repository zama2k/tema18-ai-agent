"""Observability: structured JSON logs, Prometheus metrics, optional LangFuse and Sentry.

The three layers of the course map to code as follows:

* infrastructure and service health -> Prometheus metrics (``/metrics``) and JSON logs;
* LLM observability -> LangFuse traces, generations, usage, cost and scores (optional);
* error tracking -> Sentry with PII scrubbing (optional).

Both optional layers are no-ops when their keys are not configured.
"""

from __future__ import annotations

import contextlib
import json
import logging
import math
import re
import sys
from collections.abc import Iterator
from contextvars import ContextVar
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

from prometheus_client import CollectorRegistry, Counter, Gauge, Histogram, generate_latest

from src.config import Settings
from src.models import TokenUsage

logger = logging.getLogger(__name__)

request_id_var: ContextVar[str] = ContextVar("request_id", default="-")

_STANDARD_LOG_ATTRS = set(logging.LogRecord("", 0, "", 0, "", (), None).__dict__) | {
    "message",
    "asctime",
    "taskName",
}


class JsonFormatter(logging.Formatter):
    """Format log records as one JSON object per line."""

    def __init__(self, service: str = "ai-agent", version: str = "unknown") -> None:
        super().__init__()
        self._service = service
        self._version = version

    def format(self, record: logging.LogRecord) -> str:
        """Render ``record`` as JSON (UTC ISO-8601 timestamp, request id, extras)."""
        payload: dict[str, Any] = {
            "timestamp": datetime.fromtimestamp(record.created, tz=UTC).isoformat(),
            "level": record.levelname,
            "logger": record.name,
            "service": self._service,
            "version": self._version,
            "request_id": request_id_var.get(),
            "message": record.getMessage(),
        }
        for key, value in record.__dict__.items():
            if key not in _STANDARD_LOG_ATTRS and not key.startswith("_"):
                payload[key] = value
        if record.exc_info and record.exc_info[0] is not None:
            payload["error"] = {
                "type": record.exc_info[0].__name__,
                "message": str(record.exc_info[1]),
                "stack": self.formatException(record.exc_info),
            }
        return json.dumps(payload, ensure_ascii=False, default=str)


def configure_logging(level: str = "INFO", version: str = "unknown") -> None:
    """Install a single JSON handler on the root logger (idempotent)."""
    root = logging.getLogger()
    for handler in list(root.handlers):
        if getattr(handler, "_ai_agent_json", False):
            root.removeHandler(handler)
    handler = logging.StreamHandler(sys.stdout)
    handler.setFormatter(JsonFormatter(version=version))
    handler._ai_agent_json = True  # type: ignore[attr-defined]
    root.addHandler(handler)
    root.setLevel(level)


class Metrics:
    """Prometheus metrics of the service, on a private registry (test friendly)."""

    def __init__(self, quality_report_file: str | None = None) -> None:
        self.registry = CollectorRegistry()
        self.http_requests = Counter(
            "agent_http_requests_total",
            "HTTP requests",
            ["method", "route", "status"],
            registry=self.registry,
        )
        self.http_duration = Histogram(
            "agent_http_request_duration_seconds",
            "HTTP request latency",
            ["route"],
            buckets=(0.05, 0.1, 0.25, 0.5, 1, 2.5, 5, 10, 15, 30, 60),
            registry=self.registry,
        )
        self.agent_requests = Counter(
            "agent_requests_total",
            "Agent queries by routing decision and outcome",
            ["agent_route", "outcome"],
            registry=self.registry,
        )
        self.errors = Counter(
            "agent_errors_total", "Errors by type", ["error_type"], registry=self.registry
        )
        self.tokens = Counter(
            "agent_tokens_total", "LLM tokens", ["direction"], registry=self.registry
        )
        self.cost = Counter(
            "agent_cost_usd_total", "Estimated LLM cost in USD", registry=self.registry
        )
        self.tool_calls = Counter(
            "agent_tool_calls_total",
            "Tool calls",
            ["tool", "outcome"],
            registry=self.registry,
        )
        self.build_info = Gauge(
            "agent_build_info",
            "Build information",
            ["version", "commit", "color"],
            registry=self.registry,
        )
        self.quality = Gauge(
            "agent_quality_score",
            "Latest LLM-as-judge mean quality score (0-1), read from the eval report",
            registry=self.registry,
        )
        self.quality.set_function(lambda: _read_quality(quality_report_file))

    def record_result(self, route: str, usage: TokenUsage, cost_usd: float) -> None:
        """Record a successful agent query."""
        self.agent_requests.labels(route, "success").inc()
        self.tokens.labels("input").inc(usage.input_tokens)
        self.tokens.labels("output").inc(usage.output_tokens)
        self.cost.inc(cost_usd)

    def record_error(self, route: str, error_type: str) -> None:
        """Record a failed agent query."""
        self.agent_requests.labels(route, "error").inc()
        self.errors.labels(error_type).inc()

    def render(self) -> bytes:
        """Return the Prometheus text exposition."""
        return generate_latest(self.registry)


def _read_quality(path: str | None) -> float:
    if not path:
        return math.nan
    try:
        data = json.loads(Path(path).read_text(encoding="utf-8"))
        return float(data["mean_score"])
    except (OSError, ValueError, KeyError, TypeError):
        return math.nan


# --------------------------------------------------------------------------- LangFuse


class _NullHandle:
    """Handle returned when tracing is disabled; every call is a no-op."""

    def update(self, **_: Any) -> None:
        """Ignore updates."""

    def record(self, **_: Any) -> None:
        """Ignore usage records."""


class Tracer:
    """No-op tracer. :class:`LangfuseTracer` overrides it when LangFuse is configured."""

    enabled = False

    @contextlib.contextmanager
    def trace(self, name: str, **_: Any) -> Iterator[Any]:
        """Open a trace around one agent query."""
        yield _NullHandle()

    @contextlib.contextmanager
    def generation(self, name: str, **_: Any) -> Iterator[Any]:
        """Open a generation around one LLM call."""
        yield _NullHandle()

    def score(self, name: str, value: float, comment: str | None = None) -> None:
        """Attach a score to the current trace."""

    def flush(self) -> None:
        """Flush buffered events."""


class LangfuseTracer(Tracer):
    """Sends traces, generations, usage, cost and scores to LangFuse (SDK v3+ API).

    Reference: https://langfuse.com/docs/observability/sdk/instrumentation
    Tracing problems are logged and swallowed: observability must never break a request.
    """

    enabled = True

    def __init__(self, client: Any) -> None:
        self._client = client

    @contextlib.contextmanager
    def trace(self, name: str, **kwargs: Any) -> Iterator[Any]:
        """Open a root span for one agent query."""
        try:
            cm = self._client.start_as_current_observation(as_type="span", name=name, **kwargs)
            span = cm.__enter__()
        except Exception:
            logger.warning("langfuse trace could not be started", exc_info=True)
            yield _NullHandle()
            return
        try:
            yield span
        except BaseException as exc:
            self._safe_update(span, level="ERROR", status_message=type(exc).__name__)
            cm.__exit__(type(exc), exc, exc.__traceback__)
            raise
        else:
            cm.__exit__(None, None, None)

    @contextlib.contextmanager
    def generation(self, name: str, **kwargs: Any) -> Iterator[Any]:
        """Open a generation observation for one LLM call."""
        try:
            cm = self._client.start_as_current_observation(
                as_type="generation", name=name, **kwargs
            )
            gen = cm.__enter__()
        except Exception:
            logger.warning("langfuse generation could not be started", exc_info=True)
            yield _NullHandle()
            return
        handle = _GenerationHandle(gen)
        try:
            yield handle
        except BaseException as exc:
            self._safe_update(gen, level="ERROR", status_message=type(exc).__name__)
            cm.__exit__(type(exc), exc, exc.__traceback__)
            raise
        else:
            cm.__exit__(None, None, None)

    def score(self, name: str, value: float, comment: str | None = None) -> None:
        """Attach a numeric score to the current trace."""
        try:
            self._client.score_current_trace(name=name, value=value, comment=comment)
        except Exception:
            logger.warning("langfuse score failed", exc_info=True)

    def flush(self) -> None:
        """Flush pending events (call before the process exits)."""
        try:
            self._client.flush()
        except Exception:
            logger.warning("langfuse flush failed", exc_info=True)

    @staticmethod
    def _safe_update(obj: Any, **kwargs: Any) -> None:
        try:
            obj.update(**kwargs)
        except Exception:
            logger.warning("langfuse update failed", exc_info=True)


class _GenerationHandle:
    def __init__(self, generation: Any) -> None:
        self._generation = generation

    def update(self, **kwargs: Any) -> None:
        """Pass-through update."""
        LangfuseTracer._safe_update(self._generation, **kwargs)

    def record(self, *, output: str, usage: TokenUsage, cost_usd: float, model: str) -> None:
        """Record output, token usage and cost of the finished call."""
        LangfuseTracer._safe_update(
            self._generation,
            output=output,
            model=model,
            usage_details={
                "input_tokens": usage.input_tokens,
                "output_tokens": usage.output_tokens,
            },
            cost_details={"total": cost_usd},
        )


MAX_TRACED_CHARS = 2000
_REDACTIONS: tuple[tuple[re.Pattern[str], str], ...] = (
    (re.compile(r"[\w.+-]+@[\w-]+(?:\.[\w-]+)+"), "[EMAIL]"),
    (re.compile(r"\b[A-Z]{2}\d{2}(?: ?[A-Z0-9]{4}){3,7}(?: ?[A-Z0-9]{1,4})?\b"), "[IBAN]"),
    (re.compile(r"\b(?:\d{8}|[XYZ]\d{7})[A-Z]\b"), "[ID]"),
    (re.compile(r"\b(?:sk|pk|tvly|AKIA)[-_A-Za-z0-9]{12,}"), "[SECRET]"),
    (re.compile(r"(?i)\b(bearer|api[_-]?key|token|password)\b\s*[:=]?\s*\S{6,}"), r"\1 [SECRET]"),
    (re.compile(r"(?<![\w.])\+?\d(?:[ .()-]?\d){8,}(?![\w])"), "[NUMBER]"),
)


def mask_pii(data: Any) -> Any:
    """Redact personal data and secrets and cap the length of everything sent to LangFuse.

    Strings are scrubbed (emails, IBANs, Spanish national ids, API-key-looking tokens, passwords
    and any run of 9+ digits such as phones or card numbers) and truncated to
    ``MAX_TRACED_CHARS``. Lists, tuples and dicts are processed recursively. This is a
    best-effort filter, not a guarantee: free text can still hold names or addresses.
    """
    if isinstance(data, str):
        text = data
        for pattern, replacement in _REDACTIONS:
            text = pattern.sub(replacement, text)
        if len(text) > MAX_TRACED_CHARS:
            text = f"{text[:MAX_TRACED_CHARS]}...[truncated {len(text) - MAX_TRACED_CHARS} chars]"
        return text
    if isinstance(data, dict):
        return {key: mask_pii(value) for key, value in data.items()}
    if isinstance(data, list | tuple):
        return [mask_pii(item) for item in data]
    return data


def langfuse_mask(*, data: Any, **_: Any) -> Any:
    """Adapter with the keyword-only signature that ``Langfuse(mask=...)`` expects."""
    return mask_pii(data)


def create_tracer(settings: Settings, langfuse_factory: Any | None = None) -> Tracer:
    """Return a LangFuse tracer if both keys are configured, otherwise a no-op tracer."""
    if not settings.langfuse_enabled:
        return Tracer()
    if langfuse_factory is None:
        from langfuse import Langfuse

        langfuse_factory = Langfuse
    assert settings.langfuse_public_key and settings.langfuse_secret_key
    client = langfuse_factory(
        public_key=settings.langfuse_public_key.get_secret_value(),
        secret_key=settings.langfuse_secret_key.get_secret_value(),
        host=settings.langfuse_host,
        environment=settings.app_env,
        release=settings.app_version,
        mask=langfuse_mask,
    )
    return LangfuseTracer(client)


# --------------------------------------------------------------------------- Sentry

_SENSITIVE_HEADERS = {"authorization", "cookie", "set-cookie", "x-api-key", "api-key"}


def scrub_event(event: dict[str, Any], hint: dict[str, Any] | None = None) -> dict[str, Any] | None:
    """Sentry ``before_send`` hook that removes PII and secrets from an event."""
    request = event.get("request")
    if isinstance(request, dict):
        request.pop("data", None)  # chat messages may contain personal data
        request.pop("cookies", None)
        request.pop("query_string", None)
        headers = request.get("headers")
        if isinstance(headers, dict):
            request["headers"] = {
                k: ("[Filtered]" if k.lower() in _SENSITIVE_HEADERS else v)
                for k, v in headers.items()
            }
    user = event.get("user")
    if isinstance(user, dict):
        event["user"] = {k: v for k, v in user.items() if k == "id"}
    return event


def init_sentry(settings: Settings, sentry_sdk_module: Any | None = None) -> bool:
    """Initialise Sentry if a DSN is configured.

    Returns:
        True when Sentry was initialised, False when it stays disabled.
    """
    if not settings.sentry_enabled:
        return False
    sdk: Any = sentry_sdk_module
    if sdk is None:
        import sentry_sdk as sdk
    assert settings.sentry_dsn
    sdk.init(
        dsn=settings.sentry_dsn.get_secret_value(),
        environment=settings.app_env,
        release=f"{settings.app_version}+{settings.git_sha}",
        # sentry-sdk >= 2.7x replaces send_default_pii with data_collection; everything is off
        # and before_send scrubs whatever still gets through (defence in depth).
        data_collection={
            "user_info": False,
            "cookies": {"mode": "off"},
            "http_bodies": [],
            "url_query_params": {"mode": "off"},
            "gen_ai": {"inputs": False, "outputs": False},
            "stack_frame_variables": False,
        },
        traces_sample_rate=0.0,
        before_send=scrub_event,
    )
    return True
