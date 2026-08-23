# PHASE CARD 1 - SNOWFLAKE PLATFORM LAYER

| | |
|---|---|
| **Weight** | **50% of total project work** |
| **Branch** | `branch-1` (cut from tag `contracts-v1`) |
| **Operator** | Teammate, working **solo and independently** |
| **Languages** | Python, SQL, YAML. **Zero** TypeScript. **Zero** files under `frontend/` or `docs/`. |
| **Commit prefix** | `[c1]` |
| **Shell var** | `export NEULIT_LANE=c1` |
| **Depends on** | `plan-v2/00-SHARED-CONTRACTS.md` freeze commit being on `main` and tagged |
| **Blocked by** | nothing else. You never wait on Card 2A or 2B. |

---

## 0. Preamble to paste at the top of every work session

> You are operating as **Card 1 (Snowflake platform, Python + SQL only)** on branch `branch-1`. You may edit only the paths listed under "Card 1" in `plan-v2/00-SHARED-CONTRACTS.md`. You may not edit any file under `frontend/`, `docs/`, `backend/memory/`, `backend/app/loop/`, `backend/app/summary/`, or `backend/app/verify/`, and you may not edit any FROZEN file. If your task appears to require editing a file you do not own, stop and append an entry to `Blockers.md` in the Obsidian vault instead. Before you begin, read `Handoff-Log.md`.

---

## 1. What you own, in one sentence

**Everything that talks to Snowflake, plus everything that talks to a model.** You are building the platform the other two cards consume through `RetrievalPort`, `LLMPort`, and `LedgerPort`. You never see their code and they never see yours.

You do **not** own: the search loop, the summary generator, the citation checker, the memory layer, any route except `/economics` and `/conditions`, or any UI.

---

## 2. Ownership boundary - the exact list

You may create, edit, and delete only these:

```
backend/snowflake/**
backend/app/llm/**                    (new package)
backend/app/retrieval/**
backend/app/corpus/**
backend/api/routes/economics.py
backend/api/routes/conditions.py
backend/requirements-snowflake.txt
config/snowflake.yaml
snowflake/sql/**                      (new top-level dir)
backend/tests/snowflake/**
backend/tests/test_hybrid_retrieval.py
backend/tests/test_retrieval_gold_set.py
backend/tests/test_corpus_coverage.py
backend/tests/test_build_corpus.py
backend/tests/test_fetch_pubmed.py
backend/tests/test_llm_client.py
backend/tests/test_api_conditions.py
backend/measurement/**
```

**Deleting `backend/app/llm_client.py`** is your job and is expected - replace it with the `backend/app/llm/` package. Card 2A has been told to import `LLMPort` from `backend.contracts`, never `backend.app.llm_client`, so this deletion cannot break them.

---

## 3. Work breakdown

### 3.1 Snowflake account setup and DDL - `snowflake/sql/`

Create these files. They are run in order, by hand, once, against the hackathon account.

**`snowflake/sql/01_setup.sql`**

```sql
CREATE WAREHOUSE IF NOT EXISTS NEULIT_WH
  WAREHOUSE_SIZE = XSMALL
  AUTO_SUSPEND = 60
  AUTO_RESUME = TRUE
  INITIALLY_SUSPENDED = TRUE;

CREATE DATABASE IF NOT EXISTS NEULIT;
CREATE SCHEMA   IF NOT EXISTS NEULIT.CORE;
CREATE ROLE     IF NOT EXISTS NEULIT_APP;

GRANT USAGE ON WAREHOUSE NEULIT_WH TO ROLE NEULIT_APP;
GRANT USAGE ON DATABASE NEULIT     TO ROLE NEULIT_APP;
GRANT USAGE ON SCHEMA NEULIT.CORE  TO ROLE NEULIT_APP;
GRANT SELECT ON FUTURE TABLES IN SCHEMA NEULIT.CORE TO ROLE NEULIT_APP;
GRANT INSERT ON FUTURE TABLES IN SCHEMA NEULIT.CORE TO ROLE NEULIT_APP;
-- Cortex functions live on the SNOWFLAKE database
GRANT DATABASE ROLE SNOWFLAKE.CORTEX_USER TO ROLE NEULIT_APP;
```

`AUTO_SUSPEND = 60` matters. Hackathon credits are finite and an idle XSMALL warehouse burning for eight hours is a real way to lose the demo.

**`snowflake/sql/02_tables.sql`**

