"""Integration tests: real connectivity and the HTTP API backed by the real model.

Every test is marked ``integration``; they call Azure OpenAI (and Tavily when a key exists).
Prompts are short on purpose to keep the token cost of each CI run negligible.
"""

from __future__ import annotations

import pytest
from fastapi.testclient import TestClient

from src.api import create_app
from src.config import Settings
from src.observability import Metrics
from src.providers import AzureOpenAIProvider
from src.tools import TavilySearchTool

pytestmark = pytest.mark.integration


class TestConnectivity:
    """Credentials, endpoint and deployments are alive."""

    async def test_chat_deployment_answers(self, ask) -> None:  # type: ignore[no-untyped-def]
        answer = await ask("Responde solo con la palabra OK", max_tokens=20)
        assert answer.text, "empty answer from the chat deployment"
        assert answer.input_tokens > 0 and answer.output_tokens > 0, "no token usage reported"

    async def test_judge_deployment_answers(
        self, provider: AzureOpenAIProvider, live_settings: Settings
    ) -> None:
        result = await provider.complete(
            [{"role": "user", "content": "Responde solo con la palabra OK"}],
            model=live_settings.judge_model,
            max_tokens=20,
        )
        assert result.text, f"judge deployment {live_settings.judge_model!r} returned no text"

    async def test_tavily_search_when_key_is_configured(self, live_settings: Settings) -> None:
        if not live_settings.tavily_api_key:
            pytest.skip("TAVILY_API_KEY is not configured")
        tool = TavilySearchTool(live_settings.tavily_api_key.get_secret_value(), max_results=1)
        output = await tool.run(query="Python programming language")
        assert output and output != "No results found.", "Tavily returned nothing"


class TestChatEndpoint:
    """End-to-end: HTTP -> agent -> real model -> metrics."""

    @pytest.fixture
    def client(self, live_settings: Settings):  # type: ignore[no-untyped-def]
        # The api module wires its own tools; local RAG only, so no search credits are spent.
        cfg = live_settings.model_copy(update={"tavily_api_key": None, "kb_id": None})
        with TestClient(create_app(cfg, metrics=Metrics())) as test_client:
            yield test_client

    def test_health_does_not_need_the_model(self, client: TestClient) -> None:
        body = client.get("/health").json()
        assert body["status"] == "ok" and body["provider"] == "azure"

    def test_chat_returns_the_documented_contract(self, client: TestClient) -> None:
        response = client.post("/chat", json={"message": "¿Capital de Francia? Una palabra."})
        assert response.status_code == 200, response.text
        body = response.json()
        assert {"response", "route", "usage", "cost_usd"} <= set(body)
        assert "par" in body["response"].lower(), f"unexpected answer: {body['response']!r}"
        assert body["usage"]["total_tokens"] > 0
        assert body["cost_usd"] > 0, "real calls must have a non-zero estimated cost"

    def test_metrics_reflect_real_usage(self, client: TestClient) -> None:
        client.post("/chat", json={"message": "Di hola en una palabra."})
        metrics = client.get("/metrics").text
        assert (
            'agent_requests_total{agent_route="chat_with_tools",outcome="success"} 1.0' in metrics
        )
        assert 'agent_tokens_total{direction="output"}' in metrics

    def test_deep_research_route_is_selected(self, client: TestClient) -> None:
        response = client.post(
            "/chat", json={"message": "Haz un informe de dos líneas sobre qué es RAG."}
        )
        assert response.status_code == 200, response.text
        assert response.json()["route"] == "deep_research"
