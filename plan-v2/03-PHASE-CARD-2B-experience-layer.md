# PHASE CARD 2B - EXPERIENCE & DOCUMENTATION LAYER

| | |
|---|---|
| **Weight** | **25% of total project work** |
| **Branch** | `branch-2` (shared with Card 2A) |
| **Operator** | Bryan's **Codex** |
| **Languages** | **TypeScript, TSX, CSS, Markdown, D2 only.** Zero `.py`. Zero `.sql`. Zero files under `backend/`. |
| **Commit prefix** | `[c2b]` |
| **Shell var** | `export NEULIT_LANE=c2b` |
| **Partner card** | Card 2A (Claude Code) - same branch, disjoint files, communicates only via `Handoff-Log.md` and the frozen HTTP contract |
| **Blocked by** | nothing. You develop against `NEULIT_PROFILE=fake` from minute one. |

---

## 0. Preamble to paste at the top of every Codex session - verbatim, do not paraphrase

> You are operating as **Card 2B (Codex, TypeScript and Markdown only)** on branch `branch-2`. You may edit only the paths listed under "Card 2B" in `plan-v2/00-SHARED-CONTRACTS.md`: `frontend/**`, `docs/**`, `README.md`, and the root `package.json`. You may not edit any `.py` or `.sql` file, and you may not edit any file under `backend/`. If a backend change is needed to complete your task, stop and append an entry to `Blockers.md` in the Obsidian vault naming the file, the change, and the owning card. Never edit a backend file yourself, even a one-line fix. Before you begin, read `Handoff-Log.md` and `Contracts.md`. Before you finish, append to `Handoff-Log.md`. Run `git pull --ff-only origin branch-2` before your first commit and before your last.

**The "even a one-line fix" clause is load-bearing.** The realistic failure mode for a coding agent on a shared branch is a small, well-intentioned backend edit to unblock itself. That edit is the merge conflict, and it lands in a file whose owner is mid-refactor.

---

## 1. What you own, in one sentence

**Everything a human looks at.** The Next.js app, the token-economy dashboard, the memory UI, the VitePress documentation site, the architecture diagrams, and the README. You make the Snowflake and EverMind work legible; you never implement any of it.

You do **not** own: any API implementation, any prompt, any retrieval logic, any SQL, any test under `backend/`.

---

## 2. Ownership boundary - the exact list

```
frontend/**
docs/**
README.md
package.json          (root — VitePress deps only, not frontend deps)
```

That is the entire list. Anything else is someone else's.

---

## 3. How you work without the backend existing yet

**You never wait on Card 1 or Card 2A.** The freeze commit shipped `backend/contracts/fakes.py`, so a fully functional backend runs locally with zero credentials:

```bash
NEULIT_PROFILE=fake python -m uvicorn backend.api.main:app --reload --port 8000
```

Run it. Do not read its source - that is Card 2A's lane. Treat it as a black box that speaks the contract.

**Generate your types, never hand-write them.** Add to `frontend/package.json`:

```json
"scripts": {
  "types:gen": "curl -s http://localhost:8000/openapi.json -o .openapi.json && openapi-typescript .openapi.json -o src/lib/api-types.ts"
}
```

`frontend/src/lib/api-types.ts` is generated, committed, and never edited by hand. Regenerate it after every `Handoff-Log.md` entry from Card 2A that mentions a contract surface. **A hand-edited type file is a lie that survives until integration day**, which is the worst possible time to discover it.

---

## 4. Work breakdown

### 4.1 Purge Paritok from the product surface

The v1 UI told a compression story. That story is gone; telling it anyway is worse than telling no story.

- **`frontend/src/components/waking-loader.tsx`** - v1 staged this loader for Paritok's 45-second RunPod cold start. Snowflake's warehouse resume is roughly 3-8 seconds. Retime the stages accordingly and rewrite the copy: *"Waking the Snowflake warehouse"* → *"Searching 329 papers"* → *"Checking relevance"* → *"Writing the sourced summary"*. Keep the component; a staged loader is still correct, just for a shorter and different wait. **Do not delete it** - an unexplained 5-second pause during judging reads as a broken app.
- **`frontend/src/components/hero.tsx`** and `page.tsx` - remove every Paritok badge, mention, and link. Replace with Snowflake + EverMind attribution.
- **`README.md`** - remove the entire "Paritok integration" section, the badge, and the two doc links. Do not replace it with a Snowflake section yet; that comes in §4.6 once the real numbers exist.
- Grep for `paritok` case-insensitively across `frontend/` and `docs/` and resolve every hit. **Leaving one is the kind of detail a judge notices.**

