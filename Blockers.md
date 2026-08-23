# Blockers

Append-only. "I need a change in a file I don't own." Names the file, the
change, and the owner. Owner makes the change, replies in-line, deletes the
entry.

## [c1] backend/api/dependencies.py still imports deleted v1 modules - RESOLVED 2026-08-07

Applied the suggested fix below directly (solo project, no separate Card 2A
owner to hand this to). Also had to apply the same import-and-rename fix to
`backend/api/routes/query.py`, which independently imported
`ParitokLLMClient`/`HybridRetriever` for type hints only (now `LLMPort`/
`RetrievalPort` from `backend.contracts.ports`), and fix
`retriever.get_closest_conditions(...)` → `retriever.closest_conditions(...)`
(v1/v2 method name mismatch) plus a `ConditionMatch` unpacking bug (v2
returns dataclass instances, code was unpacking as 3-tuples). App now
imports cleanly under `NEULIT_PROFILE=live_no_memory`. See the new blocker
below for what's still broken deeper in the pipeline.

<details><summary>Original entry</summary>

**File:** `backend/api/dependencies.py` (Card 2A ownership)

**Problem:** `dependencies.py` still does, at module scope:

```python
from backend.app.llm_client import ParitokLLMClient
from backend.app.retrieval.hybrid import HybridRetriever
```

Both `backend/app/llm_client.py` and `backend/app/retrieval/hybrid.py` were
deleted at the freeze commit / by Card 1 respectively (freeze §2.1 deletes
`llm_client.py`; Card 1's phase card §3.4 deletes `hybrid.py`,
`bm25_index.py`, `vector_index.py`). Because `backend/api/routes/demo.py`
imports `get_demo_contrast` from `dependencies.py`, and `backend/api/main.py`
(FROZEN) includes the demo router, **the entire FastAPI app currently fails
to import** under `NEULIT_PROFILE=fake` with zero credentials - i.e. `from
backend.api.main import app` raises `ModuleNotFoundError` right now on
branch-1.

**Re-confirmed on branch-1 at commit `68a3ca2`** with a fresh run (no
credentials, `.venv-c1`):

```
NEULIT_PROFILE=fake .venv-c1/Scripts/python -c "from backend.api.main import app"
```

exact current traceback:

```
Traceback (most recent call last):
  File "<string>", line 1, in <module>
    from backend.api.main import app
  File "C:\Users\vivaa\idk-card1\backend\api\main.py", line 32, in <module>
    from backend.api.routes.demo import router as demo_router  # noqa: E402
    ^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^
  File "C:\Users\vivaa\idk-card1\backend\api\routes\demo.py", line 8, in <module>
    from backend.api.dependencies import get_demo_contrast
  File "C:\Users\vivaa\idk-card1\backend\api\dependencies.py", line 12, in <module>
    from backend.app.llm_client import ParitokLLMClient
ModuleNotFoundError: No module named 'backend.app.llm_client'
```

Still unresolved as of this re-check - no changes made to `dependencies.py`
(outside Card 1 ownership).

**Requested change:** `dependencies.py`'s `get_llm_client()` /
`get_retriever()` should resolve `LLMPort` / `RetrievalPort` implementations
via `backend.contracts.registry.get_services()` (as the ownership doc
intends - Card 1 builds the ports, Card 2A's app wires them through
`get_services()`), not import the deleted v1 classes directly.

**Workaround applied so Card 1's own test suite stays green in the
meantime:** `backend/tests/test_api_conditions.py` now mounts
`backend.api.routes.conditions.router` on a standalone `FastAPI()` instance
instead of importing `backend.api.main:app`. This is a workaround, not a
fix - the underlying app-level import failure is still present and will
affect any other lane's test that imports `backend.api.main`.

**Owner:** Card 2A. Not resolved by Card 1 because `backend/api/dependencies.py`
is outside Card 1's ownership bucket (plan-v2/00-SHARED-CONTRACTS.md §3).

**Copy-pasteable fix suggestion** (current `dependencies.py` lines 12-27,
verified against this branch-1 commit): replace the two stale imports and
the two functions that construct the deleted classes directly with calls
into `backend.contracts.registry.get_services()`, which already resolves
`RetrievalPort` / `LLMPort` implementations for the active `NEULIT_PROFILE`
(see `backend/contracts/registry.py`, FROZEN, and `config/services.yaml`
which maps `fake` -> Card 1's fake/stub implementations, `live_no_memory` /
`live` -> `CortexSearchRetriever` / `CortexLLMClient`):

```python
# remove:
# from backend.app.llm_client import ParitokLLMClient
# from backend.app.retrieval.hybrid import HybridRetriever

from backend.contracts.registry import get_services
from backend.contracts.ports import LLMPort, RetrievalPort

def get_llm_client() -> LLMPort:
    return get_services().llm

def get_retriever() -> RetrievalPort:
    return get_services().retrieval
```