```sql
CREATE OR REPLACE TABLE NEULIT.CORE.PAPERS (
  PMID        STRING       NOT NULL PRIMARY KEY,
  TITLE       STRING       NOT NULL,
  ABSTRACT    STRING       NOT NULL,
  JOURNAL     STRING,
  PUB_YEAR    NUMBER(4,0),
  CONDITION   STRING       NOT NULL,
  IS_RARE     BOOLEAN      NOT NULL,
  URL         STRING,
  SEARCH_BLOB STRING       NOT NULL,   -- TITLE || ' ' || ABSTRACT, what Cortex Search indexes
  LOADED_AT   TIMESTAMP_NTZ DEFAULT CURRENT_TIMESTAMP()
);

CREATE OR REPLACE TABLE NEULIT.CORE.CONDITIONS (
  CONDITION     STRING NOT NULL PRIMARY KEY,
  IS_RARE       BOOLEAN NOT NULL,
  PAPER_COUNT   NUMBER,
  DESCRIPTION   STRING,
  BRAIN_REGIONS ARRAY,
  CONDITION_VEC VECTOR(FLOAT, 768)     -- Cortex EMBED_TEXT_768 over DESCRIPTION
);

CREATE OR REPLACE TABLE NEULIT.CORE.TOKEN_LEDGER (
  EVENT_ID          STRING DEFAULT UUID_STRING(),
  REQUEST_ID        STRING NOT NULL,
  SESSION_ID        STRING,
  USER_ID           STRING,
  CALL_SITE         STRING NOT NULL,   -- hyde|relevance_check|refine|summary|citation_check|memory_distill
  MODEL             STRING NOT NULL,
  PROMPT_TOKENS     NUMBER NOT NULL,
  COMPLETION_TOKENS NUMBER NOT NULL,
  TOTAL_TOKENS      NUMBER NOT NULL,
  COST_USD          NUMBER(18,8) NOT NULL,
  LATENCY_MS        NUMBER,
  DEGRADED          BOOLEAN DEFAULT FALSE,
  OCCURRED_AT       TIMESTAMP_NTZ NOT NULL
);

CREATE OR REPLACE TABLE NEULIT.CORE.MODEL_PRICING (
  MODEL              STRING NOT NULL PRIMARY KEY,
  CREDITS_PER_MTOK_IN  NUMBER(18,8) NOT NULL,
  CREDITS_PER_MTOK_OUT NUMBER(18,8) NOT NULL,
  USD_PER_CREDIT       NUMBER(18,8) NOT NULL,
  EFFECTIVE_FROM     TIMESTAMP_NTZ NOT NULL
);
```

`MODEL_PRICING` as a table, not a Python constant, is deliberate: it means the cost model is data that Cortex Analyst can reason about and that you can correct mid-hackathon without a redeploy. Seed it from the current Snowflake Cortex credit-consumption table for whichever model you select, and record the credit-to-USD rate you used.

**`snowflake/sql/03_search_service.sql`**

```sql
CREATE OR REPLACE CORTEX SEARCH SERVICE NEULIT.CORE.PAPERS_SEARCH
  ON SEARCH_BLOB
  ATTRIBUTES PMID, TITLE, JOURNAL, PUB_YEAR, CONDITION, IS_RARE, URL
  WAREHOUSE = NEULIT_WH
  TARGET_LAG = '1 hour'
AS (
  SELECT SEARCH_BLOB, PMID, TITLE, ABSTRACT, JOURNAL,
         PUB_YEAR, CONDITION, IS_RARE, URL
  FROM NEULIT.CORE.PAPERS
);
```

Cortex Search does hybrid lexical + vector natively. **This is why `bm25_index.py` and `vector_index.py` are deleted at freeze** - you are not reimplementing fusion, you are consuming a service that already does it, then applying the rarity re-rank on top.

**`snowflake/sql/04_views.sql`** - the views `/economics` reads and Cortex Analyst is pointed at:

