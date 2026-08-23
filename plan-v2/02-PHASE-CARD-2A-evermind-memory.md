# PHASE CARD 2A - EVERMIND MEMORY & PIPELINE ORCHESTRATION

| | |
|---|---|
| **Weight** | **25% of total project work** |
| **Branch** | `branch-2` (shared with Card 2B) |
| **Operator** | Bryan's **Claude Code** |
| **Languages** | **Python only.** Zero `.ts` / `.tsx` / `.css`. Zero files under `frontend/` or `docs/`. Zero `.sql`. |
| **Commit prefix** | `[c2a]` |
| **Shell var** | `export NEULIT_LANE=c2a` |
| **Partner card** | Card 2B (Codex) - same branch, disjoint files, communicates only via `Handoff-Log.md` and the frozen HTTP contract |
| **Blocked by** | nothing. You develop entirely on `NEULIT_PROFILE=fake` until CP2. |

---

## 0. Preamble to paste at the top of every Claude Code session - verbatim, do not paraphrase

> You are operating as **Card 2A (Claude Code, Python only)** on branch `branch-2`. You may edit only the paths listed under "Card 2A" in `plan-v2/00-SHARED-CONTRACTS.md`. You may not edit any `.ts`, `.tsx`, `.css`, `.sql` file, any file under `frontend/`, `docs/`, `backend/snowflake/`, or `backend/app/retrieval/`, and you may not edit any FROZEN file. If your task appears to require editing a file you do not own, stop and append an entry to `Blockers.md` in the Obsidian vault instead. Before you begin, read `Handoff-Log.md`. Before you finish, append to `Handoff-Log.md`. Run `git pull --ff-only origin branch-2` before your first commit and before your last.

**Why the wording is fixed.** You and Codex are on the same branch. The only thing preventing you from stepping on each other is that each of you knows precisely which files are yours, stated in the same words every time. A paraphrased preamble is how an agent talks itself into "just a small edit" in the other lane, and that edit is the merge conflict.

---

## 1. What you own, in one sentence

**The memory layer and the orchestration that consumes every port.** You own EverOS integration, the researcher profile, the session thread, the seen-paper dedup, the memory-conditioned re-rank, and the rewiring of the search loop / summary / citation checker onto `backend.contracts`.

You do **not** own: anything that talks to Snowflake, any retrieval scoring, the LLM client itself, any UI, or any documentation.

---

## 2. Ownership boundary - the exact list

```
backend/memory/**
backend/app/pipeline.py
backend/app/loop/**
backend/app/summary/**
backend/app/verify/**
backend/api/routes/memory.py
backend/api/routes/query.py
backend/api/routes/demo.py
backend/api/schemas.py
backend/api/dependencies.py
backend/requirements-memory.txt
config/evermind.yaml
backend/tests/memory/**
backend/tests/test_search_loop.py
backend/tests/test_loop_prompts.py
backend/tests/test_summary_generate.py
backend/tests/test_citation_check.py
backend/tests/test_pipeline_integration.py
backend/tests/test_api_query.py
backend/tests/test_api_demo.py
backend/tests/test_query_stream.py
backend/tests/test_multiturn_session.py
backend/seed.py
```

### Your hard prohibitions

- You never `import` from `backend.snowflake.*`. You get Snowflake behaviour through `get_services()` and the ports. If you find yourself needing a Snowflake detail, you need a contract change, and that is a `Decisions.md` entry.
- You never write to `TOKEN_LEDGER` or call `LedgerPort.record()`. Card 1's `LLMPort.chat()` does that on every call, including failures. Writing your own ledger events would double-count every request and corrupt the demo's headline number.
- You never edit `backend/app/retrieval/rarity.py`. If a memory boost needs to interact with the rarity boost, it does so **after** retrieval, inside your re-ranker, using `ScoredPaper.rarity_multiplier` as read-only input.
- You never run `npm`, `next`, or `playwright`.

---

## 3. What EverOS does in this product

Decided, and not up for reinterpretation mid-build:

**Researcher profile + session thread.** EverOS remembers who is asking - their specialty, the conditions they have explored, the papers they have already been shown - and later queries are re-ranked and later summaries are written against what that person already knows. That is the "gets better the more it's used" claim, and every feature below serves it.

Explicitly **not** in scope: query-rewrite learning, cross-user memory, memory-driven corpus expansion. If those look tempting, they are the reason this card ships at 60%.

---

## 4. Work breakdown

### 4.1 `backend/memory/evermind.py` - `EverOSMemory implements MemoryPort`

The whole EverOS surface, wrapped so the rest of the codebase never sees the SDK.