`run_demo_contrast` (from `backend.app.retrieval.demo_fixture`, Card 1's
file, still present and unaffected) can keep calling whatever it currently
calls `get_retriever()` for - the return type is now the `RetrievalPort`
Protocol rather than the concrete deleted `HybridRetriever`, so any call
site that only uses `.search()` / `.closest_conditions()` / `.get_by_pmids()`
/ `.health()` (the Protocol's methods) needs no further change.

</details>

## [c1] client.chat() call sites across the search/summary/verify pipeline don't match LLMPort - RESOLVED 2026-08-07

**Files:** `backend/app/loop/refine.py` (lines 37, 98), `backend/app/summary/generate.py`
(line 88), `backend/app/verify/citation_check.py` (lines 141, 253) - all
outside Card 1's ownership bucket (`backend/app/loop/`, `backend/app/summary/`,
`backend/app/verify/` are explicitly excluded in
`plan-v2/01-PHASE-CARD-1-snowflake-platform.md` §0).

**Problem:** every one of these calls `client.chat(messages, ...)` with v1
`ParitokLLMClient` kwargs - `response_format={"type": "json_object"}`,
`direct=True`, or no kwargs at all beyond `messages`. None of these match
`LLMPort.chat()`'s actual signature (`backend/contracts/ports.py`, FROZEN):

```python
def chat(self, messages, *, call_site: CallSite, request_id: str,
         session_id: str, user_id: str, json_schema: dict | None = None,
         max_output_tokens: int = 1024) -> ChatResult: ...
```

`call_site`, `request_id`, `session_id`, and `user_id` are keyword-only with
no defaults. Every one of these call sites will raise `TypeError: missing
required keyword-only argument` the moment `POST /query` actually executes
end-to-end against `CortexLLMClient` - confirmed by reading the call sites,
not yet hit live because triggering `/query` requires a full HTTP request
through `run_search_loop`, which I did not run given the scope below.

**Why this isn't a quick fix:** it's not a rename. `request_id`/`session_id`/
`user_id` need to be threaded from the HTTP request (`backend/api/routes/query.py`'s
`query()` handler, which does have `request: Request` available) down through
`run_search_loop(client, retriever, ...)` → `refine.py`'s internal calls →
`generate_sourced_summary(...)` → `check_citations`/`check_differential(...)`.
That's a signature change across every function in the call chain, plus a
decision on what `call_site` value each of the ~6 call sites should pass
(the `CallSite` literal already enumerates the right names: `hyde`,
`relevance_check`, `refine`, `summary`, `citation_check` - matching 1:1 to
call sites in `refine.py`/`generate.py`/`citation_check.py`, which is a good
sign the original design anticipated this, but the plumbing itself isn't
written). `response_format=`/`direct=True` also need remapping to
`json_schema=`.

**Current status:** Snowflake connectivity itself (search, LLM, ledger) is
fully verified live and working in isolation - see `Handoff-Log.md`. This
blocker is why a full `POST /query` HTTP round-trip has not been verified
end-to-end; `/economics/*` and `/conditions` do not depend on this pipeline
and are unaffected.

**Owner:** whoever owns `backend/app/loop/`, `backend/app/summary/`,
`backend/app/verify/` (Card 2A per the original card split). On a solo
project, this is the next piece of work after Snowflake connectivity  - 
budget it as a real task, not a quick patch.

**Resolution (solo project, applied directly):** threaded `request_id`/
`session_id`/`user_id` from `backend/api/routes/query.py`'s route handlers
(new `_request_scoped_ids()` helper - generates a UUID, honors optional
`X-Session-Id`/`X-User-Id` headers, no auth system exists in
`live_no_memory` so a fresh anonymous identity per request is the
reasonable default) down through `run_search_loop` → `hyde.py`/
`relevance_check.py`/`refine.py`'s inline calls → `generate_sourced_summary`
→ `check_citations`/`check_differential`. Mapped each call site to the
matching `CallSite` literal (`hyde`, `relevance_check`, `refine`, `summary`,
`citation_check` for both citation and differential checks - no separate
literal exists for differential, and it's the same verification stage
conceptually). Dropped the v1 `response_format=`/`direct=` kwargs (`LLMPort`
has no such params).

Three more bugs surfaced only once the pipeline actually ran against a live
model (never true before this pass):

