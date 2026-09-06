"""Pins the service profile for the whole test session.

--- THE DEFECT THIS FIXES --------------------------------------------------

`backend/api/main.py` loads `.env` into `os.environ` at import time, by
design, so a Snowflake key never passes through a tool call. `.env` also
carries `NEULIT_PROFILE=live_no_memory`. So the moment any test imports the
app -- which happens during COLLECTION, before a single test runs -- the whole
process is relabelled as the live profile.

`registry.get_services` is an `lru_cache(maxsize=1)` that reads
`NEULIT_PROFILE` at build time. Several API test fixtures call
`get_services.cache_clear()` on teardown. The first such teardown after the
app import rebuilds the singleton on `live_no_memory`, and from that point on
every test that touches `get_services()` gets `CortexSearchRetriever` and
`CortexLLMClient` instead of the fakes.

Measured before this file existed: `pytest backend/tests` gave 15 failures
that all passed when their files were run alone, and the run opened real
network connections to Snowflake (which failed on MFA). Reproduction:
`.agent-work/c31/repro_env_leak.py`.

The failure mode matters more than the failure count. Without credentials
`CortexSearchRetriever.search` logs a warning and returns `[]` -- it does not
raise. So the visible symptom of a silently flipped profile is an answer built
on zero retrieved records, which is exactly the ungrounded answer the
grounding checks in `backend/app/verify/grounding.py` exist to refuse.

--- WHY THE FIX IS HERE ----------------------------------------------------

`backend/api/main.py` is FROZEN (plan-v2/00-SHARED-CONTRACTS.md section 3) and
the `.env` load is deliberate production behaviour, not a bug in itself. What
must not happen is a TEST process inheriting it. A conftest at the tests root
is imported before any test module, so setting the variable outright here
turns main.py's `setdefault` into a no-op and the profile can no longer move
under a running suite.

`backend/tests/snowflake/conftest.py` already reached the same conclusion
about the SNOWFLAKE_* half of the same `.env` load and gated live tests behind
an explicit marker. This is the `NEULIT_PROFILE` half of that leak.

A run that genuinely wants a live profile sets it in the environment, which
this respects.
"""
from __future__ import annotations

import os

import pytest

#: Set before any test module is imported, and therefore before
#: `backend.api.main` gets a chance to read `.env`.
os.environ.setdefault("NEULIT_PROFILE", "fake")
_PINNED_PROFILE = os.environ["NEULIT_PROFILE"]


@pytest.fixture(autouse=True)
def _profile_stays_pinned():
    """Restores the profile after any test that moved it, and drops the
    service singleton if it did.

    A test is free to point the profile somewhere else for its own duration;
    what it may not do is leave it there. Clearing the cache on the way out
    only when the value actually changed keeps the singleton's identity intact
    for the tests that assert on it.
    """
    before = os.environ.get("NEULIT_PROFILE")
    yield
    after = os.environ.get("NEULIT_PROFILE")
    if after != before:
        from backend.contracts.registry import get_services

        if before is None:
            os.environ.pop("NEULIT_PROFILE", None)
        else:
            os.environ["NEULIT_PROFILE"] = before
        get_services.cache_clear()