### 4.2 Token economy dashboard - the Snowflake showcase

New route `frontend/src/app/economy/page.tsx` plus components under `frontend/src/components/economy/`.

Consumes `GET /economics/summary`, `GET /economics/request/{id}`, `POST /economics/ask`.

**Four panels:**

1. **Headline strip.** Total requests, total tokens, total spend, and **median cost per query** in the selected window. Cost per query is the number the whole track is about - make it the largest thing on screen.
2. **Cost by pipeline step.** Horizontal bar over `by_call_site`: `hyde`, `relevance_check`, `refine`, `summary`, `citation_check`, `memory_distill`. This visualization is the argument: it shows *where* an agent's money goes, which is the point of instrumenting rather than compressing. Order bars by spend descending, label each with both token count and USD.
3. **Spend over time.** Line chart over `by_hour`. Two series: tokens and USD.
4. **Ask the data.** A text input hitting `POST /economics/ask` (Cortex Analyst). Render the natural-language answer prominently, the returned rows as a table, and the generated SQL in a collapsed `<details>`. Ship it with 3 clickable example questions pre-filled - *"which pipeline step is most expensive?"*, *"what did the last 10 queries cost?"*, *"how many calls degraded?"* - because a blank NL input during a demo is a coin flip and a pre-filled one is a guarantee.

**Per-request cost drilldown.** On the main results page, a collapsed row under the summary showing that specific query's `cost` block, straight from the `/query` response. Cost the user can see attached to the answer they just got is more persuasive than any aggregate.

**Handle the honest states.** `by_call_site` will be all zeros until Card 1's ledger lands at CP2. Render an explicit "ledger not yet reporting" state rather than a chart of zeros. Same for a `degraded` flag on any call: surface it, don't hide it.

### 4.3 Memory UI - the EverMind showcase

Memory that isn't visible isn't a feature. Three surfaces:

1. **`frontend/src/components/memory/profile-panel.tsx`** - a collapsible sidebar panel from `GET /memory/profile`: specialty (editable, `POST /memory/specialty`), conditions explored as chips, query count, seen-paper count, and the `distilled_context` shown verbatim in a quoted block. **Showing the distilled context verbatim is the demo.** It is the moment a judge sees that the agent formed a model of the user rather than just logging events.
2. **Personalization toggle** on the query form, bound to `personalize` in the `POST /query` body. Default **on**. It must be a visible toggle so the same query can be run cold and warm side by side, which is the only way to *show* memory rather than assert it.
3. **Memory effect indicators on results:**
   - A "seen before" marker on any paper whose `memoryMultiplier < 1.0`.
   - A "builds on your work in {condition}" marker where `memoryMultiplier > 1.0`.
   - A line above the results reading *"{seen_filtered} papers you've already read were filtered out"* when `memory.seen_filtered > 0`.
   - When `memory.applied === false`, a quiet neutral note explaining why (personalization off, or memory unavailable). **Never silently render an unpersonalized result as if it were personalized.**
4. **A reset control** wired to `POST /memory/forget`, behind a confirm. You need this to demo cold-start and warm-start back to back without restarting anything.

Extend `frontend/src/components/retrieval-trace.tsx` to show the new per-round `memory_applied` and `seen_filtered` fields Card 2A adds to the trace.

### 4.4 Health surface

A small footer indicator reading `GET /health`, showing four dots for retrieval / llm / memory / ledger. During integration this tells you in one glance whether a failure is yours or the backend's, and during judging it demonstrates that every dependency degrades independently rather than taking the app down.

### 4.5 Frontend tests - `frontend/tests/`

Playwright, against `NEULIT_PROFILE=fake`:

- Query submit → loader stages → results render with citations.
- Personalization toggle off → `memory.applied === false` path renders the neutral note, no false indicators.
- Seen-paper filtering banner appears when `seen_filtered > 0`.
- Economy dashboard renders all four panels; the "ledger not reporting" state renders on an all-zero payload.
- `/memory/forget` clears the profile panel.
- Health footer renders four states including a red one.

