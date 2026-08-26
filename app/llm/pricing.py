"""Cost estimation per model.

Prices are USD per 1M tokens (input, output). Kept as data, not code, so a
new model or price change is a one-line edit. ``None`` cost means "unknown
model" - we surface that honestly instead of guessing.
"""

from app.llm.base import TokenUsage

# model -> (usd_per_1m_input, usd_per_1m_output)
PRICING_USD_PER_1M: dict[str, tuple[float, float]] = {
    "gpt-4o": (2.50, 10.00),
    "gpt-4o-mini": (0.15, 0.60),
    "gpt-4.1": (2.00, 8.00),
    "gpt-4.1-mini": (0.40, 1.60),
    "gpt-5.6-terra": (2.00, 12.00),
    "claude-sonnet-5": (3.00, 15.00),
    "gemini-3.6-flash": (1.50, 7.50),
    "text-embedding-3-small": (0.02, 0.0),
    "text-embedding-3-large": (0.13, 0.0),
}


def estimate_cost_usd(model: str, usage: TokenUsage) -> float | None:
    """Estimate call cost; returns None for unknown models."""
    prices = PRICING_USD_PER_1M.get(model)
    if prices is None:
        return None
    input_price, output_price = prices
    return (usage.prompt_tokens * input_price + usage.completion_tokens * output_price) / 1_000_000
