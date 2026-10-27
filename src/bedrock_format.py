"""Pure helpers for the Anthropic-on-Bedrock request/response format."""

from __future__ import annotations

import json
from typing import Any

from src.models import LLMResponse, Message, TokenUsage, ToolCall, ToolSchema

ANTHROPIC_VERSION = "bedrock-2023-05-31"


def parse_bedrock_response(response: dict[str, Any]) -> LLMResponse:
    """Parse a decoded Bedrock (Anthropic Messages) response body.

    Expected shape::

        {"content": [{"type": "text", "text": "..."}],
         "usage": {"input_tokens": 12, "output_tokens": 34}}

    ``tool_use`` blocks are returned as tool calls.

    Args:
        response: Decoded JSON body.

    Returns:
        The normalised response.

    Raises:
        ValueError: If the body is malformed or carries neither text nor tool calls.
    """
    if not isinstance(response, dict):
        raise ValueError("Bedrock response must be a JSON object")
    content = response.get("content")
    if not isinstance(content, list) or not content:
        raise ValueError("Bedrock response has empty or missing 'content'")

    texts: list[str] = []
    tool_calls: list[ToolCall] = []
    for block in content:
        if not isinstance(block, dict) or "type" not in block:
            raise ValueError("Bedrock response contains a malformed content block")
        if block["type"] == "text":
            text = block.get("text")
            if not isinstance(text, str):
                raise ValueError("Bedrock text block has no string 'text'")
            texts.append(text)
        elif block["type"] == "tool_use":
            args = block.get("input")
            if not isinstance(block.get("id"), str) or not isinstance(block.get("name"), str):
                raise ValueError("Bedrock tool_use block lacks 'id' or 'name'")
            tool_calls.append(
                ToolCall(
                    id=block["id"],
                    name=block["name"],
                    arguments=args if isinstance(args, dict) else None,
                    raw_arguments=json.dumps(args) if args is not None else "",
                )
            )

    text_joined = "".join(texts).strip()
    if not text_joined and not tool_calls:
        raise ValueError("Bedrock response has no text and no tool calls")

    usage = response.get("usage")
    if (
        not isinstance(usage, dict)
        or not isinstance(usage.get("input_tokens"), int)
        or not isinstance(usage.get("output_tokens"), int)
    ):
        raise ValueError("Bedrock response has missing or malformed 'usage'")

    return LLMResponse(
        text=text_joined,
        tool_calls=tool_calls,
        usage=TokenUsage(usage["input_tokens"], usage["output_tokens"]),
        finish_reason=response.get("stop_reason"),
    )


def build_bedrock_request(
    messages: list[Message], tools: list[ToolSchema] | None, max_tokens: int, temperature: float
) -> dict[str, Any]:
    """Convert OpenAI-style chat messages and tool schemas to a Bedrock request body.

    System messages become the top-level ``system`` field; assistant tool calls become
    ``tool_use`` blocks and ``tool`` messages become ``tool_result`` blocks of a user turn.
    """
    system_parts: list[str] = []
    converted: list[dict[str, Any]] = []
    for msg in messages:
        role = msg["role"]
        if role == "system":
            system_parts.append(str(msg["content"]))
        elif role == "user":
            _append(converted, "user", [{"type": "text", "text": str(msg["content"])}])
        elif role == "assistant":
            blocks: list[dict[str, Any]] = []
            if msg.get("content"):
                blocks.append({"type": "text", "text": str(msg["content"])})
            for call in msg.get("tool_calls") or []:
                fn = call["function"]
                blocks.append(
                    {
                        "type": "tool_use",
                        "id": call["id"],
                        "name": fn["name"],
                        "input": json.loads(fn.get("arguments") or "{}"),
                    }
                )
            _append(converted, "assistant", blocks)
        elif role == "tool":
            _append(
                converted,
                "user",
                [
                    {
                        "type": "tool_result",
                        "tool_use_id": msg["tool_call_id"],
                        "content": str(msg["content"]),
                    }
                ],
            )
        else:
            raise ValueError(f"unsupported message role: {role!r}")

    body: dict[str, Any] = {
        "anthropic_version": ANTHROPIC_VERSION,
        "max_tokens": max_tokens,
        "temperature": temperature,
        "messages": converted,
    }
    if system_parts:
        body["system"] = "\n\n".join(system_parts)
    if tools:
        body["tools"] = [
            {
                "name": t["function"]["name"],
                "description": t["function"].get("description", ""),
                "input_schema": t["function"].get("parameters", {"type": "object"}),
            }
            for t in tools
        ]
    return body


def _append(converted: list[dict[str, Any]], role: str, blocks: list[dict[str, Any]]) -> None:
    """Append blocks, merging consecutive turns of the same role (Bedrock requires alternation)."""
    if converted and converted[-1]["role"] == role:
        converted[-1]["content"].extend(blocks)
    else:
        converted.append({"role": role, "content": list(blocks)})