```sql
CREATE OR REPLACE VIEW NEULIT.CORE.V_COST_BY_CALL_SITE AS
SELECT CALL_SITE, MODEL,
       COUNT(*) AS CALLS,
       SUM(PROMPT_TOKENS)     AS PROMPT_TOKENS,
       SUM(COMPLETION_TOKENS) AS COMPLETION_TOKENS,
       SUM(TOTAL_TOKENS)      AS TOTAL_TOKENS,
       SUM(COST_USD)          AS COST_USD,
       AVG(LATENCY_MS)        AS AVG_LATENCY_MS,
       SUM(IFF(DEGRADED,1,0)) AS DEGRADED_CALLS
FROM NEULIT.CORE.TOKEN_LEDGER
GROUP BY 1,2;

CREATE OR REPLACE VIEW NEULIT.CORE.V_COST_PER_REQUEST AS
SELECT REQUEST_ID, ANY_VALUE(SESSION_ID) AS SESSION_ID,
       ANY_VALUE(USER_ID) AS USER_ID,
       COUNT(*) AS LLM_CALLS,
       SUM(TOTAL_TOKENS) AS TOTAL_TOKENS,
       SUM(COST_USD)     AS COST_USD,
       MIN(OCCURRED_AT)  AS STARTED_AT
FROM NEULIT.CORE.TOKEN_LEDGER
GROUP BY REQUEST_ID;

CREATE OR REPLACE VIEW NEULIT.CORE.V_COST_BY_HOUR AS
SELECT DATE_TRUNC('hour', OCCURRED_AT) AS HOUR,
       SUM(TOTAL_TOKENS) AS TOTAL_TOKENS,
       SUM(COST_USD)     AS COST_USD,
       COUNT(DISTINCT REQUEST_ID) AS REQUESTS
FROM NEULIT.CORE.TOKEN_LEDGER
GROUP BY 1;
```

**Deliverable:** all four SQL files committed, plus a `snowflake/sql/README.md` giving the exact run order and the `snowsql` command line. Paste the `SHOW CORTEX SEARCH SERVICES` output into `Handoff-Log.md` when the service reports `ACTIVE`.

---

### 3.2 Connection layer - `backend/snowflake/session.py`

A single lazily-initialized, thread-safe Snowpark `Session` factory reading the `SNOWFLAKE_*` env vars.

Requirements:

- One session per process, guarded by a lock. Never one per request.
- `get_session()` returns `None` (does not raise) if credentials are absent, so the `fake` profile and CI keep working with no Snowflake in the environment.
- A `snowflake_available() -> bool` helper every other module in `backend/snowflake/` calls before doing work.
- Query timeout set explicitly (`STATEMENT_TIMEOUT_IN_SECONDS = 30`). An interactive query must never hang on a suspended warehouse resume.
- Structured logging on connect: account, warehouse, role, database, schema. **Never log the password.**

---

### 3.3 Corpus migration - `backend/app/corpus/`

Rewrite `build_corpus.py` and `conditions.py` so the target is Snowflake, not JSON.

1. `python -m backend.app.corpus.build_corpus --to-snowflake` reads `backend/data/corpus.json` (329 papers, 14 conditions) and writes `PAPERS` and `CONDITIONS`.
2. `SEARCH_BLOB` is computed on write as `TITLE || ' ' || ABSTRACT`.
3. `CONDITIONS.CONDITION_VEC` is filled with `SNOWFLAKE.CORTEX.EMBED_TEXT_768('snowflake-arctic-embed-m', DESCRIPTION)`, run as a single `UPDATE`, not row by row.
4. `PAPER_COUNT` is derived, never hand-entered.
5. The load is **idempotent** - re-running truncates and reloads. You will run this more than once and a half-loaded table is a bad hour.
6. `fetch_pubmed.py` stays as-is functionally; only change its output sink.

**Verification gate before you move on:** `SELECT COUNT(*) FROM PAPERS` returns 329 and `SELECT COUNT(*) FROM CONDITIONS` returns 14, with exactly 10 rows where `IS_RARE = TRUE`. Paste both counts into `Handoff-Log.md`.

---

### 3.4 Retrieval - `backend/snowflake/retrieval.py` + `backend/app/retrieval/`

`CortexSearchRetriever` implements `RetrievalPort` exactly as specified in the contracts document.

**`search()` algorithm, step by step:**

1. Call the Cortex Search Service on `query`, requesting `limit = max(top_k * 4, 40)`. Over-fetch is required because rarity re-rank and `exclude_pmids` both remove candidates after the fact.
2. If `secondary_query` is present, run a second Cortex Search call and merge by PMID. Fuse with the same 60/40 primary/secondary weighting v1 used, so retrieval quality is comparable to the old build.
3. Normalize Cortex Search relevance scores to `[0, 1]` across the returned set, same min-max normalization as v1's `_normalize`.
4. Apply `rarity_boost(paper)` from `backend/app/retrieval/rarity.py`. **Keep this file's formula byte-identical to v1.** It is the project's differentiating claim and it must not silently change while the backend changes underneath it. Record the multiplier in `ScoredPaper.rarity_multiplier`.
5. Drop any PMID in `exclude_pmids`. **This happens after scoring and before truncation.** Card 2A's seen-paper dedup is entirely dependent on this ordering; getting it wrong produces short result lists that look like a memory bug in someone else's code.
6. Sort by final score, truncate to `top_k`, return `list[ScoredPaper]`.
7. Set `lexical_score` and `semantic_score` to the Cortex Search sub-scores if the response exposes them; `0.0` otherwise. Do not fabricate values.

