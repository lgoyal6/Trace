"""Phase E - call-site model routing.

Maps each CallSite to a model tier -- "cheap" for binary-gate /
query-expansion calls that are quality-tolerant, "strong" for calls where
output quality matters most (the thing a user or a citation check actually
reads). This is purely a model-selection concern; the existing
`MODEL_PRICING` lookup (backend/app/llm/pricing.py) already keys by model
name, so once routing picks a model, cost math downstream needs no changes
-- it just naturally prices whichever model was actually used.

Tier assignment (phase card Phase E):
  cheap  -> relevance_check, hyde     (binary gate / query expansion --
            wrong-but-cheap is an acceptable failure mode; a bad hyde
            expansion or a wrong relevance gate just costs a retrieval
            round-trip, not a wrong answer shown to the user)
  strong -> summary, citation_check, refine, memory_distill
            (summary and citation_check are the text a user/citation
            verifier directly reads; refine rewrites the user's own query,
            visible to them; memory_distill produces a persisted EverOS
            profile summary that gets reused across sessions -- all four
            are worth paying for quality on)

Model selection, in priority order:
  1. SNOWFLAKE_CORTEX_MODEL_CHEAP / SNOWFLAKE_CORTEX_MODEL_STRONG, if set.
  2. SNOWFLAKE_CORTEX_MODEL (the pre-existing single-model env var), used
     for BOTH tiers if set and the tier-specific vars are not -- preserves
     backward compatibility with the single-model config already documented
     in .env.example / config/snowflake.yaml, and with Decisions.md's
     recorded contingency plan for region-unavailable models (which only
     ever sets SNOWFLAKE_CORTEX_MODEL).
  3. Hardcoded per-tier defaults (below), if none of the above are set.

Default model choice, documented:
  - Strong tier default: `claude-3-5-sonnet`, matching the existing
    `_DEFAULT_MODEL` in backend/snowflake/llm.py so routing is a no-op for
    the strong tier out of the box -- no behavior change for anyone not
    opting into routing via env vars.
  - Cheap tier default: `mistral-7b`, a small, low-cost model already
    available on Snowflake Cortex COMPLETE
    (https://docs.snowflake.com/en/user-guide/snowflake-cortex/llm-functions
    lists it among Cortex's supported models) and cheap enough to be a
    sensible default for high-volume, quality-tolerant gate/expansion
    calls. No published AI-Credit rate for `mistral-7b` was found in this
    pass (see the MODEL_PRICING seed note in snowflake/sql/02_tables.sql
    for the same caveat already applied to claude-3-5-sonnet /
    claude-sonnet-4-5) -- a placeholder rate is seeded there, clearly
    labeled, and must be reconciled against the account's real Service
    Consumption Table once live credentials exist (same as the other rows).
"""
from __future__ import annotations

import os

from backend.contracts.models import CallSite

_DEFAULT_STRONG_MODEL = "claude-3-5-sonnet"
_DEFAULT_CHEAP_MODEL = "mistral-7b"

CHEAP_CALL_SITES: frozenset[str] = frozenset({"relevance_check", "hyde"})
STRONG_CALL_SITES: frozenset[str] = frozenset(
    {"summary", "citation_check", "refine", "memory_distill"}
)


def _tier_for_call_site(call_site: str) -> str:
    """Returns "cheap" or "strong". Unknown call sites (should not happen --
    CallSite is a closed Literal) fall back to "strong", the safer default.
    """
    if call_site in CHEAP_CALL_SITES:
        return "cheap"
    return "strong"


def model_for_call_site(call_site: CallSite) -> str:
    """Resolves the model name to use for `call_site`, per the priority
    order documented in this module's docstring.
    """
    single = os.environ.get("SNOWFLAKE_CORTEX_MODEL")
    tier = _tier_for_call_site(call_site)

    if tier == "cheap":
        override = os.environ.get("SNOWFLAKE_CORTEX_MODEL_CHEAP")
        if override:
            return override
        if single:
            return single
        return _DEFAULT_CHEAP_MODEL

    override = os.environ.get("SNOWFLAKE_CORTEX_MODEL_STRONG")
    if override:
        return override
    if single:
        return single
    return _DEFAULT_STRONG_MODEL
