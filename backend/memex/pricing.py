"""Snowflake Cortex credit rates - the pricing oracle.

Every number MemEx puts on screen is a measured token count multiplied by a
rate from this table (published, citable). Nothing is invented and nothing is
estimated unless it says so.

--- SOURCE -----------------------------------------------------------------
Snowflake Service Consumption Table, **Effective: July 31, 2026**
  https://www.snowflake.com/legal-files/CreditConsumptionTable.pdf
  Table 6(a) - "Snowflake AI Features Table, Cortex AI Functions"
  rows "AI_COMPLETE - <model>", columns Input / Output.
Read from the PDF on 2026-08-07 (build day) - the rates below are transcribed
from that table, not recalled.

The docs page (docs.snowflake.com/en/user-guide/snowflake-cortex/aisql-cost)
carries no numbers of its own; it points at the PDF above and states that
**both input and output tokens are billable** for AI_COMPLETE. Hence the split
rates here rather than one blended figure.

Units: AI credits per ONE MILLION tokens.

Deliberately NOT sourced from ACCOUNT_USAGE / CORTEX_FUNCTIONS_QUERY_USAGE_
HISTORY: those views lag ~5 min to 3 hrs. They are the receipt, never the live
meter.

NOTE: Table 6(a) is the AI-**functions** table, which is what AI_COMPLETE over
the SQL API bills against. Tables 6(b)-6(e) are lower but they price different
products (prompt caching, Cortex Agents, CoWork, CoCo) - quoting those for a
plain AI_COMPLETE call would understate the cold path and flatter our own
number. Use 6(a).

Python port of scaffold/lib/pricing.ts. Behaviour is identical; the only
addition is `usage_for()`, which emits a dict shaped like the frozen
`backend.contracts.models.TokenUsage` so priced calls can cross the port
boundary without this module importing the contracts package.
"""
from __future__ import annotations

import os
from dataclasses import dataclass
from typing import Literal

# ---------------------------------------------------------------------------
# The table
# ---------------------------------------------------------------------------

ModelAvailability = Literal["native", "cross-region"]
TokenKind = Literal["input", "output"]


@dataclass(frozen=True)
class CreditRate:
    """AI credits per 1M tokens, split by direction."""

    input: float
    output: float
    availability: ModelAvailability
    #: Anything that qualifies the number - promo pricing, preview status.
    note: str | None = None


#: AI_COMPLETE rates, Table 6(a).
#:
#: Scope: the models a US-West-2 trial can plausibly call today - the natives,
#: plus the cross-region premium tier that CORTEX_ENABLED_CROSS_REGION unlocks.
#: `native` works out of the box. `cross-region` needs, once, as ACCOUNTADMIN:
#:     ALTER ACCOUNT SET CORTEX_ENABLED_CROSS_REGION = 'ANY_REGION';
#: Adding a row is transcription from the PDF, not a judgement call.
CREDIT_RATES: dict[str, CreditRate] = {
    # -- Native in AWS US West 2 - no cross-region flag needed ---------------
    "llama3.1-8b": CreditRate(input=0.132, output=0.132, availability="native"),
    "llama3.1-70b": CreditRate(input=0.432, output=0.432, availability="native"),
    "llama3.3-70b": CreditRate(input=0.432, output=0.432, availability="native"),
    "llama4-maverick": CreditRate(input=0.144, output=0.582, availability="native"),
    "mistral-7b": CreditRate(input=0.09, output=0.12, availability="native"),
    "mixtral-8x7b": CreditRate(input=0.27, output=0.42, availability="native"),
    "mistral-large2": CreditRate(input=1.2, output=3.6, availability="native"),
    "openai-gpt-4.1": CreditRate(input=1.2, output=4.8, availability="native"),
    # -- Cross-region: the premium tier the cold path burns ------------------
    "claude-opus-5": CreditRate(input=3.0, output=15.0, availability="cross-region"),
    "claude-sonnet-5": CreditRate(
        input=1.2,
        output=6.0,
        availability="cross-region",
        # Table 6(a) footnote 19.
        note="promotional pricing - Snowflake states prices rise 50% on 2026-09-01",
    ),
    "claude-sonnet-4-5": CreditRate(input=1.8, output=9.0, availability="cross-region"),
    "claude-haiku-4-5": CreditRate(input=0.6, output=3.0, availability="cross-region"),
    "gemini-3.1-pro": CreditRate(input=1.2, output=7.2, availability="cross-region"),
}

#: Rate used for a model that is not in the table.
#:
#: Never silently applied: everything that consumes it carries `published=False`
#: so the UI can label the number "estimated". An honest estimate beats fake
#: precision, and a demo that throws because someone typed a new model name is
#: worse than either.
FALLBACK_RATE = CreditRate(
    input=0.432,
    output=0.432,
    availability="native",
    note="not in the published table - llama3.3-70b's rate used as a stand-in",
)

FALLBACK_MODEL = "llama3.3-70b"


# ---------------------------------------------------------------------------
# Model selection
# ---------------------------------------------------------------------------

#: The cold path: premium model, full transcript stuffed in.
DEFAULT_COLD_MODEL = "claude-opus-5"
#: The warm path: cheapest model that can still answer from a compact digest.
DEFAULT_WARM_MODEL = "llama3.1-8b"


def cold_model() -> str:
    """Premium model for the cold path.

    Env-overridable so the key-arrival drill is a `.env` edit, never a code
    edit. If CORTEX_ENABLED_CROSS_REGION turns out to be blocked, set
    MEMEX_COLD_MODEL=mistral-large2 - still a 9.1x rate gap over the warm path,
    so the story survives intact.
    """
    return os.environ.get("MEMEX_COLD_MODEL") or DEFAULT_COLD_MODEL