**`closest_conditions()`:** embed the query with `EMBED_TEXT_768`, then `VECTOR_COSINE_SIMILARITY` against `CONDITIONS.CONDITION_VEC`, ordered descending, limit `top_n`. Return `ConditionMatch` including `is_rare`.

**`get_by_pmids()`:** plain `SELECT ... WHERE PMID IN (...)`, order preserved to match the input sequence.

**`health()`:** confirm the search service exists and is `ACTIVE`, and that `PAPERS` is non-empty. Return `{"ok": bool, "detail": str}`.

**Degradation:** every method returns an empty result and logs a warning if `snowflake_available()` is `False`. It never raises.

**Files to remove from `backend/app/retrieval/`:** `bm25_index.py`, `vector_index.py`, `hybrid.py`. **Files to keep:** `rarity.py`, `condition_match.py` (reduced to pure scoring helpers with no embedding model), `query_specificity.py`, `demo_fixture.py`.

---

### 3.5 Inference - `backend/app/llm/` + `backend/snowflake/llm.py`

Delete `backend/app/llm_client.py`. Delete the Groq client, the Gemini client, and every reference to `OPENAI_BASE_URL`.

`CortexLLMClient` implements `LLMPort`:

- Calls `SNOWFLAKE.CORTEX.COMPLETE(model, prompt_or_messages, options)` via Snowpark.
- Model name comes from `SNOWFLAKE_CORTEX_MODEL`, defaulting to `claude-3-5-sonnet`. Never hardcode.
- **Structured output.** Four of six call sites need parseable JSON. Cortex `COMPLETE` supports a `response_format` option in `options`; use it and pass the caller's `json_schema` through. If the model returns something unparseable anyway, retry once with a repair instruction, then return a degraded `ChatResult` with the raw content preserved in `.content` so Card 2A's caller can decide.
- **Retry:** up to 3 attempts, exponential backoff `2**attempt` seconds, on transient Snowflake errors only. Never retry a schema-validation failure more than the one repair attempt.
- **Timeout:** 20s per attempt. Hard-capped. The old build's 180s Paritok cold-start tolerance is gone and should not be recreated.
- **No fallback provider.** If Cortex fails after retries, return `ChatResult(degraded=True)`. This is a decision, not an oversight - one provider is what makes the ledger's cost numbers trustworthy.
- **Usage extraction.** Cortex `COMPLETE` returns token counts in its response metadata. Read them. If a given call shape does not return them, count with a local tokenizer and set a `estimated=True` marker in the log line - but never write a fabricated number to the ledger without flagging it.
- **Ledger write is mandatory and happens in `chat()`, in a `finally` block.** Every path, including degraded and exception paths, writes exactly one `LedgerEvent`. Card 2A is explicitly forbidden from writing to the ledger, so if you miss a path the cost data is silently wrong and nobody else can fix it.

**Cost calculation.** Look up `MODEL_PRICING` for the active model (cache for the process lifetime), compute:

```
cost_usd = (prompt_tokens / 1e6) * credits_per_mtok_in  * usd_per_credit
         + (completion_tokens / 1e6) * credits_per_mtok_out * usd_per_credit
```

If the model is missing from `MODEL_PRICING`, write `cost_usd = 0` and log an error. Do not guess a price.

---

### 3.6 Ledger - `backend/snowflake/ledger.py`

`SnowflakeLedger` implements `LedgerPort`.

- `record()` must be **non-blocking from the caller's perspective.** Buffer events in a bounded in-memory queue (`maxsize=1000`) and flush from a single background thread every 2 seconds or every 25 events, whichever comes first. A slow warehouse must never add latency to a user's query.
- On queue-full, drop the oldest and increment a dropped counter exposed in `health()`. Losing a ledger row is acceptable; blocking a query is not.
- Flush with a multi-row `INSERT`, not one statement per event.
- Register an `atexit` flush so a demo run's last request still shows up in the dashboard.
- `health()` reports `{"ok", "detail", "queued", "dropped", "last_flush_iso"}`.