**Namespacing.** Every write is scoped `{EVEROS_NAMESPACE}:{user_id}`. Session-scoped writes add `:{session_id}`. Getting namespacing wrong is how one demo user's profile leaks into another's, which is the single most embarrassing possible failure for a memory-layer project.

**Methods and their storage shape:**

| Method | What it stores / reads |
|---|---|
| `get_profile(user_id)` | Reads the profile record; returns an empty `ResearcherProfile` if absent. Never raises on miss. |
| `set_specialty(user_id, specialty)` | One free-text field, e.g. `"neuroradiology resident"`. Capped at 120 chars. |
| `record_query(user_id, session_id, query, matched_conditions)` | Appends to the session thread, increments `query_count`, unions `matched_conditions` into `conditions_explored`. |
| `record_papers_shown(user_id, session_id, pmids)` | Appends to both the session thread and the durable per-user seen set. |
| `seen_pmids(user_id)` | Returns the durable set. **Must be fast** - it is on the hot path of every query. Cache in-process with a 60s TTL. |
| `get_thread(user_id, session_id)` | The current session's queries and PMIDs shown. |
| `forget(user_id)` | Deletes everything under the user's namespace. Required for the demo - you must be able to show a cold user and a warm user back to back. |
| `health()` | `{"ok", "detail"}`; a reachability probe, not a write. |

**Degradation is mandatory.** With `EVEROS_API_KEY` unset or EverOS unreachable, every read returns an empty default and every write is a logged no-op. `/query` continues to work and simply reports `memory.applied = false`. **A memory outage must never fail a search.** Enforce this with a test that patches the client to raise on every call and asserts `/query` still returns 200.

**Latency budget.** Memory reads on the query path get a hard **300 ms** combined budget. Exceed it and you return defaults and set `memory.applied = false`. Personalization is worth having; it is not worth a visibly slower demo.

---

### 4.2 `backend/memory/profile.py` - distillation

`ResearcherProfile.distilled_context` is a **≤ 600 character** natural-language paragraph injected into the summary prompt. It is what makes the personalization visible rather than theoretical.

- Regenerate it via `LLMPort.chat(..., call_site="memory_distill")` - this is your only LLM call, and it goes through the same client so Card 1's ledger prices it. Do not call any model directly.
- Regenerate **lazily and rarely**: only when `query_count` crosses a multiple of 3, or when `specialty` changes. Regenerating per query would put a sixth LLM call on the hot path for negligible gain and would visibly distort the cost-per-query number on the economics dashboard.
- Content shape: specialty, the conditions explored so far, and the apparent depth of prior engagement. Nothing else. It must never contain PMIDs, patient-like details, or verbatim query text.
- Hard-truncate at 600 chars after generation. Do not trust the model to obey the limit.

---

### 4.3 `backend/memory/rerank.py` - memory-conditioned re-rank

A pure function, no I/O, fully unit-testable:

```python
def apply_memory(
    results: list[ScoredPaper],
    profile: ResearcherProfile,
    seen: set[str],
) -> tuple[list[ScoredPaper], int]:
    """Returns (reordered results, count of seen papers demoted)."""
```

Rules, in order:

1. **Seen papers are demoted, not deleted, at this layer.** Hard exclusion happens upstream via `RetrievalPort.search(exclude_pmids=...)`; this function is the second-chance path for anything that slipped through. Multiply seen papers by `0.6`.
2. **Condition affinity.** A paper whose `condition` is already in `profile.conditions_explored` gets `1.15`. The user is building a thread on that condition; continuity is the point.
3. **Never let memory outrank rarity.** Cap the combined `memory_multiplier` to the range `[0.6, 1.2]`. The project's core claim is rare-first retrieval; a personalization boost that can flip a rare paper below a common one has broken the product to make a demo point. Assert this cap in a test.
4. Write the applied factor into `ScoredPaper.memory_multiplier` so Card 2B can show it in the UI and so the trace is honest.
5. Re-sort, return.

**Test this against an adversarial case:** a profile that has explored a common condition heavily, and a query whose best match is a rare paper. The rare paper must still win. If it doesn't, the cap in rule 3 is wrong.

---

### 4.4 `backend/app/pipeline.py` - the orchestrator you own

This file replaces the implicit orchestration that was scattered across routes and `llm_client` in v1. One function, one clear order:

```
run_query(query, user_id, session_id, personalize) -> QueryResult
```