Plus a type-safety gate: `npm run types:gen && npm run build` must pass. **If regenerated types break the build, Card 2A changed a contract - that is a `Blockers.md` entry, not a local type patch.**

### 4.6 Documentation - `docs/`

The VitePress site is a real deliverable, not an afterthought. Rewrite it around the new architecture.

**Delete:** `docs/paritok-integration.md`, `docs/paritok-feedback.md`, and the two `paritok-*` diagram pairs (removed at freeze; confirm they are gone).

**Rewrite:**

- `docs/overview.md` - what the product is, unchanged in substance. Update the stack paragraph.
- `docs/architecture.md` - the new component map: Next.js → FastAPI → ports → {Cortex Search, Cortex COMPLETE, EverOS, TOKEN_LEDGER}. **Document the ports pattern explicitly**; it is a genuine architectural point, not internal trivia, because it is why memory and Snowflake fail independently.
- `docs/api-reference.md` - regenerate from `/openapi.json`, including `/memory/*` and `/economics/*`.
- `docs/data-model-corpus.md` - now describes the Snowflake `PAPERS` / `CONDITIONS` tables. Get the DDL from `snowflake/sql/02_tables.sql`; **read it, do not edit it.**
- `docs/data-model-schemas.md` - the contract dataclasses.
- `docs/architecture-llm-paths.md` → rename to `docs/architecture-inference.md`. Six call sites, one client, one model, one ledger row per call.

**New:**

- `docs/token-economy.md` - the flagship page. What the ledger records, how cost is computed from `MODEL_PRICING`, where spend actually goes by call site, and what the numbers came out to. **Every figure on this page must be pulled from `backend/measurement/results/decision.md`, which Card 1 produces.** Do not estimate, do not round in your favour, and cite the sample size. A hackathon judge who catches one invented number discounts every other number on the page.
- `docs/memory.md` - what EverOS stores, the namespacing model, the 300 ms latency budget, the `[0.6, 1.2]` re-rank cap and *why* it exists (personalization must never outrank rarity), and what the degraded path looks like. The cap is the most defensible design decision in the memory layer; make sure the page says so plainly.
- `docs/migration-from-v1.md` - an honest account of what was removed and why. State plainly that prompt compression was dropped and replaced with measurement, and that this is a change in strategy, not a like-for-like swap. **Write the trade-off, don't bury it:** v1 could claim a 40.9% token reduction on one call; v2 can claim it knows the exact cost of every call, which is the prerequisite for reducing any of them. If retrieval quality moved at all in Card 1's parity measurement, report that number here too, in whichever direction it went.

**Diagrams** - D2 sources in `docs/public/diagrams/`, SVGs regenerated:

- `architecture.d2` - update for the new stack.
- `request-flow.d2` - the eleven steps of `pipeline.py` with the six LLM call sites labelled.
- `data-model.d2` - the Snowflake tables and views.
- `deployment.d2` - update; Paritok proxy is gone.
- `reliability.d2` - update to show the four independent degradation paths.
- **New** `token-ledger.d2` - one query fanning out into six ledger rows, aggregating into the views, surfacing in the dashboard and in Cortex Analyst. This is the diagram that explains the whole track submission in one picture; give it the most care.
- **New** `memory-loop.d2` - query → retrieve → memory re-rank → summary → record → next query is better.

**`README.md`** - rewritten last, after CP3, when the real numbers exist. Structure: problem → who it's for → what it is not → how it works → **Snowflake integration** (retrieval, inference, ledger, Analyst, with numbers) → **EverMind integration** (profile, thread, dedup, re-rank, with the cap explained) → stack → structure → quickstart → **limitations, honestly stated** → docs → license.

Keep v1's limitations discipline: name each limitation, then name the future work that addresses it. Add the new ones truthfully - Cortex Search `TARGET_LAG` means the index is not real-time; the corpus is still 14 conditions; memory is single-namespace with no auth, so `user_id` is trusted input and this is a demo-scope decision, not a production one.

---

## 5. How you and Card 2A stay out of each other's way