---

### 3.7 Cortex Analyst - `backend/snowflake/analyst.py` + semantic model

**`snowflake/sql/semantic_model.yaml`** - the Cortex Analyst semantic model over `V_COST_BY_CALL_SITE`, `V_COST_PER_REQUEST`, `V_COST_BY_HOUR`, and `MODEL_PRICING`. Define:

- Logical table names a person would say out loud: "token spend", "requests", "models".
- Measures: `total_tokens`, `cost_usd`, `calls`, `avg_latency_ms`, `degraded_calls`.
- Dimensions: `call_site`, `model`, `hour`, `user_id`, `session_id`.
- Synonyms that make the demo work on the first try: `call_site` → "step", "stage", "part of the pipeline"; `cost_usd` → "spend", "how much", "dollars"; `summary` → "the summary step", "answer generation".
- At least 5 verified queries, including: *"what did the last 10 queries cost"*, *"which pipeline step is most expensive"*, *"what is the average cost per request today"*, *"how many calls degraded"*, *"cost per user"*.

`analyst.py` wraps the Cortex Analyst REST endpoint: takes a natural-language question, returns `{answer, sql, rows}`. On failure, return a clear `{answer: "...unavailable...", sql: "", rows: []}` rather than raising.

**Be honest about the risk here.** Cortex Analyst needs a well-formed semantic model and is the most likely thing on this card to eat two hours. It is scheduled last for that reason. If it is not working by CP3, ship `/economics/ask` returning a canned SQL-backed answer path and note it as a limitation. Do not let it block the ledger or the dashboard.

---

### 3.8 Routes - `backend/api/routes/economics.py`, `conditions.py`

Implement the four `/economics` endpoints and keep `/conditions` working against the Snowflake `CONDITIONS` table. Shapes are frozen in §4 of the contracts document. **You may not change a response shape** - Card 2B has already generated TypeScript types from it. If a shape is genuinely wrong, file it in `Decisions.md` and change it at an integration checkpoint, with 2B present.

Rate limits via the existing `slowapi` limiter: 20/min on `/economics/summary`, 6/min on `/economics/ask` (Analyst calls are not cheap).

---

### 3.9 Measurement - `backend/measurement/`

The v1 measurement gate compared compressed vs. uncompressed prompts. That comparison no longer exists. Repurpose the directory to answer the question the new stack actually raises:

**`backend/measurement/run_gate.py`** now runs the existing gold-set queries and reports:

1. **Retrieval parity.** Cortex Search + rarity re-rank vs. the v1 BM25 + MiniLM baseline (recomputable from `corpus.json` via `FakeRetrieval`) on the same gold set. Metric: recall@10 and rare-condition recall@10. **The claim you need to be able to defend is that moving to Cortex Search did not cost retrieval quality.** If it did, report the number honestly rather than hiding it.
2. **Cost per query.** Median and p95 `cost_usd` per `/query`, broken down by call site, straight out of `V_COST_PER_REQUEST`.
3. **Latency per call site**, median and p95.

Output to `backend/measurement/results/decision.md`. This file is the source of every number Card 2B will put in the docs, so write it in a form that is quotable: one table, explicit units, explicit sample size.

---

### 3.10 Tests - `backend/tests/snowflake/` and the reassigned files

Two tiers, and the split matters:

**Tier 1 - credential-free, must run in CI.** `NEULIT_PROFILE=fake`, no `SNOWFLAKE_*` env vars set.

- `test_retrieval_contract.py` - `CortexSearchRetriever` with a mocked Snowpark session: verifies `exclude_pmids` is applied before truncation, that `top_k` is respected, that rarity multipliers land in the returned dataclass, and that an unavailable session returns `[]` rather than raising.
- `test_llm_contract.py` - mocked `COMPLETE`: verifies exactly one ledger event per `chat()` call across success, retry-then-success, and total-failure paths; verifies `json_schema` is passed through; verifies the 20s timeout is set.
- `test_cost_math.py` - pricing table math against hand-computed values, including the missing-model case yielding `0` plus an error log.
- `test_ledger_buffer.py` - queue flush on count threshold, flush on time threshold, drop-oldest on overflow with the counter incrementing, `atexit` flush.
- `test_health.py` - all four `health()` shapes.

