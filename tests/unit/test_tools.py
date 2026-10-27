"""Unit tests for the tools (Tavily and RAG)."""

from __future__ import annotations

from typing import Any

import pytest
from botocore.stub import Stubber

from src.config import Settings
from src.exceptions import AgentBaseException, RAGRetrievalError, TavilySearchError, ToolError
from src.tools import RAGTool, TavilySearchTool, build_tools


class FakeTavily:
    def __init__(self, result: dict[str, Any] | Exception) -> None:
        self.result = result
        self.calls: list[dict[str, Any]] = []

    async def search(self, **kwargs: Any) -> dict[str, Any]:
        self.calls.append(kwargs)
        if isinstance(self.result, Exception):
            raise self.result
        return self.result


class TestTavilySearchTool:
    async def test_formats_answer_and_results(self) -> None:
        client = FakeTavily(
            {
                "answer": "Madrid es la capital.",
                "results": [
                    {"title": "Madrid", "url": "https://a.test", "content": "Capital de España"},
                    {"title": "B", "url": "https://b.test", "content": "x" * 2000},
                ],
            }
        )
        output = await TavilySearchTool("key", client=client).run(query="  capital de España ")
        assert client.calls[0]["query"] == "capital de España"
        assert "Summary: Madrid es la capital." in output
        assert "https://a.test" in output
        assert len(output) < 1500  # snippets are clipped

    async def test_no_results(self) -> None:
        assert await TavilySearchTool("k", client=FakeTavily({"results": []})).run(query="q") == (
            "No results found."
        )

    @pytest.mark.parametrize("query", [None, "", "   ", 5])
    async def test_invalid_query(self, query: Any) -> None:
        with pytest.raises(TavilySearchError, match="non-empty"):
            await TavilySearchTool("k", client=FakeTavily({})).run(query=query)

    async def test_client_failure_is_wrapped(self) -> None:
        tool = TavilySearchTool("k", client=FakeTavily(RuntimeError("secret detail")))
        with pytest.raises(TavilySearchError) as info:
            await tool.run(query="q")
        assert "secret detail" not in str(info.value)
        assert isinstance(info.value, ToolError) and isinstance(info.value, AgentBaseException)

    def test_schema_is_a_valid_function_definition(self) -> None:
        schema = TavilySearchTool("k", client=FakeTavily({})).schema()
        assert schema["type"] == "function"
        assert schema["function"]["name"] == "web_search"
        assert schema["function"]["parameters"]["required"] == ["query"]

    def test_default_client_is_the_async_tavily_client(self) -> None:
        tool = TavilySearchTool("tvly-test")
        assert type(tool._client).__name__ == "AsyncTavilyClient"


class TestRAGToolLocal:
    async def test_finds_relevant_document(self) -> None:
        output = await RAGTool().run(query="¿Qué es RAG y la recuperación de documentos?")
        assert "RAG" in output

    async def test_accents_and_case_are_ignored(self) -> None:
        output = await RAGTool().run(query="DESPLIEGUE BLUE GREEN")
        assert "blue/green" in output

    async def test_no_match(self) -> None:
        assert await RAGTool().run(query="zzzz qqqq") == "No relevant documents found."

    async def test_respects_top_k(self) -> None:
        output = await RAGTool(top_k=1).run(query="llm ci despliegue rag observabilidad")
        assert output.count("\n") == 0

    @pytest.mark.parametrize("query", [None, "", "  "])
    async def test_invalid_query(self, query: Any) -> None:
        with pytest.raises(RAGRetrievalError):
            await RAGTool().run(query=query)

    def test_backend_name(self) -> None:
        assert RAGTool().backend == "local"


def kb_client() -> Any:
    import boto3

    return boto3.client(
        "bedrock-agent-runtime",
        region_name="us-east-1",
        aws_access_key_id="t",
        aws_secret_access_key="t",
    )


class TestRAGToolBedrock:
    async def test_retrieve_formats_results(self) -> None:
        client = kb_client()
        stub = Stubber(client)
        stub.add_response(
            "retrieve",
            {
                "retrievalResults": [
                    {"content": {"text": "Primer fragmento"}, "score": 0.9},
                    {"content": {"text": "Segundo fragmento"}, "score": 0.8},
                ]
            },
            {
                "knowledgeBaseId": "KB12345678",
                "retrievalQuery": {"text": "hola"},
                "retrievalConfiguration": {"vectorSearchConfiguration": {"numberOfResults": 3}},
            },
        )
        stub.activate()
        tool = RAGTool("KB12345678", client)
        assert tool.backend == "bedrock"
        output = await tool.run(query="hola")
        stub.assert_no_pending_responses()
        assert output == "- Primer fragmento\n- Segundo fragmento"

    async def test_empty_results(self) -> None:
        client = kb_client()
        stub = Stubber(client)
        stub.add_response("retrieve", {"retrievalResults": []})
        stub.activate()
        assert await RAGTool("KB12345678", client).run(query="x") == "No relevant documents found."

    async def test_client_error_is_wrapped(self) -> None:
        client = kb_client()
        stub = Stubber(client)
        stub.add_client_error("retrieve", service_error_code="ResourceNotFoundException")
        stub.activate()
        with pytest.raises(RAGRetrievalError, match="ResourceNotFoundException"):
            await RAGTool("KB12345678", client).run(query="x")

    async def test_transport_error_is_wrapped(self) -> None:
        from botocore.exceptions import EndpointConnectionError

        class Broken:
            def retrieve(self, **_: Any) -> Any:
                raise EndpointConnectionError(endpoint_url="https://x.test")

        with pytest.raises(RAGRetrievalError, match="transport"):
            await RAGTool("KB12345678", Broken()).run(query="x")


class TestBuildTools:
    def test_only_rag_without_keys(self) -> None:
        cfg = Settings(llm_provider="mock", _env_file=None)  # type: ignore[call-arg]
        assert [t.name for t in build_tools(cfg)] == ["knowledge_base"]

    def test_tavily_and_bedrock_kb_when_configured(self) -> None:
        cfg = Settings(  # type: ignore[call-arg]
            llm_provider="mock", tavily_api_key="tvly-x", kb_id="KB12345678", _env_file=None
        )
        tools = build_tools(cfg)
        assert [t.name for t in tools] == ["web_search", "knowledge_base"]
        assert tools[1].backend == "bedrock"  # type: ignore[attr-defined]
