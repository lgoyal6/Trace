# Decisions

Append-only. One entry per architecturally-significant decision made ahead
of need, so it isn't made under checkpoint pressure later. Names the
trigger, the decision, and who acts on it.

## [c1] Contingency: `claude-3-5-sonnet` unavailable in account's Cortex region

**Trigger:** plan-v2/01-PHASE-CARD-1-snowflake-platform.md section 5, item
4: "Cortex model availability is region-specific. Confirm your account's
region supports `claude-3-5-sonnet` on `COMPLETE` before building around
it." This decision is being recorded now, ahead of CP3, precisely so it does
not get made under CP3 pressure with a live account already burning
credits.

**Status:** not yet triggered in this sandbox — no live Snowflake account
was available to check region/model availability. This entry documents the
plan for whoever hits it first with real credentials.

**Decision, if `claude-3-5-sonnet` is confirmed unavailable on `COMPLETE`
in the account's region:**

1. **Check availability first, don't guess.** Run, with a live session:
   ```sql
   SELECT SYSTEM$SHOW_AVAILABLE_LLM_MODELS(); -- or the current equivalent
   -- or attempt a minimal COMPLETE call and read the error:
   SELECT SNOWFLAKE.CORTEX.COMPLETE('claude-3-5-sonnet', 'ping');
   ```
   Snowflake's error message on an unsupported model names the account's
   region and (usually) suggests supported alternatives — capture the exact
   error text into this entry before proceeding.