def warm_model() -> str:
    """Cheapest model that can still answer from a compact digest."""
    return os.environ.get("MEMEX_WARM_MODEL") or DEFAULT_WARM_MODEL


def usd_per_credit() -> float:
    """USD per Snowflake credit.

    Region- and edition-dependent. Service Consumption Table 2(a), AWS US West
    (Oregon): Standard $2.00, Enterprise $3.00, Business Critical $4.00.
    Default matches Enterprise, what a 30-day trial provisions.

    Credits are the honest unit - dollars are the legible one. Show both, and
    label the dollar figure as depending on edition if anyone asks.
    """
    try:
        raw = float(os.environ.get("SNOWFLAKE_CREDIT_RATE_USD", "3.00"))
    except (TypeError, ValueError):
        return 3.0
    return raw if raw > 0 else 3.0


# ---------------------------------------------------------------------------
# Pricing
# ---------------------------------------------------------------------------


def rate_for(model: str) -> CreditRate | None:
    """Published rate for `model`, or None when it is not in the table."""
    return CREDIT_RATES.get(model)


def is_published(model: str) -> bool:
    """True when `model` has a published rate rather than the stand-in."""
    return model in CREDIT_RATES


def cost_for(tokens: int, model: str, kind: TokenKind = "input") -> float:
    """Credits consumed by `tokens` tokens on `model`.

    Defaults to the INPUT rate: token counting measures the prompt, and the
    prompt is what memory actually displaces. Pass "output" for completions.
    """
    rate = rate_for(model) or FALLBACK_RATE
    per_mtok = rate.input if kind == "input" else rate.output
    return (tokens / 1_000_000) * per_mtok


def usd_for(credits: float) -> float:
    """Convert credits to dollars at the configured edition rate."""
    return credits * usd_per_credit()


@dataclass(frozen=True)
class PriceBreakdown:
    """One call priced end to end, with the rate it was priced at attached."""

    model: str
    tokens_in: int
    tokens_out: int
    #: Credits for the prompt - the part memory displaces.
    credits_in: float
    #: Credits for the completion.
    credits_out: float
    credits_total: float
    usd_total: float
    rate: CreditRate
    #: False when `rate` is the stand-in, i.e. label the number "estimated".
    published: bool

    def as_dict(self) -> dict:
        """JSON-safe projection for API responses."""
        return {
            "model": self.model,
            "tokens_in": self.tokens_in,
            "tokens_out": self.tokens_out,
            "credits_in": self.credits_in,
            "credits_out": self.credits_out,
            "credits_total": self.credits_total,
            "usd_total": self.usd_total,
            "published": self.published,
            "rate": {
                "input_per_mtok": self.rate.input,
                "output_per_mtok": self.rate.output,
                "availability": self.rate.availability,
                "note": self.rate.note,
            },
        }


def price_call(model: str, tokens_in: int, tokens_out: int = 0) -> PriceBreakdown:
    """Price a single AI_COMPLETE call against Table 6(a)."""
    rate = rate_for(model) or FALLBACK_RATE
    credits_in = (tokens_in / 1_000_000) * rate.input
    credits_out = (tokens_out / 1_000_000) * rate.output
    credits_total = credits_in + credits_out
    return PriceBreakdown(
        model=model,
        tokens_in=tokens_in,
        tokens_out=tokens_out,
        credits_in=credits_in,
        credits_out=credits_out,
        credits_total=credits_total,
        usd_total=usd_for(credits_total),
        rate=rate,
        published=is_published(model),
    )


def usage_for(model: str, prompt_tokens: int, completion_tokens: int) -> dict:
    """A `TokenUsage`-shaped dict priced against the published Cortex table.

    Keys match `backend.contracts.models.TokenUsage` field-for-field, so a
    caller can do `TokenUsage(**usage_for(...))`. Kept as a dict here so this
    module stays importable with zero dependency on the contracts package.

    This is the function that makes the cost story real on the *fake* profile:
    FakeLLM reports a flat invented `cost_usd`, but it reports honest token
    counts. We keep its counts and re-price them at Snowflake's published rate.
    """
    breakdown = price_call(model, prompt_tokens, completion_tokens)
    return {
        "prompt_tokens": prompt_tokens,
        "completion_tokens": completion_tokens,
        "total_tokens": prompt_tokens + completion_tokens,
        "model": model,
        "cost_usd": round(breakdown.usd_total, 8),
    }


def savings_percent(cold_credits: float, warm_credits: float) -> float:
    """What the warm path saved against the cold path, as a percentage.

    Returns 0 rather than a division blow-up when cold is zero - a demo banner
    reading "NaN% saved" is worse than one reading "0%".
    """
    if not cold_credits > 0:
        return 0.0
    return ((cold_credits - warm_credits) / cold_credits) * 100.0


def cost_ratio(cold_credits: float, warm_credits: float) -> float:
    """"1/40th the price" - the shape the demo script actually says out loud."""
    if not warm_credits > 0:
        return 0.0
    return cold_credits / warm_credits


# ---------------------------------------------------------------------------
# Formatting - credits are tiny numbers, and tiny numbers format badly
# ---------------------------------------------------------------------------


def format_credits(credits: float) -> str:
    """Credits, at enough precision to be non-zero on a single call."""
    if credits == 0:
        return "0"
    if credits < 0.000001:
        return f"{credits:.2e}"
    return f"{credits:.6f}"


def format_usd(usd: float) -> str:
    """Dollars. Sub-cent amounts keep their significant digits instead of $0.00."""
    if usd == 0:
        return "$0.00"
    if usd < 0.01:
        return f"${usd:.6f}"
    return f"${usd:.2f}"
