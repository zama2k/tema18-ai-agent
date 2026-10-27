"""Tools the agent can call: web search (Tavily) and document retrieval (RAG)."""

from __future__ import annotations

import asyncio
import logging
import re
import unicodedata
from abc import ABC, abstractmethod
from typing import Any

from botocore.exceptions import BotoCoreError, ClientError

from src.config import Settings
from src.exceptions import RAGRetrievalError, TavilySearchError
from src.models import ToolSchema

logger = logging.getLogger(__name__)

MAX_SNIPPET_CHARS = 600


class Tool(ABC):
    """Base class of agent tools."""

    name: str
    description: str
    parameters: dict[str, Any]

    def schema(self) -> ToolSchema:
        """Return the OpenAI-style function schema of this tool."""
        return {
            "type": "function",
            "function": {
                "name": self.name,
                "description": self.description,
                "parameters": self.parameters,
            },
        }

    @abstractmethod
    async def run(self, **kwargs: Any) -> str:
        """Execute the tool and return text for the model.

        Raises:
            ToolError: On any tool-specific failure.
        """


def _clip(text: str, limit: int = MAX_SNIPPET_CHARS) -> str:
    text = " ".join(text.split())
    return text if len(text) <= limit else text[: limit - 3] + "..."


class TavilySearchTool(Tool):
    """Async web search through the Tavily API."""

    name = "web_search"
    description = (
        "Search the web for current information. Use it for recent events or facts "
        "you cannot know reliably."
    )
    parameters = {
        "type": "object",
        "properties": {"query": {"type": "string", "description": "Search query."}},
        "required": ["query"],
        "additionalProperties": False,
    }

    def __init__(self, api_key: str, client: Any | None = None, max_results: int = 3) -> None:
        if client is None:
            from tavily import AsyncTavilyClient

            client = AsyncTavilyClient(api_key=api_key)
        self._client = client
        self._max_results = max_results

    async def run(self, **kwargs: Any) -> str:
        """Search the web.

        Args:
            **kwargs: Must contain ``query`` (non-empty string).

        Raises:
            TavilySearchError: On invalid input or any Tavily failure.
        """
        query = kwargs.get("query")
        if not isinstance(query, str) or not query.strip():
            raise TavilySearchError("'query' must be a non-empty string")
        try:
            result = await self._client.search(
                query=query.strip(), max_results=self._max_results, include_answer=True
            )
        except Exception as exc:  # the Tavily SDK raises heterogeneous exception types
            logger.warning("tavily search failed: %s", type(exc).__name__)
            raise TavilySearchError(f"search failed ({type(exc).__name__})") from exc

        lines: list[str] = []
        if result.get("answer"):
            lines.append(f"Summary: {_clip(str(result['answer']))}")
        for item in result.get("results", [])[: self._max_results]:
            lines.append(
                f"- {item.get('title', 'untitled')} ({item.get('url', '')}): "
                f"{_clip(str(item.get('content', '')))}"
            )
        if not lines:
            return "No results found."
        return "\n".join(lines)


LOCAL_CORPUS: list[dict[str, str]] = [
    {
        "id": "rag",
        "title": "Que es RAG",
        "text": "RAG (Retrieval-Augmented Generation) combina recuperacion de documentos y "
        "generacion: se buscan fragmentos relevantes en una base de conocimiento y se anaden "
        "al prompt para que el modelo responda con datos actuales y verificables.",
    },
    {
        "id": "blue-green",
        "title": "Despliegue blue/green",
        "text": "En un despliegue blue/green hay dos entornos identicos. Se despliega la nueva "
        "version en el inactivo, se comprueba su salud y solo entonces se cambia el trafico. "
        "Si falla, el entorno activo sigue sirviendo y no hay caida.",
    },
    {
        "id": "quality-gate",
        "title": "Quality gate en CI",
        "text": "Un quality gate bloquea la integracion si no se cumplen umbrales: lint limpio, "
        "tipos correctos, cobertura minima del 80 por ciento y pruebas de calidad del LLM.",
    },
    {
        "id": "observability",
        "title": "Observabilidad de agentes de IA",
        "text": "Tres capas: infraestructura (CloudWatch o Prometheus), observabilidad del LLM "
        "(LangFuse: trazas, tokens, coste, puntuaciones) y seguimiento de errores (Sentry).",
    },
    {
        "id": "llm-judge",
        "title": "LLM como juez",
        "text": "Un LLM-as-a-judge puntua respuestas con una rubrica (correccion, completitud, "
        "idioma, seguridad). Debe validarse con un meta-test: una respuesta mala tiene que "
        "puntuar por debajo del umbral.",
    },
]