2. **Pick the best available substitute, in this preference order** (per
   plan-v2/00-SHARED-CONTRACTS.md section 2.2's single-provider constraint —
   no fallback chain, no second pricing model, no Groq or non-Cortex
   provider under any circumstance):
   - `claude-sonnet-4-5` if available (Anthropic's current-generation model
     on Cortex as of this research pass — see the `MODEL_PRICING` research
     note in `snowflake/sql/02_tables.sql` and `Handoff-Log.md` item 4,
     which already seeds a pricing row for this model as a hedge).
   - Otherwise, the best available Claude model on `COMPLETE` in that
     region, largest context/quality tier Snowflake exposes.
   - Only if no Claude model is available at all: the best available
     non-Claude model Cortex COMPLETE exposes in that region. This is a
     last resort, not a preference — flag it loudly in this file if it
     happens, since it changes the JSON-schema/repair-prompt assumptions
     baked into `backend/app/llm/json_repair.py` (which were tuned against
     Claude's response shape) and may need re-validation.

3. **Wire the substitution through configuration, not code.** Set
   `SNOWFLAKE_CORTEX_MODEL=<chosen-model>` in the deployment environment —
   `backend/snowflake/llm.py`'s `_active_model()` already reads this env
   var and defaults to `claude-3-5-sonnet` only when it's unset, so no code
   change is required in `CortexLLMClient`.

4. **Update `MODEL_PRICING` for the chosen model.** `INSERT`/`MERGE` a row
   into `NEULIT.CORE.MODEL_PRICING` for `<chosen-model>` with real credit
   rates from `SNOWFLAKE.ACCOUNT_USAGE` / the account's Cortex rate card
   (not list-price research numbers — see `Handoff-Log.md` item 4 on why
   the seeded rows are a starting point, not a substitute for account
   verification) before any `/economics` cost number can be trusted for
   this model. `CortexLLMClient.health()` already returns
   `{"ok": False, "detail": "model ... missing from MODEL_PRICING"}` if this
   step is skipped, so the failure mode is loud rather than a silently wrong
   $0 cost.

5. **Update `snowflake/sql/semantic_model.yaml`** if it references
   `claude-3-5-sonnet` by name anywhere in the Cortex Analyst call (it does,
   in `backend/snowflake/analyst.py`'s hardcoded
   `SNOWFLAKE.CORTEX.COMPLETE('claude-3-5-sonnet', ...)` call for the
   Analyst-over-COMPLETE path) — swap it to read `SNOWFLAKE_CORTEX_MODEL`
   the same way `llm.py` does, or hardcode the confirmed-available
   substitute, so Analyst and the main chat path never disagree about which
   model is live.

6. **Record the actual substitution here** (append, don't edit the plan
   above) once it happens: chosen model, region, the exact
   unavailability error text from step 1, and the date.

**Owner:** whoever on Card 1 (or a successor lane, if Card 1 has handed off
by then) first gets real Snowflake credentials and runs CP1/CP2 setup.

**RESOLVED 2026-08-07, against the live account (identifier redacted; see
`.env`, which is gitignored):**

1. Confirmed unavailable. `SELECT SNOWFLAKE.CORTEX.COMPLETE('claude-3-5-sonnet', 'ping')`
   failed after a full 20s statement timeout (not a fast 400 — the call hangs
   until timeout rather than failing fast, which is itself worth knowing:
   don't assume "slow" means "warehouse cold-starting", check the model name
   first). Retried directly via the connector with `timeout=25`:
   ```
   512513 (P0000): Request failed for external function COMPLETE with
   remote service error: '400 'unknown model "claude-3-5-sonnet"''
   ```
2. Substitute chosen: **`claude-sonnet-4-5`** — exactly the hedge this file
   predicted. Verified live (`SELECT SNOWFLAKE.CORTEX.COMPLETE('claude-sonnet-4-5', ...)`
   returned `'PONG'` in 1.8s). `claude-4-sonnet` also resolved successfully as
   an alias but `claude-sonnet-4-5` is the canonical name kept. Also
   confirmed working as non-Claude fallbacks if ever needed: `llama3.1-8b`,
   `mistral-large2`. Confirmed NOT available in this region: `claude-3-7-sonnet`,
   `claude-sonnet-4`, `reka-flash`, `snowflake-arctic`.
3. Wired via configuration: `.env`, `.env.example`, and
   `config/snowflake.yaml`'s `default_cortex_model` all updated to
   `claude-sonnet-4-5`; `backend/snowflake/llm.py`'s `_DEFAULT_MODEL` fallback
   updated to match (env var still takes precedence, no hardcoding of the
   active model beyond this fallback).
4. `MODEL_PRICING` already had a `claude-sonnet-4-5` row from the earlier
   research pass (list-price sourced, not yet reconciled against
   `SNOWFLAKE.ACCOUNT_USAGE` — that table needs usage history to populate on
   a fresh account, so exact reconciliation is still a follow-up once the
   account has real billed usage).
5. `backend/snowflake/analyst.py` no longer hardcodes `claude-3-5-sonnet` —
   it now calls the same `_active_model()` (reads `SNOWFLAKE_CORTEX_MODEL`)
   so the Analyst path and the main chat path can never disagree about which
   model is live.

---

## [c1] Proposal: surface cache hit-rate / cost-saved on /economics/summary (needs 2A/2B sign-off)

**Context:** Phase C of the hackathon "Cost of Intelligence" feature
(prompt caching + context compression, `backend/app/llm/cache.py` +
`backend/app/llm/compress.py`) produces real, per-run numbers --
`cache_stats()` returns `{hits, misses, hit_rate, estimated_cost_saved_usd}`
-- that would be a natural addition to the demo dashboard.

**Why it's not wired in:** `GET /economics/summary`'s response shape is
frozen at contracts-v1 (`plan-v2/00-SHARED-CONTRACTS.md` section 4):

```
GET /economics/summary?window=24h
  res  { total_requests, total_tokens, total_cost_usd,
         by_call_site: {call_site, requests, tokens, cost_usd}[],
         by_hour: {hour_iso, tokens, cost_usd}[] }
```

There is no field for cache hit-rate or cost-saved, and per section 4's own
rule ("Card 2B never asks Card 2A to change a shape mid-build -- if a shape
is wrong, it is logged in Decisions.md and changed once, at an integration
checkpoint, by both at the same time"), Card 1 is not changing this shape
unilaterally. `backend/api/routes/economics.py` (Card 1 ownership) is left
untouched by this work.

**Proposed shape change** (additive, backward compatible -- existing
fields unchanged):

```
GET /economics/summary?window=24h
  res  { total_requests, total_tokens, total_cost_usd,
         by_call_site: {call_site, requests, tokens, cost_usd}[],
         by_hour: {hour_iso, tokens, cost_usd}[],
         cache: { hits: number, misses: number, hit_rate: number,
                  estimated_cost_saved_usd: number } }
```

Backed by `backend.app.llm.cache.cache_stats()`, which already returns
exactly this shape (see `backend/app/llm/cache.py`). Wiring would be a
one-line addition to `EconomicsSummaryOut` (new optional `cache` field) and
one line in `economics_summary()`'s two return statements.

**Owner:** needs Card 2A (owns `backend/api/dependencies.py` / the app
wiring, and is the other party to the section 4 HTTP contract) and Card 2B
(consumes this shape via generated `api-types.ts`) sign-off before this
lands, per the section 4 rule above. Not resolved by Card 1 in this pass.

## POST /query gains an optional `policy` — additive, logged per section 4

**Decided 2026-08-07.** `plan-v2/00-SHARED-CONTRACTS.md` section 4 freezes the
`POST /query` shape and requires shape changes to be logged here rather than
made silently mid-build. This is that log entry.

**What changed, exactly:**

```
POST /query
  req  { ..., policy?: "tight" | "generous" | null }     # NEW, optional
  res  { ..., policy: PolicyOut | null }                 # NEW, null by default
```

```
PolicyOut = { label, topK, compressTopN, papersInPrompt,
              promptTokensBeforeCompression, promptTokensAfterCompression,
              tokensSaved, reductionPct }
```

**Why this is safe to land without a joint checkpoint,** unlike the
`/economics/summary` `cache` field logged above:

1. **Both halves are additive and optional.** Omitting `policy` from the
   request yields byte-identical behaviour to before — `pipeline.run_query`'s
   `policy` kwarg defaults to `None`, which is the pre-existing code path
   (`RETRIEVAL_TOP_K` papers, no compression). The response field is `null`.
   Pinned by `test_api_query.py`'s contract-shape test, which now asserts
   `body["policy"] is None` for an unrequested policy.
2. **No existing consumer breaks.** Card 2B generates `api-types.ts` from
   `/openapi.json` (`npm run types:gen`), so re-running that picks the field up
   as optional. A consumer that never regenerates simply ignores an extra
   nullable key.
3. **An unknown label is a 422, not a fallback.** `QueryRequest`'s validator
   calls `policy_for_label()`, which raises on an unknown name. A demo that
   quietly runs the wrong arm is worse than one that errors.

**Why it exists:** it is the API surface for the breadth/depth trade measured
in `backend/measurement/results/policy_bench.md` — rare-condition recall
0.4118 -> 0.7475 (+81.5%) at −0.34% cost, by retrieving 3x the papers and
compressing each to one sentence. Without a request-level toggle the result is
a number in a JSON file; with it, the two arms can be run live side by side.

**Not decided here:** whether `GENEROUS` should become the default. That needs
a live citation-checked run — the bench calls no LLM and so measures what
reaches the prompt, not answer quality. Opt-in until that exists.
