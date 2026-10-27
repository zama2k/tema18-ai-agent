"""Unit tests for the FastAPI application (TestClient + scripted/mock providers)."""

from __future__ import annotations

import json
import re
from collections.abc import Iterator
from typing import Any

import pytest
from fastapi.testclient import TestClient

from src.agent import AIAgent
from src.api import create_app
from src.config import Settings
from src.exceptions import (
    ContentFilteredError,
    MaxIterationsError,
    ProviderError,
    ProviderTimeoutError,
)
from src.observability import Metrics
from src.providers import MockProvider
from src.tools import Tool
from tests.unit.conftest import ScriptedProvider, text_response


def make_settings(**kwargs: Any) -> Settings:
    return Settings(  # type: ignore[call-arg]
        llm_provider="mock",
        git_sha="abc1234",
        deploy_color="green",
        app_version="9.9.9",
        _env_file=None,
        **kwargs,
    )


@pytest.fixture
def client() -> Iterator[TestClient]:
    app = create_app(make_settings(), metrics=Metrics())
    with TestClient(app) as test_client:
        yield test_client


def metrics_text(test_client: TestClient) -> str:
    return test_client.get("/metrics").text


class TestHealth:
    def test_reports_build_identity(self, client: TestClient) -> None:
        body = client.get("/health").json()
        assert body == {
            "status": "ok",
            "version": "9.9.9",
            "commit": "abc1234",
            "color": "green",
            "provider": "mock",
            "environment": "local",
        }

    def test_does_not_call_the_llm(self) -> None:
        provider = ScriptedProvider([])
        agent = AIAgent(provider, [], make_settings())
        with TestClient(create_app(make_settings(), agent=agent, metrics=Metrics())) as c:
            assert c.get("/health").status_code == 200
        assert provider.calls == []


class TestChat:
    def test_success_contract(self, client: TestClient) -> None:
        response = client.post("/chat", json={"message": "¿Cuál es la capital de Francia?"})
        assert response.status_code == 200
        body = response.json()
        assert set(body) == {"response", "route", "usage", "cost_usd", "model"}
        assert "París" in body["response"]
        assert body["route"] == "chat_with_tools"
        assert body["usage"]["total_tokens"] == (
            body["usage"]["input_tokens"] + body["usage"]["output_tokens"]
        )

    def test_deep_research_route(self, client: TestClient) -> None:
        body = client.post("/chat", json={"message": "Hazme un informe"}).json()
        assert body["route"] == "deep_research"

    @pytest.mark.parametrize(
        "payload",
        [{}, {"message": ""}, {"message": "x" * 4001}, {"message": 5}, {"msg": "hola"}],
    )
    def test_validation_errors(self, client: TestClient, payload: dict[str, Any]) -> None:
        assert client.post("/chat", json=payload).status_code == 422

    def test_blank_message_is_a_validation_error(self, client: TestClient) -> None:
        assert client.post("/chat", json={"message": "   "}).status_code == 422

    @pytest.mark.parametrize(
        ("exc", "status", "error_type"),
        [
            (ProviderTimeoutError("t"), 504, "timeout"),
            (ContentFilteredError("cf"), 422, "content_filtered"),
            (MaxIterationsError("m"), 502, "max_iterations"),
            (ProviderError("secret upstream detail"), 502, "upstream_error"),
            (RuntimeError("secret internal detail"), 500, "internal_error"),
        ],
    )
    def test_errors_are_mapped_without_leaking_details(
        self, exc: Exception, status: int, error_type: str
    ) -> None:
        agent = AIAgent(ScriptedProvider([exc]), [], make_settings())
        with TestClient(create_app(make_settings(), agent=agent, metrics=Metrics())) as c:
            response = c.post("/chat", json={"message": "hola"})
        assert response.status_code == status
        error = response.json()["error"]
        assert error["type"] == error_type
        assert "secret" not in response.text
        assert error["request_id"] == response.headers["X-Request-ID"]

    def test_error_metrics_are_recorded(self) -> None:
        agent = AIAgent(ScriptedProvider([ProviderError("x")]), [], make_settings())
        with TestClient(create_app(make_settings(), agent=agent, metrics=Metrics())) as c:
            c.post("/chat", json={"message": "hola"})
            text = metrics_text(c)
        assert 'agent_http_requests_total{method="POST",route="/chat",status="502"} 1.0' in text
        assert 'agent_requests_total{agent_route="chat_with_tools",outcome="error"} 1.0' in text
        assert 'agent_errors_total{error_type="ProviderError"} 1.0' in text

    def test_tool_metrics(self) -> None:
        class Search(Tool):
            name = "web_search"
            description = "search"
            parameters = {"type": "object", "properties": {"query": {"type": "string"}}}

            async def run(self, **kwargs: Any) -> str:
                return "dato"

        agent = AIAgent(MockProvider(), [Search()], make_settings())
        with TestClient(create_app(make_settings(), agent=agent, metrics=Metrics())) as c:
            body = c.post("/chat", json={"message": "busca algo"}).json()
            text = metrics_text(c)
        assert "dato" in body["response"]
        assert 'agent_tool_calls_total{outcome="called",tool="web_search"} 1.0' in text

    def test_tool_failure_metric(self) -> None:
        from tests.unit.conftest import tool_response

        provider = ScriptedProvider([tool_response(("c", "ghost", {})), text_response("ok")])
        agent = AIAgent(provider, [], make_settings())
        with TestClient(create_app(make_settings(), agent=agent, metrics=Metrics())) as c:
            assert c.post("/chat", json={"message": "hola"}).status_code == 200
            assert 'outcome="failed"' in metrics_text(c)