def _tokens(text: str) -> set[str]:
    normalized = unicodedata.normalize("NFKD", text.lower())
    stripped = "".join(c for c in normalized if not unicodedata.combining(c))
    return {t for t in re.findall(r"[a-z0-9]+", stripped) if len(t) > 2}


class RAGTool(Tool):
    """Document retrieval from a Bedrock Knowledge Base, or a small local corpus.

    With a ``kb_id`` and a ``bedrock-agent-runtime`` client it calls ``retrieve``. Without
    them it falls back to keyword scoring over :data:`LOCAL_CORPUS`, so the agent works
    (and is testable) without any AWS account.
    """

    name = "knowledge_base"
    description = (
        "Look up internal documentation about RAG, CI/CD, deployments, observability and "
        "LLM evaluation."
    )
    parameters = {
        "type": "object",
        "properties": {"query": {"type": "string", "description": "What to look up."}},
        "required": ["query"],
        "additionalProperties": False,
    }

    def __init__(
        self,
        kb_id: str | None = None,
        client: Any | None = None,
        corpus: list[dict[str, str]] | None = None,
        top_k: int = 3,
    ) -> None:
        self._kb_id = kb_id
        self._client = client
        self._corpus = corpus if corpus is not None else LOCAL_CORPUS
        self._top_k = top_k

    @property
    def backend(self) -> str:
        """Return ``bedrock`` or ``local``."""
        return "bedrock" if self._kb_id and self._client is not None else "local"

    async def run(self, **kwargs: Any) -> str:
        """Retrieve documents.

        Raises:
            RAGRetrievalError: On invalid input or a Bedrock failure.
        """
        query = kwargs.get("query")
        if not isinstance(query, str) or not query.strip():
            raise RAGRetrievalError("'query' must be a non-empty string")
        if self.backend == "bedrock":
            return await self._retrieve_bedrock(query.strip())
        return self._retrieve_local(query)

    def _retrieve_local(self, query: str) -> str:
        wanted = _tokens(query)
        scored = [
            (len(wanted & _tokens(f"{doc['title']} {doc['text']}")), doc) for doc in self._corpus
        ]
        hits = [(s, d) for s, d in sorted(scored, key=lambda x: -x[0]) if s > 0][: self._top_k]
        if not hits:
            return "No relevant documents found."
        return "\n".join(f"- {d['title']}: {d['text']}" for _, d in hits)

    async def _retrieve_bedrock(self, query: str) -> str:
        def _call() -> dict[str, Any]:
            return self._client.retrieve(  # type: ignore[union-attr]
                knowledgeBaseId=self._kb_id,
                retrievalQuery={"text": query},
                retrievalConfiguration={
                    "vectorSearchConfiguration": {"numberOfResults": self._top_k}
                },
            )

        try:
            response = await asyncio.to_thread(_call)
        except ClientError as exc:
            code = exc.response.get("Error", {}).get("Code", "ClientError")
            raise RAGRetrievalError(f"Bedrock retrieve failed ({code})") from exc
        except BotoCoreError as exc:
            raise RAGRetrievalError(f"Bedrock transport error: {type(exc).__name__}") from exc

        results = response.get("retrievalResults", [])
        if not results:
            return "No relevant documents found."
        return "\n".join(f"- {_clip(r.get('content', {}).get('text', ''))}" for r in results)


def build_tools(settings: Settings) -> list[Tool]:
    """Create the tools enabled by the configuration.

    RAG is always available (local corpus fallback); Tavily needs ``TAVILY_API_KEY``.
    """
    tools: list[Tool] = []
    if settings.tavily_api_key:
        tools.append(TavilySearchTool(settings.tavily_api_key.get_secret_value()))
    kb_client = None
    if settings.kb_id:
        import boto3

        kb_client = boto3.client("bedrock-agent-runtime", region_name=settings.aws_default_region)
    tools.append(RAGTool(settings.kb_id, kb_client))
    return tools
