"""Cost math against NEULIT.CORE.MODEL_PRICING.

Kept separate from backend/snowflake/llm.py so the arithmetic (and its
missing-model edge case) can be unit tested with plain dicts, no Snowpark
session required.
"""
from __future__ import annotations

import logging
from dataclasses import dataclass

logger = logging.getLogger("neulit.snowflake.pricing")


@dataclass(frozen=True)
class ModelPriceRow:
    model: str
    credits_per_mtok_in: float
    credits_per_mtok_out: float
    usd_per_credit: float


def compute_cost_usd(
    prompt_tokens: int,
    completion_tokens: int,
    model: str,
    pricing: dict[str, ModelPriceRow],
) -> float:
    """cost_usd = (prompt/1e6)*credits_in*usd_per_credit
                + (completion/1e6)*credits_out*usd_per_credit

    Returns 0.0 and logs an error if `model` is missing from `pricing` -
    never guesses a price.
    """
    row = pricing.get(model)
    if row is None:
        logger.error("pricing: model %r missing from MODEL_PRICING, cost_usd=0", model)
        return 0.0

    cost = (prompt_tokens / 1_000_000) * row.credits_per_mtok_in * row.usd_per_credit
    cost += (completion_tokens / 1_000_000) * row.credits_per_mtok_out * row.usd_per_credit
    return round(cost, 8)
