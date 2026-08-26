"""Tier 1 - credential-free. Pricing math against hand-computed values."""
from backend.app.llm.pricing import ModelPriceRow, compute_cost_usd


def test_cost_math_hand_computed():
    pricing = {
        "claude-3-5-sonnet": ModelPriceRow(
            model="claude-3-5-sonnet",
            credits_per_mtok_in=3.0,
            credits_per_mtok_out=15.0,
            usd_per_credit=2.0,
        )
    }
    # 1,000,000 prompt tokens * 3 credits/mtok * $2/credit = $6.00
    # 1,000,000 completion tokens * 15 credits/mtok * $2/credit = $30.00
    cost = compute_cost_usd(1_000_000, 1_000_000, "claude-3-5-sonnet", pricing)
    assert cost == 36.0


def test_cost_math_small_counts():
    pricing = {
        "claude-3-5-sonnet": ModelPriceRow(
            model="claude-3-5-sonnet",
            credits_per_mtok_in=3.0,
            credits_per_mtok_out=15.0,
            usd_per_credit=2.0,
        )
    }
    # 500 prompt + 100 completion tokens
    expected = round((500 / 1_000_000) * 3.0 * 2.0 + (100 / 1_000_000) * 15.0 * 2.0, 8)
    cost = compute_cost_usd(500, 100, "claude-3-5-sonnet", pricing)
    assert cost == expected


def test_cost_math_missing_model_yields_zero(caplog):
    cost = compute_cost_usd(1000, 1000, "unknown-model", {})
    assert cost == 0.0


def test_cost_math_zero_tokens():
    pricing = {
        "m": ModelPriceRow(model="m", credits_per_mtok_in=1.0, credits_per_mtok_out=1.0, usd_per_credit=1.0)
    }
    assert compute_cost_usd(0, 0, "m", pricing) == 0.0