class TestObservability:
    def test_request_id_is_generated(self, client: TestClient) -> None:
        rid = client.get("/health").headers["X-Request-ID"]
        assert re.fullmatch(r"[0-9a-f]{32}", rid)

    def test_valid_incoming_request_id_is_kept(self, client: TestClient) -> None:
        response = client.get("/health", headers={"X-Request-ID": "trace-abc-12345"})
        assert response.headers["X-Request-ID"] == "trace-abc-12345"

    def test_hostile_request_id_is_replaced(self, client: TestClient) -> None:
        response = client.get("/health", headers={"X-Request-ID": "bad id\twith spaces!"})
        assert re.fullmatch(r"[0-9a-f]{32}", response.headers["X-Request-ID"])

    def test_metrics_exposition(self, client: TestClient) -> None:
        client.get("/health")
        client.post("/chat", json={"message": "hola"})
        text = metrics_text(client)
        assert 'agent_http_requests_total{method="GET",route="/health",status="200"} 1.0' in text
        assert 'agent_http_requests_total{method="POST",route="/chat",status="200"} 1.0' in text
        assert "agent_http_request_duration_seconds_bucket" in text
        assert 'agent_build_info{color="green",commit="abc1234",version="9.9.9"} 1.0' in text
        assert "agent_tokens_total" in text and "agent_cost_usd_total" in text

    def test_unknown_routes_do_not_explode_label_cardinality(self, client: TestClient) -> None:
        for i in range(5):
            assert client.get(f"/nope/{i}").status_code == 404
        text = metrics_text(client)
        assert 'route="unmatched",status="404"} 5.0' in text
        assert "/nope/" not in text

    def test_access_log_is_structured(self, caplog: pytest.LogCaptureFixture) -> None:
        with TestClient(create_app(make_settings(), metrics=Metrics())) as c:
            with caplog.at_level("INFO", logger="ai_agent.api"):
                c.get("/health")
        records = [r for r in caplog.records if r.getMessage() == "request"]
        assert records and records[-1].status == 200  # type: ignore[attr-defined]
        assert records[-1].path == "/health"  # type: ignore[attr-defined]


class TestLifecycle:
    def test_agent_is_built_from_settings(self) -> None:
        app = create_app(make_settings(), metrics=Metrics())
        with TestClient(app):
            assert isinstance(app.state.agent, AIAgent)
            assert app.state.agent.provider_name == MockProvider.name

    def test_tracer_is_flushed_on_shutdown(self) -> None:
        flushed: list[bool] = []

        from src.observability import Tracer

        class SpyTracer(Tracer):
            def flush(self) -> None:
                flushed.append(True)

        app = create_app(make_settings(), metrics=Metrics(), tracer=SpyTracer())
        with TestClient(app):
            pass
        assert flushed == [True]

    def test_metrics_are_created_when_not_injected(self) -> None:
        with TestClient(create_app(make_settings())) as c:
            assert json.dumps(c.get("/health").json())
            assert "agent_build_info" in c.get("/metrics").text