**Tier 2 - live, run by hand, marked `@pytest.mark.live`.** Real credentials. Round-trips the search service, one real `COMPLETE`, one real ledger insert and read-back, one real Analyst question.

Reassigned v1 test files you now own: rewrite `test_hybrid_retrieval.py`, `test_retrieval_gold_set.py`, `test_corpus_coverage.py`, `test_build_corpus.py`, `test_fetch_pubmed.py`, `test_llm_client.py`, `test_api_conditions.py` against the new implementations. **Keep the filenames.** Renaming them creates a delete+add pair that shows up as a conflict for anyone else who touches the tests directory.

---

## 4. Sequence and checkpoints

| Order | Work | Done by |
|---|---|---|
| 1 | §3.1 DDL + §3.2 session | before CP1 |
| 2 | §3.3 corpus migration | before CP1 |
| 3 | §3.1 search service live, §3.4 retriever | **CP1 gate** |
| 4 | §3.5 Cortex LLM client | before CP2 |
| 5 | §3.6 ledger | **CP2 gate** |
| 6 | §3.8 `/economics/summary` + `/request/{id}` | before CP3 |
| 7 | §3.7 Cortex Analyst + `/economics/ask` | **CP3 gate** |
| 8 | §3.9 measurement, §3.10 Tier 2 tests | after CP3 |

**CP1 hand-off note you must post to `Handoff-Log.md`:** search service status, row counts, and a sample `search()` result for the gold-set query `"asymmetric parietal hypometabolism on FDG-PET with progressive apraxia"`.

**CP2 hand-off note:** confirmation that `NEULIT_PROFILE=live_no_memory` runs `/query` end to end, plus one `SELECT * FROM V_COST_PER_REQUEST LIMIT 5` output. Card 2B needs those rows to know their dashboard renders real shapes.

---

## 5. Things that will bite you, listed so they don't

1. **Cortex Search Service indexing is not instant.** After `CREATE`, the service needs to build. Poll `SHOW CORTEX SEARCH SERVICES` until it is serving before you assume retrieval is broken.
2. **`TARGET_LAG = '1 hour'` means reloading `PAPERS` does not immediately update the index.** During the corpus-tuning phase, either drop and recreate the service or set a shorter lag temporarily. Then set it back - a short lag burns credits continuously.
3. **`AUTO_SUSPEND` resume adds seconds to the first query after idle.** This is the new cold start, and it is much shorter than Paritok's was. Do not build a 180-second tolerance for it. Card 2B still has a staged loader; a 3-8 second resume is what it now covers.
4. **Cortex model availability is region-specific.** Confirm your account's region supports `claude-3-5-sonnet` on `COMPLETE` before building around it. If not, pick the best available and record the choice in `Decisions.md` - the model name flows into `MODEL_PRICING` and the ledger.
5. **Credits.** Check consumption at CP1 and CP2. If the burn rate projects past the allocation, drop the warehouse to XSMALL if it is not already, raise `AUTO_SUSPEND` aggressiveness, and cut the Analyst rate limit.
6. **Do not add a Groq fallback back in "just in case."** It reintroduces a second pricing model, breaks the ledger's single-source claim, and lives in a file Card 2A is not allowed to see.

---

## 6. Definition of done for Card 1

- [ ] All four SQL files run clean on a fresh account; `snowflake/sql/README.md` documents the order.
- [ ] `PAPERS` = 329 rows, `CONDITIONS` = 14 rows (10 rare), verified counts posted to `Handoff-Log.md`.
- [ ] Cortex Search Service `ACTIVE` and returning results for the gold-set query.
- [ ] `CortexSearchRetriever`, `CortexLLMClient`, `SnowflakeLedger` all implement their ports with **no signature deviation** from `backend/contracts/ports.py`.
- [ ] `exclude_pmids` verified applied before `top_k` truncation, by test.
- [ ] Exactly one ledger event per `chat()` call, verified across success / retry / failure paths, by test.
- [ ] `NEULIT_PROFILE=live_no_memory` runs a full `/query` end to end.
- [ ] All four `/economics` endpoints return the frozen shapes.
- [ ] Cortex Analyst answers all 5 verified queries, or the limitation is written up in `decision.md`.
- [ ] Tier 1 tests green with **zero** Snowflake credentials in the environment.
- [ ] `git diff --name-only contracts-v1...branch-1` contains no path outside §2 and no FROZEN file.
- [ ] `backend/measurement/results/decision.md` contains the retrieval-parity table and the cost-per-query table.
