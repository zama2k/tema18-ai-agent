"""Model price table and cost calculation.

Prices are USD per 1,000,000 tokens (input, output). They are list prices and drift over
time: override them without code changes through ``PRICE_OVERRIDES`` (JSON, e.g.
``{"gpt-5.6-sol": [4.0, 20.0]}``) when your contract differs.
"""

from __future__ import annotations

import json
import os
from dataclasses import dataclass

PRICES_AS_OF = "2026-10-27"


@dataclass(frozen=True)
class ModelPrice:
    """Price of a model in USD per million tokens."""

    input_per_million: float
    output_per_million: float
    source: str
    estimate: bool = False


# Longest matching key wins, so "claude-3-haiku" is preferred over the "haiku" tier alias.
PRICE_TABLE: dict[str, ModelPrice] = {
    "gpt-4o-mini": ModelPrice(
        0.15, 0.60, "OpenAI pricing page (developers.openai.com/api/docs/pricing), 2026-10-27"
    ),
    "gpt-5.6-sol": ModelPrice(
        4.00,
        20.00,
        "OpenAI list price, standard short context, 2026-10-27; Azure may bill differently",
        estimate=True,
    ),
    "claude-3-haiku": ModelPrice(
        0.25, 1.25, "Anthropic list price for Claude 3 Haiku (Bedrock on-demand not re-verified)"
    ),
    "claude-3-sonnet": ModelPrice(3.00, 15.00, "Anthropic list price for Claude 3 Sonnet"),
    "claude-3-opus": ModelPrice(15.00, 75.00, "Anthropic list price for Claude 3 Opus"),
}
TIER_ALIASES: dict[str, str] = {
    "haiku": "claude-3-haiku",
    "sonnet": "claude-3-sonnet",
    "opus": "claude-3-opus",
}


class UnknownModelPriceError(ValueError):
    """Raised when no price is known for a model."""


def _overrides() -> dict[str, ModelPrice]:
    raw = os.environ.get("PRICE_OVERRIDES")
    if not raw:
        return {}
    try:
        data = json.loads(raw)
        return {
            str(name).lower(): ModelPrice(float(v[0]), float(v[1]), "PRICE_OVERRIDES")
            for name, v in data.items()
        }
    except (ValueError, TypeError, IndexError, AttributeError) as exc:
        raise ValueError('PRICE_OVERRIDES must be JSON like {"model": [input, output]}') from exc


def get_price(model: str) -> ModelPrice:
    """Resolve the price of ``model`` (substring match, overrides first).

    Raises:
        UnknownModelPriceError: If the model is not in the table.
    """
    name = model.lower()
    table = {**PRICE_TABLE, **{k: v for k, v in _overrides().items()}}
    for key in sorted(table, key=len, reverse=True):
        if key in name:
            return table[key]
    for alias, key in TIER_ALIASES.items():
        if alias in name:
            return table[key]
    raise UnknownModelPriceError(f"no price known for model {model!r}")


def calculate_cost(model: str, input_tokens: int, output_tokens: int) -> float:
    """Return the cost in USD of a call.

    Args:
        model: Model or deployment name (for example ``gpt-4o-mini`` or ``haiku``).
        input_tokens: Prompt tokens.
        output_tokens: Completion tokens (reasoning tokens included when billed as output).

    Raises:
        ValueError: If a token count is negative or the model has no known price.
    """
    if input_tokens < 0 or output_tokens < 0:
        raise ValueError("token counts must be non-negative")
    price = get_price(model)
    return (
        input_tokens * price.input_per_million + output_tokens * price.output_per_million
    ) / 1_000_000
