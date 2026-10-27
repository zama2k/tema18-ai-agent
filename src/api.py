"""FastAPI application: ``/health``, ``/chat`` and ``/metrics``.

Run with ``uvicorn src.api:create_app --factory``.
"""

from __future__ import annotations

import logging
import re
import time
import uuid
from collections.abc import AsyncIterator, Awaitable, Callable
from contextlib import asynccontextmanager
from typing import Any

from fastapi import FastAPI, Request, Response
from fastapi.responses import JSONResponse
from prometheus_client import CONTENT_TYPE_LATEST
from pydantic import BaseModel, Field, field_validator

from src.agent import AIAgent, route_message
from src.config import Settings, load_settings
from src.exceptions import AgentBaseException, MaxIterationsError, ProviderTimeoutError
from src.observability import (
    Metrics,
    Tracer,
    configure_logging,
    create_tracer,
    init_sentry,
    request_id_var,
)
from src.providers import create_provider
from src.tools import build_tools

logger = logging.getLogger("ai_agent.api")

_REQUEST_ID_RE = re.compile(r"^[A-Za-z0-9._-]{8,64}$")


class ChatRequest(BaseModel):
    """Body of ``POST /chat``."""

    message: str = Field(min_length=1, max_length=4000, description="User message.")

    @field_validator("message")
    @classmethod
    def _not_blank(cls, value: str) -> str:
        if not value.strip():
            raise ValueError("message must not be blank")
        return value


class UsageOut(BaseModel):
    """Token usage of a query."""

    input_tokens: int
    output_tokens: int
    total_tokens: int


class ChatResponse(BaseModel):
    """Body returned by ``POST /chat``."""

    response: str
    route: str
    usage: UsageOut
    cost_usd: float
    model: str


def _error(status: int, error_type: str, message: str) -> JSONResponse:
    return JSONResponse(
        status_code=status,
        content={
            "error": {
                "type": error_type,
                "message": message,
                "request_id": request_id_var.get(),
            }
        },
    )


def create_app(
    settings: Settings | None = None,
    agent: AIAgent | None = None,
    metrics: Metrics | None = None,
    tracer: Tracer | None = None,
) -> FastAPI:
    """Application factory.

    Args:
        settings: Settings; loaded from the environment/``ENV_FILE`` when omitted.
        agent: Pre-built agent (tests); built from the settings when omitted.
        metrics: Metrics registry (tests); created when omitted.
        tracer: Tracer (tests); created from the settings when omitted.
    """
    cfg = settings or load_settings()
    app_metrics = metrics or Metrics(cfg.quality_report_file)
    app_metrics.build_info.labels(cfg.app_version, cfg.git_sha, cfg.deploy_color or "none").set(1)

    @asynccontextmanager
    async def lifespan(app: FastAPI) -> AsyncIterator[None]:
        configure_logging(cfg.log_level, cfg.app_version)
        init_sentry(cfg)
        app.state.tracer = tracer or create_tracer(cfg)
        if agent is None:
            app.state.agent = AIAgent(
                create_provider(cfg), build_tools(cfg), cfg, tracer=app.state.tracer
            )
        else:
            app.state.agent = agent
        logger.info(
            "service started",
            extra={"commit": cfg.git_sha, "color": cfg.deploy_color, "env": cfg.app_env},
        )
        yield
        app.state.tracer.flush()

    app = FastAPI(title="AI Agent", version=cfg.app_version, lifespan=lifespan)
    app.state.metrics = app_metrics

    @app.middleware("http")
    async def observe(
        request: Request, call_next: Callable[[Request], Awaitable[Response]]
    ) -> Response:
        incoming = request.headers.get("x-request-id", "")
        rid = incoming if _REQUEST_ID_RE.match(incoming) else uuid.uuid4().hex
        token = request_id_var.set(rid)
        started = time.perf_counter()
        status = 500
        try:
            response = await call_next(request)
            status = response.status_code
            response.headers["X-Request-ID"] = rid
            return response
        finally:
            elapsed = time.perf_counter() - started
            route = request.scope.get("route")
            label = getattr(route, "path", "unmatched")
            app_metrics.http_requests.labels(request.method, label, str(status)).inc()
            app_metrics.http_duration.labels(label).observe(elapsed)
            logger.info(
                "request",
                extra={
                    "method": request.method,
                    "path": label,
                    "status": status,
                    "duration_ms": round(elapsed * 1000, 1),
                },
            )
            request_id_var.reset(token)

    @app.get("/health")
    async def health() -> dict[str, Any]:
        """Liveness plus build identity (used by deploy health checks)."""
        return {
            "status": "ok",
            "version": cfg.app_version,
            "commit": cfg.git_sha,
            "color": cfg.deploy_color,
            "provider": app.state.agent.provider_name,
            "environment": cfg.app_env,
        }

    @app.post("/chat", response_model=ChatResponse)
    async def chat(body: ChatRequest) -> Any:
        """Run the agent on one message."""
        active: AIAgent = app.state.agent
        route = route_message(body.message)
        try:
            result = await active.process_query(body.message)
        except ProviderTimeoutError as exc:
            app_metrics.record_error(route, "timeout")
            logger.error("llm timeout", extra={"error": str(exc)})
            return _error(504, "timeout", "The language model did not answer in time.")
        except MaxIterationsError as exc:
            app_metrics.record_error(route, "max_iterations")
            logger.error("iteration cap", extra={"error": str(exc)})
            return _error(502, "max_iterations", "The agent could not produce an answer.")
        except AgentBaseException as exc:
            app_metrics.record_error(route, type(exc).__name__)
            logger.error("agent error", extra={"error": str(exc)})
            return _error(502, "upstream_error", "The language model backend failed.")
        except Exception as exc:
            app_metrics.record_error(route, "unexpected")
            logger.exception("unexpected error")
            _capture(exc)
            return _error(500, "internal_error", "Unexpected internal error.")

        app_metrics.record_result(result.route, result.usage, result.cost_usd)
        for tool in result.tools_used:
            app_metrics.tool_calls.labels(tool, "called").inc()
        if result.tool_failures:
            app_metrics.tool_calls.labels("any", "failed").inc(result.tool_failures)
        return ChatResponse(
            response=result.response,
            route=result.route,
            usage=UsageOut(
                input_tokens=result.usage.input_tokens,
                output_tokens=result.usage.output_tokens,
                total_tokens=result.usage.total,
            ),
            cost_usd=round(result.cost_usd, 8),
            model=result.model,
        )

    @app.get("/metrics")
    async def metrics_endpoint() -> Response:
        """Prometheus exposition."""
        return Response(content=app_metrics.render(), media_type=CONTENT_TYPE_LATEST)

    return app


def _capture(exc: Exception) -> None:
    try:
        import sentry_sdk

        sentry_sdk.capture_exception(exc)
    except Exception:
        logger.debug("sentry capture failed", exc_info=True)