1. **`retriever.search()` in `refine.py`** returns `list[ScoredPaper]`
   (frozen dataclasses), but the code unpacked it as `[p for p, _ in
   retrieved]` (v1's `list[tuple[dict, float]]` shape). Added a `_paper_dict()`
   helper converting each `ScoredPaper` to the plain dict shape the rest of
   the pipeline (`relevance_check.py`, `generate.py`, `citation_check.py`,
   `query.py`) already expects via `p["pmid"]`/`p["title"]`/etc.
2. **`compress_for_prompt`** (`backend/app/summary/generate.py`) was
   imported from the deleted `llm_client.py` and never reimplemented in v2  - 
   v2 has no prompt-compression step at all (consistent with the measurement
   gate rewrite, which dropped the same v1 compressed-vs-uncompressed
   comparison). Replaced with the uncompressed prompt text directly, with
   `original_tokens`/`compressed_tokens` both set via Card 1's
   `backend.app.llm.tokenizer.estimate_tokens` (identical values - no actual
   compression happens, kept only for the existing field shape).
3. **`CortexLLMClient.chat()`** (`backend/snowflake/llm.py`, Card 1's own
   file) assumed `messages: list[Message]` (the frozen dataclass), but every
   pipeline call site builds messages as plain `{"role", "content"}` dicts  - 
   the wire shape Cortex COMPLETE itself expects. Fixed `chat()` to accept
   either shape via small `_role()`/`_content()` helpers, rather than
   rewriting every prompt-builder across 4 files outside Card 1's ownership.
4. **The model wraps JSON replies in markdown code fences**
   (` ```json\n{...}\n``` `) despite every system prompt saying "Return ONLY
   valid JSON" - confirmed live with `claude-sonnet-4-5`. Every
   `json.loads(result.content)` call site in `relevance_check.py`,
   `generate.py`, `citation_check.py` (5 sites total) was failing silently
   into its degraded-fallback path as a result - this is why the relevance
   check was marking 0/5 papers relevant on the first live `/query` run,
   which looked like a retrieval-quality problem but was actually a parsing
   bug. Swapped all five to Card 1's existing `backend.app.llm.json_repair.try_parse_json`
   (already strips fences; built for `CortexLLMClient`'s own structured-output
   path, just not previously reused by the pipeline's manual `json.loads`
   call sites).

**Verified live**, full `POST /query` via `TestClient` against the real
Snowflake account for the CP1 gold-set-style query: `status=200`,
`degraded=False`, `no_match=False`, real sourced summary text with `[N]`
citations, `case_context` populated (condition = Primary progressive
aphasia semantic variant / retrieved corticobasal syndrome literature),
2 citations, `flagged_claims` showing real per-sentence citation
verification (`supported`/`uncited` statuses), trace showing
`"2/5 passed relevance check -> passed"` on iteration 1.

## [c1] Card 2A needs to wire backend/app/llm/compress.py into pipeline.py

**File:** `backend/app/pipeline.py` (Card 2A ownership)

**Not a bug/import failure like the entry above - a feature hand-off.**
`backend/app/llm/compress.py` (new, Card 1, this pass) exposes
`compress_papers_for_prompt(query, papers, top_n=4)` - extractive
sentence-level compression of retrieved paper abstracts, real measured
34.71% token reduction on the gold set (see
`backend/measurement/results/decision.md` section 4). It is fully
implemented and tested (`backend/tests/snowflake/test_compression.py`) but
**not called from anywhere in the live request path**, because prompt
assembly for the `summary` and `citation_check` call sites lives in
`backend/app/pipeline.py`, which is outside Card 1's ownership bucket.

**Requested change:** wherever `pipeline.py` builds the multi-paper
abstract text that goes into the `summary` and `citation_check` prompts,
call `compress_papers_for_prompt(query, [(p.pmid, p.abstract) for p in
papers], top_n=4)` and use each `PaperCompression.result.text` in place of
the raw abstract, keyed by `pmid` so citation markers stay aligned to the
right paper. `hyde`, `relevance_check`, `refine`, and `memory_distill`
should NOT be touched - they don't build multi-paper abstract prompts.

**Owner:** Card 2A. Not resolved by Card 1 because `backend/app/pipeline.py`
is outside Card 1's ownership bucket (`scripts/ownership.txt`).

**Note added during merge (2026-08-07):** `backend/app/pipeline.py` is
currently a `NotImplementedError` stub, not wired into `/query` at all  - 
the actual live `/query` path (fixed in the "client.chat() call sites"
entry above) calls `run_search_loop`/`generate_sourced_summary`/
`check_citations`/`check_differential` directly from
`backend/api/routes/query.py`, bypassing `Pipeline` entirely. So this
hand-off request should probably target `generate.py`/`citation_check.py`
directly (the real call sites) rather than `pipeline.py` (dead code), once
someone picks it up.

## [c1] .env.example needs two new optional env vars documented (Phase E routing)

**File:** `.env.example` (repo root, not in Card 1's ownership bucket per
`scripts/ownership.txt` -- not listed there at all, so not touched
unilaterally).

**Context:** Phase E (`backend/app/llm/routing.py`, wired into
`CortexLLMClient.chat()` in `backend/snowflake/llm.py`) adds two new
optional env vars: `SNOWFLAKE_CORTEX_MODEL_CHEAP` and
`SNOWFLAKE_CORTEX_MODEL_STRONG`. Both are optional -- if unset, the
existing `SNOWFLAKE_CORTEX_MODEL` is used for every call site exactly as
before (backward compatible, no behavior change for anyone not opting in).

**Requested change:** add, near the existing `SNOWFLAKE_CORTEX_MODEL=
claude-3-5-sonnet` line:

```
SNOWFLAKE_CORTEX_MODEL_CHEAP=
SNOWFLAKE_CORTEX_MODEL_STRONG=
```

with a short comment noting they're optional per-call-site-tier overrides
(see `backend/app/llm/routing.py`'s module docstring for the full
priority order and default model choices).

**Owner:** whoever owns `.env.example` (unclear from `scripts/ownership.txt`
which doesn't mention this file at all -- flagged here rather than edited
unilaterally, per the same out-of-bucket rule as the other entries above).