1. `request_id = uuid4()`. Thread it through **every** `LLMPort.chat` call. Card 1's `/economics/request/{request_id}` is worthless if you don't.
2. `services = get_services()`.
3. If `personalize`: load profile and `seen_pmids` under the 300 ms budget. Else: empty profile, empty seen set, `memory.applied = false`.
4. **HyDE** (`call_site="hyde"`) → hypothetical document.
5. **Retrieve**: `services.retrieval.search(query, secondary_query=hyde_doc, top_k=10, exclude_pmids=seen if personalize else ())`.
6. **Memory re-rank**: `apply_memory(...)`.
7. **Relevance check** (`call_site="relevance_check"`) → confidence.
8. If below threshold and rounds remain: **refine** (`call_site="refine"`) and loop back to step 5. Max 2 rounds total, unchanged from v1.
9. **Summary** (`call_site="summary"`), with `profile.distilled_context` injected when `personalize` and the context is non-empty.
10. **Citation check** (`call_site="citation_check"`) per claim.
11. Write memory: `record_query(...)`, `record_papers_shown(...)`.
12. Assemble the response including the `memory` and `cost` blocks from §4 of the contracts doc. Aggregate `cost` from the `TokenUsage` on each `ChatResult` you received - **do not query the ledger.** You already hold every number; a read-back would race the ledger's async flush and produce a response that disagrees with itself.

**Every step is individually degradable.** A failed HyDE call falls through to the raw query. A failed relevance check treats the round as passing. A failed citation check returns the summary with `supported: null` on each claim and a truthful flag. The request returns 200 with reduced fidelity, always.

---

### 4.5 Rewire loop / summary / verify onto contracts

`backend/app/loop/{hyde,refine,relevance_check,trace}.py`, `backend/app/summary/generate.py`, `backend/app/verify/citation_check.py`:

- Replace every `ParitokLLMClient` reference with an injected `LLMPort`.
- Every call passes `call_site`, `request_id`, `session_id`, `user_id`. **No exceptions** - an unattributed LLM call is a hole in the cost story.
- Every call that expects structured output passes an explicit `json_schema`. Stop parsing free text with regex.
- `trace.py` gains two fields per round: `memory_applied: bool` and `seen_filtered: int`, so the existing retrieval-trace UI can show that memory did something.
- **Summary prompt change:** when `distilled_context` is present, prepend a `system` message: *"The reader is described as: {distilled_context}. Assume familiarity with material they have already explored; prioritize what is new to them. Do not mention this description in your answer."* The last clause matters - a summary that opens with "As a neuroradiology resident, you'll know..." is a personalization demo that reads as a bug.
- **Do not weaken the citation requirement for personalized summaries.** Every sentence still carries a numbered citation. Personalization changes emphasis, never evidentiary standards.

---

### 4.6 Routes - `memory.py`, `query.py`, `demo.py`, `schemas.py`

Implement exactly the shapes frozen in §4 of the contracts doc. Card 2B has already generated TypeScript from those shapes and is building UI against them.

- `POST /query` - the new `personalize`, `user_id`, `session_id` request fields and the new `memory` + `cost` response blocks.
- `GET /memory/profile`, `POST /memory/specialty`, `POST /memory/forget`, `GET /memory/thread`.
- `demo.py` - keep the fixture demo path working; it is the fallback if live services die during judging. Make it return a plausible `memory` and `cost` block so the UI renders identically in demo mode.
- Rate limits via the existing limiter: 10/min on `/query`, 30/min on memory reads, 5/min on `/memory/forget`.
- **If a frozen shape turns out to be wrong, you do not change it unilaterally.** Log it in `Decisions.md`, tell Codex via `Handoff-Log.md`, change it at the next checkpoint. A response-shape drift is the one thing that breaks Card 2B without touching a single file they own, and it is therefore the failure mode this whole plan is built to prevent.

---

### 4.7 `backend/seed.py`

Rewrite so `python -m backend.seed` runs `run_query` once with a fixed query under the current profile and writes `backend/data/seed_output.json`. Add a `--profile` flag. This is your fastest end-to-end smoke test; keep it working from hour one.

---

### 4.8 Tests - `backend/tests/memory/` and the reassigned files

All must pass under `NEULIT_PROFILE=fake` with **no** `EVEROS_*` and no `SNOWFLAKE_*` env vars.

New, in `backend/tests/memory/`:

- `test_evermind_client.py` - namespacing correctness (user A never reads user B), empty defaults on miss, `forget()` clearing everything, the 60s `seen_pmids` cache.
- `test_memory_degradation.py` - client raises on every call → `/query` still 200, `memory.applied == false`. **This is the most important test on this card.**
- `test_memory_latency_budget.py` - client sleeps past 300 ms → defaults returned, `memory.applied == false`, total added latency bounded.
- `test_rerank.py` - the 0.6 / 1.15 factors, the `[0.6, 1.2]` cap, and the adversarial rare-vs-explored-common case from §4.3.
- `test_profile_distillation.py` - regenerates only on the multiple-of-3 / specialty-change triggers; hard-truncates at 600 chars; never emits a PMID.