You share `branch-2`. These are the mechanics:

1. **Disjoint by language.** You touch `.ts`, `.tsx`, `.css`, `.md`, `.d2`. Claude Code touches `.py`. There is no file either of you can both open.
2. **`git pull --ff-only origin branch-2` before every push.** Disjoint paths mean this should always fast-forward. **If it refuses, someone edited outside their lane** - do not merge, do not force, find the file and file it in `Blockers.md`.
3. **One `Handoff-Log.md` entry per session, before you stop.** Format:

   ```
   ## [c2b] 2026-08-06T16:05Z
   Changed: frontend/src/components/memory/profile-panel.tsx, frontend/src/lib/api-types.ts
   Contract surface consumed: GET /memory/profile, POST /memory/specialty
   I am now depending on: distilled_context being non-empty after 3 queries
   Mismatch found: /memory/thread returns pmids_shown as null, types say string[]
   Blockers: filed 1 (see Blockers.md)
   ```

   The "Mismatch found" line is how a contract drift gets caught in minutes rather than at CP3.
4. **Never read Claude Code's Python to figure out a response shape.** Read `Contracts.md` in the vault, or regenerate types from `/openapi.json`. Their in-progress code is not truth; the contract is.
5. **When something on your side is blocked by the backend, build the empty state and move on.** Every panel needs a loading, empty, error, and degraded state anyway. Building those while waiting is real work, not filler.

---

## 6. Sequence and checkpoints

| Order | Work | Done by |
|---|---|---|
| 1 | §3 fake backend running, `types:gen` working | **CP1 gate** |
| 2 | §4.1 Paritok purge, §4.4 health footer | **CP1 gate** |
| 3 | §4.3 memory UI against fake data | before CP2 |
| 4 | §4.2 economy dashboard against fake data, all states | **CP2 gate** |
| 5 | §4.6 docs skeleton - every page exists with headings and TODOs | **CP2 gate** |
| 6 | After CP2 merge: re-point everything at real data, fix drift | **CP3 gate** |
| 7 | §4.6 docs filled with real numbers, diagrams regenerated, README | after CP3 |
| 8 | §4.5 Playwright suite | after CP3 |

**Docs skeleton before CP2 is not busywork.** Documentation written at hour 22 is documentation nobody reads. Having every page stubbed with headings means the last phase is filling in numbers, which takes minutes, instead of designing an information architecture, which takes hours you will not have.

---

## 7. Definition of done for Card 2B

- [ ] `grep -ri paritok frontend/ docs/ README.md` returns **nothing**.
- [ ] `frontend/src/lib/api-types.ts` is generated, never hand-edited, and regenerated after every Card 2A contract entry in `Handoff-Log.md`.
- [ ] `npm run types:gen && npm run build` passes against a live backend on `NEULIT_PROFILE=live`.
- [ ] All four economy panels render, including the "ledger not reporting" state on an all-zero payload.
- [ ] Cortex Analyst input ships with 3 pre-filled example questions that work.
- [ ] Memory panel shows `distilled_context` verbatim.
- [ ] Personalization toggle produces a **visibly different** result set for the same query, cold vs. warm - demonstrated and recorded in `Handoff-Log.md`.
- [ ] `memory.applied === false` renders a neutral explanatory note, never a false indicator.
- [ ] Health footer renders all four ports, and a deliberately-broken port shows red.
- [ ] Every number in `docs/token-economy.md` traces to `backend/measurement/results/decision.md`.
- [ ] `docs/migration-from-v1.md` states the compression-to-measurement trade-off plainly, including the retrieval-parity number in whichever direction it went.
- [ ] Both new diagrams (`token-ledger.d2`, `memory-loop.d2`) exist as D2 source **and** rendered SVG.
- [ ] `npm run docs:build` succeeds with zero dead internal links.
- [ ] Playwright suite green against `NEULIT_PROFILE=fake`.
- [ ] `git diff --name-only contracts-v1...branch-2 -- 'frontend/*' 'docs/*'` - and **no `.py` or `.sql` file anywhere** in your commits. Verify with `git log --author=... --name-only branch-2 | grep -E '\.(py|sql)$'` returning nothing for `[c2b]` commits.
- [ ] A `Handoff-Log.md` entry exists for every work session.