Reassigned v1 files, rewritten in place, **filenames kept**: `test_search_loop.py`, `test_loop_prompts.py`, `test_summary_generate.py`, `test_citation_check.py`, `test_pipeline_integration.py`, `test_api_query.py`, `test_api_demo.py`, `test_query_stream.py`, `test_multiturn_session.py`.

`test_pipeline_integration.py` must additionally assert that **exactly six or fewer** `LLMPort.chat` calls occur per non-refining query, each with a distinct `call_site`, and that all share one `request_id`. That single assertion protects the entire cost story from silent regression.

---

## 5. How you and Card 2B stay out of each other's way

You share `branch-2`. These are the mechanics:

1. **Disjoint by language.** You touch `.py`. Codex touches `.ts`, `.tsx`, `.md`, `.d2`. There is no file either of you can both open.
2. **`git pull --ff-only origin branch-2` before every push.** Because your paths are disjoint, this should always fast-forward. **If it ever refuses, one of you edited outside your lane** - do not merge, do not force, find the file.
3. **One `Handoff-Log.md` entry per session, before you stop.** Format:

   ```
   ## [c2a] 2026-08-06T14:20Z
   Changed: backend/app/pipeline.py, backend/api/routes/query.py
   Contract surface touched: POST /query response now populates `memory` block (real, not stub)
   2B can now rely on: memory.applied / seen_filtered / profile_used are truthful under NEULIT_PROFILE=fake
   Still stubbed: cost.by_call_site returns zeros until Card 1's ledger lands at CP2
   Blockers: none
   ```

   The "2B can now rely on" line is the payload. Codex reads that line and nothing else in your diff.
4. **Never read Codex's working tree to "check" something.** If you need to know what the UI expects, read `Contracts.md` in the vault. It is the shared truth; their code is not.
5. **When you stub a response field, say so explicitly in the log.** A field that exists but returns zeros, undeclared, will burn an hour of Codex's time chasing a frontend bug that isn't one.

---

## 6. Sequence and checkpoints

| Order | Work | Done by |
|---|---|---|
| 1 | §4.1 EverOS client + `test_evermind_client.py` | **CP1 gate** |
| 2 | §4.4 `pipeline.py` skeleton on fake services, §4.5 rewiring | before CP2 |
| 3 | §4.3 re-rank, §4.2 distillation | before CP2 |
| 4 | §4.6 routes returning real `memory` block on fake profile | **CP2 gate** |
| 5 | `NEULIT_PROFILE=live_no_snowflake` end-to-end | **CP2 gate** |
| 6 | After CP2 merge: run `NEULIT_PROFILE=live`, populate real `cost` block | **CP3 gate** |
| 7 | §4.8 remaining tests, §4.7 seed | after CP3 |

**At CP2 you will use Card 1's real Snowflake client for the first time.** Budget an hour for the first live run to fail on something dull - a model name, a role grant, a serialization difference in `ScoredPaper`. That is expected and is exactly why CP2 exists at 60% and not at 90%.

---

## 7. Definition of done for Card 2A

- [ ] `EverOSMemory` implements `MemoryPort` with **no signature deviation** from `backend/contracts/ports.py`.
- [ ] Namespacing verified: user A cannot read user B's profile, by test.
- [ ] `/query` returns 200 with `memory.applied == false` when EverOS is fully unreachable, by test.
- [ ] Memory reads respect the 300 ms budget, by test.
- [ ] `apply_memory` cap verified: a rare paper never falls below a common one due to personalization, by test.
- [ ] Exactly one `request_id` per `/query`, threaded through every LLM call, asserted by test.
- [ ] Zero direct `LedgerPort.record()` calls anywhere in your code - verify with `grep -rn "ledger.record" backend/app backend/api backend/memory` returning nothing.
- [ ] Zero imports of `backend.snowflake` - verify with `grep -rn "backend.snowflake" backend/app backend/api backend/memory` returning nothing.
- [ ] `distilled_context` never exceeds 600 chars and never contains a PMID, by test.
- [ ] Personalized summaries still carry a citation on every sentence, by test.
- [ ] `python -m backend.seed` works under `fake`, `live_no_snowflake`, and `live`.
- [ ] All tests green with **zero** credentials in the environment.
- [ ] `git diff --name-only contracts-v1...branch-2 -- '*.py'` contains no path outside §2 and no FROZEN file.
- [ ] A `Handoff-Log.md` entry exists for every work session.
