# 00 - SHARED CONTRACTS & MERGE PROTOCOL

Read this before opening any phase card. Nobody writes a line of feature code until the freeze commit described here is on main and tagged.

This document exists for one reason: three workstreams are going to touch one repository at the same time, and merge conflicts in a hackathon cost more than the features they were fighting over. Every rule below is a mechanical rule, not a guideline. If a rule and your judgment disagree, follow the rule and file a note in Obsidian.

## 0. What changed and why

The old build (NeuLitTrace v1) was a rare-neuroimaging RAG whose token-economy story came from Paritok prompt compression. Paritok is removed entirely. The v2 stack is:

| Concern | v1 | v2 |
|---|---|---|
| Prompt compression | Paritok CompressionPipeline | Removed. No replacement. |
| Token economy story | "we compressed 40.9% of the summary prompt" | Snowflake cost ledger - every LLM call writes prompt/completion tokens + priced USD to TOKEN_LEDGER; Cortex Analyst answers cost questions in natural language |
| Inference | Groq llama-3.3-70b-versatile + Gemini failover | Snowflake Cortex COMPLETE for all six call sites. Groq and Gemini clients are deleted. |
| Retrieval | in-memory rank-bm25 + local sentence-transformers over corpus.json | Snowflake Cortex Search Service (native hybrid lexical + vector) over a PAPERS table, with the rarity boost applied as a post-retrieval re-rank |
| Corpus storage | backend/data/corpus.json | Snowflake NEULIT.CORE.PAPERS / CONDITIONS tables (the JSON stays in-repo only as migration source + fake fixture) |
| Memory / personalization | none | EverMind EverOS - researcher profile, cross-session thread, seen-paper ledger, memory-conditioned re-rank and summary |
| Brain atlas | nilearn local lookup | unchanged |
| Rate limiting | slowapi | unchanged |

Deliberate non-goals. We are not writing our own compressor. We are not keeping a Groq fallback. We are not building live PubMed ingestion. If you find yourself doing any of those, stop and file a blocker.

## 1. The three lanes

| Lane | Card | Weight | Branch | Operator | Language allowed |
|---|---|---|---|---|---|
| Snowflake platform | 01-PHASE-CARD-1 | 50% | branch-1 | Teammate, working solo | Python + SQL + YAML |
| EverMind memory & orchestration | 02-PHASE-CARD-2A | 25% | branch-2 | Bryan's Claude Code | Python only |
| Experience & docs layer | 03-PHASE-CARD-2B | 25% | branch-2 | Bryan's Codex | TypeScript + Markdown + D2 only |

### The language rule (this is the primary conflict prevention mechanism)

Card 2A may not create, edit, or delete any file ending in .ts, .tsx, .css, .mjs, .json under frontend/, or any file under docs/.

Card 2B may not create, edit, or delete any file ending in .py, .sql, or any file under backend/.

Card 1 may not create, edit, or delete any file under frontend/, docs/, backend/memory/, backend/app/loop/, backend/app/summary/, or backend/app/verify/.

Cards 2A and 2B share a branch, so this language split is what keeps them from colliding. They communicate through two surfaces only: the frozen HTTP contract (§4) and the Obsidian vault (§7). They never read each other's uncommitted work.

## 2. The freeze commit

One person (Bryan) does this before either branch is cut. It lands on main as a single commit, then:

```bash
git commit -m "freeze: v2 contracts, stubs, and ownership boundaries"
git tag contracts-v1
git push origin main --tags
```

After this tag, no file listed in §2.1 through §2.6 may be modified by anyone on any branch without a joint decision recorded in Obsidian Decisions.md and a new tag contracts-v2 cut on main and merged into both branches at the same time.

### 2.1 Delete these

```
paritok.yaml
docs/paritok-integration.md
docs/paritok-feedback.md
docs/public/diagrams/paritok-impact.d2
docs/public/diagrams/paritok-impact.svg
docs/public/diagrams/paritok-usage.d2
docs/public/diagrams/paritok-usage.svg
backend/tests/test_compression.py
backend/tests/test_candidate_a_loop.py        # Paritok A/B gate — the comparison no longer exists
backend/tests/test_candidate_b_summary.py     # ditto
backend/app/llm_client.py                     # replaced by backend/app/llm/ (Card 1)
backend/app/retrieval/bm25_index.py
backend/app/retrieval/vector_index.py
backend/app/retrieval/hybrid.py
backend/tests/test_bm25_index.py
```

Deleting backend/app/llm_client.py at freeze time, not on a branch, is deliberate: it is the one file both Card 1 and Card 2A would otherwise have reason to open. Removing it before either branch exists means neither can. Card 2A imports LLMPort from backend.contracts; Card 1 builds backend/app/llm/ fresh.

backend/data/corpus.json stays - it is the migration source for Card 1 and the fake fixture for Cards 2A/2B.

### 2.2 Create backend/contracts/ - FROZEN

This package is the seam. Both branches import from it; nobody edits it.

backend/contracts/__init__.py - re-exports everything below.

backend/contracts/models.py

```python
"""FROZEN at tag contracts-v1. Do not edit on a feature branch."""
from __future__ import annotations
from dataclasses import dataclass, field
from typing import Literal

CallSite = Literal[
    "hyde",             # query expansion
    "relevance_check",  # loop gate
    "refine",           # query rewrite
    "summary",          # sourced summary generation
    "citation_check",   # per-claim verification
    "memory_distill",   # EverOS profile summarization
]

@dataclass(frozen=True)
class Paper:
    pmid: str
    title: str
    abstract: str
    journal: str
    year: int
    condition: str
    is_rare: bool
    url: str

@dataclass(frozen=True)
class ScoredPaper:
    paper: Paper
    score: float             # final score after every adjustment
    lexical_score: float     # 0.0 if the backend does not expose it
    semantic_score: float    # 0.0 if the backend does not expose it
    rarity_multiplier: float # 1.0 == no boost applied
    memory_multiplier: float = 1.0  # set only by Card 2A's re-ranker

@dataclass(frozen=True)
class ConditionMatch:
    condition: str
    similarity: float
    paper_count: int
    is_rare: bool

@dataclass(frozen=True)
class Message:
    role: Literal["system", "user", "assistant"]
    content: str

@dataclass(frozen=True)
class TokenUsage:
    prompt_tokens: int
    completion_tokens: int
    total_tokens: int
    model: str
    cost_usd: float

@dataclass(frozen=True)
class ChatResult:
    content: str
    usage: TokenUsage
    degraded: bool = False
    error: str | None = None

@dataclass(frozen=True)
class LedgerEvent:
    request_id: str
    session_id: str
    user_id: str
    call_site: CallSite
    usage: TokenUsage
    latency_ms: int
    degraded: bool
    occurred_at_iso: str

@dataclass(frozen=True)
class ResearcherProfile:
    user_id: str
    specialty: str | None
    conditions_explored: list[str] = field(default_factory=list)
    query_count: int = 0
    distilled_context: str = ""   # <= 600 chars, injected into summary prompt

@dataclass(frozen=True)
class SessionThread:
    session_id: str
    user_id: str
    queries: list[str] = field(default_factory=list)
    pmids_shown: list[str] = field(default_factory=list)
```

backend/contracts/ports.py

```python
"""FROZEN at tag contracts-v1. Do not edit on a feature branch."""
from __future__ import annotations
from typing import Protocol, Sequence
from backend.contracts.models import *  # noqa

class RetrievalPort(Protocol):
    def search(
        self,
        query: str,
        *,
        secondary_query: str | None = None,
        top_k: int = 10,
        apply_rarity: bool = True,
        exclude_pmids: Sequence[str] = (),
    ) -> list[ScoredPaper]: ...

    def closest_conditions(self, query: str, top_n: int = 3) -> list[ConditionMatch]: ...

    def get_by_pmids(self, pmids: Sequence[str]) -> list[Paper]: ...

    def health(self) -> dict: ...

class LLMPort(Protocol):
    def chat(
        self,
        messages: list[Message],
        *,
        call_site: CallSite,
        request_id: str,
        session_id: str,
        user_id: str,
        json_schema: dict | None = None,
        max_output_tokens: int = 1024,
    ) -> ChatResult: ...

    def health(self) -> dict: ...

class MemoryPort(Protocol):
    def get_profile(self, user_id: str) -> ResearcherProfile: ...
    def get_thread(self, user_id: str, session_id: str) -> SessionThread: ...
    def record_query(self, user_id: str, session_id: str, query: str,
                     matched_conditions: Sequence[str]) -> None: ...
    def record_papers_shown(self, user_id: str, session_id: str,
                            pmids: Sequence[str]) -> None: ...
    def seen_pmids(self, user_id: str) -> set[str]: ...
    def set_specialty(self, user_id: str, specialty: str) -> None: ...
    def forget(self, user_id: str) -> None: ...
    def health(self) -> dict: ...

class LedgerPort(Protocol):
    def record(self, event: LedgerEvent) -> None: ...
    def health(self) -> dict: ...
```

Contract obligations, stated so there is no ambiguity:

RetrievalPort.search must honour exclude_pmids by removing those PMIDs from results before truncating to top_k. Card 2A depends on this for seen-paper dedup; Card 1 must implement it.
LLMPort.chat must call LedgerPort.record exactly once per invocation, including on the degraded path (with zeroed usage and degraded=True). Card 1 owns this. Card 2A must never write to the ledger directly.
Every port method must return rather than raise on backend failure. search returns [], chat returns a degraded ChatResult, memory methods return empty defaults. A missing Snowflake credential must degrade the feature, never 500 the request.
health() returns {"ok": bool, "detail": str} on every port. Card 2B renders these.

backend/contracts/registry.py - FROZEN. Lazy string-path DI so neither branch ever edits an import list:

```python
"""FROZEN at tag contracts-v1."""
from __future__ import annotations
import functools, importlib, os
from dataclasses import dataclass
import yaml
from backend.contracts.ports import LLMPort, LedgerPort, MemoryPort, RetrievalPort

_CONFIG = os.environ.get("NEULIT_SERVICES_CONFIG", "config/services.yaml")

@dataclass
class Services:
    retrieval: RetrievalPort
    llm: LLMPort
    memory: MemoryPort
    ledger: LedgerPort

def _load(path: str):
    module_path, _, cls_name = path.rpartition(".")
    return getattr(importlib.import_module(module_path), cls_name)()

@functools.lru_cache(maxsize=1)
def get_services() -> Services:
    profile = os.environ.get("NEULIT_PROFILE", "fake")
    with open(_CONFIG) as fh:
        cfg = yaml.safe_load(fh)[profile]
    return Services(
        retrieval=_load(cfg["retrieval"]),
        llm=_load(cfg["llm"]),
        memory=_load(cfg["memory"]),
        ledger=_load(cfg["ledger"]),
    )
```

config/services.yaml - FROZEN, and it already names classes that do not work yet:

```yaml
fake:      # default. Runs with zero credentials. Cards 2A/2B live here until integration.
  retrieval: backend.contracts.fakes.FakeRetrieval
  llm:       backend.contracts.fakes.FakeLLM
  memory:    backend.contracts.fakes.FakeMemory
  ledger:    backend.contracts.fakes.FakeLedger

live:      # everything real
  retrieval: backend.snowflake.retrieval.CortexSearchRetriever
  llm:       backend.snowflake.llm.CortexLLMClient
  memory:    backend.memory.evermind.EverOSMemory
  ledger:    backend.snowflake.ledger.SnowflakeLedger

live_no_memory:   # Card 1's integration profile — no EverOS credentials needed
  retrieval: backend.snowflake.retrieval.CortexSearchRetriever
  llm:       backend.snowflake.llm.CortexLLMClient
  memory:    backend.contracts.fakes.FakeMemory
  ledger:    backend.snowflake.ledger.SnowflakeLedger

live_no_snowflake: # Card 2A's integration profile — no Snowflake credentials needed
  retrieval: backend.contracts.fakes.FakeRetrieval
  llm:       backend.contracts.fakes.FakeLLM
  memory:    backend.memory.evermind.EverOSMemory
  ledger:    backend.contracts.fakes.FakeLedger
```

This file existing pre-freeze is what removes the biggest conflict in the repo: nobody ever edits a wiring file to add their implementation.

backend/contracts/fakes.py - FROZEN. Deterministic in-process implementations of all four ports, backed by backend/data/corpus.json and canned LLM responses keyed by call_site. Requirements:

FakeRetrieval does naive token-overlap scoring over the corpus JSON, applies the same rarity multiplier formula as v1, honours exclude_pmids, and is fully deterministic.
FakeLLM returns a fixed valid JSON payload per call_site (so json_schema consumers parse successfully), reports plausible token counts, and records to the injected ledger.
FakeMemory is a process-local dict.
FakeLedger appends to an in-memory list exposed as .events.

Without the fakes, Cards 2A and 2B are blocked on Card 1 finishing. With them, all three lanes start at minute zero. This is the single highest-leverage item in the freeze commit - do not skip it or half-build it.

### 2.3 Create empty stub modules - FROZEN filenames, owned bodies

Each stub is one file containing a class that raises NotImplementedError. The file must exist at freeze time so that config/services.yaml and main.py never need editing later. The body is owned by exactly one card.

| Stub file | Body owned by |
|---|---|
| backend/snowflake/__init__.py | Card 1 |
| backend/snowflake/session.py | Card 1 |
| backend/snowflake/retrieval.py | Card 1 |
| backend/snowflake/llm.py | Card 1 |
| backend/snowflake/ledger.py | Card 1 |
| backend/snowflake/analyst.py | Card 1 |
| backend/api/routes/economics.py | Card 1 |
| backend/memory/__init__.py | Card 2A |
| backend/memory/evermind.py | Card 2A |
| backend/memory/profile.py | Card 2A |
| backend/memory/rerank.py | Card 2A |
| backend/api/routes/memory.py | Card 2A |
| backend/app/pipeline.py | Card 2A |

Stub route modules must already return HTTP 501 {"detail": "not implemented"} from every declared endpoint, with the final path and response shape declared in §4.

### 2.4 Freeze backend/api/main.py

Rewrite once, at freeze time, to import and include all routers including economics and memory. Add /health fan-out over the four ports. After freeze, this file is untouchable.

```python
app.include_router(query_router)
app.include_router(demo_router)
app.include_router(conditions_router)
app.include_router(atlas_router)
app.include_router(economics_router)   # stub at freeze; Card 1 fills the module
app.include_router(memory_router)      # stub at freeze; Card 2A fills the module
```

### 2.5 Split the dependency files

backend/requirements.txt - FROZEN, contents exactly:

```
-r requirements-base.txt
-r requirements-snowflake.txt
-r requirements-memory.txt
```

| File | Owner | Seeded with |
|---|---|---|
| backend/requirements-base.txt | FROZEN | fastapi, uvicorn, pydantic, slowapi, nilearn, numpy, pyyaml, pytest, pytest-asyncio, httpx |
| backend/requirements-snowflake.txt | Card 1 | snowflake-snowpark-python, snowflake-connector-python |
| backend/requirements-memory.txt | Card 2A | EverOS SDK pin |

Card 2B never touches Python deps. frontend/package.json is Card 2B's alone.

### 2.6 Freeze the env template

.env.example - FROZEN, containing every key any lane will need, so nobody adds one later:

```
NEULIT_PROFILE=fake
NEULIT_SERVICES_CONFIG=config/services.yaml
FRONTEND_ORIGIN=http://localhost:3000

# --- Card 1 ---
SNOWFLAKE_ACCOUNT=
SNOWFLAKE_USER=
SNOWFLAKE_PASSWORD=
SNOWFLAKE_ROLE=NEULIT_APP
SNOWFLAKE_WAREHOUSE=NEULIT_WH
SNOWFLAKE_DATABASE=NEULIT
SNOWFLAKE_SCHEMA=CORE
SNOWFLAKE_CORTEX_MODEL=claude-3-5-sonnet
SNOWFLAKE_SEARCH_SERVICE=NEULIT.CORE.PAPERS_SEARCH

# --- Card 2A ---
EVEROS_API_KEY=
EVEROS_BASE_URL=
EVEROS_NAMESPACE=neulittrace

# --- Card 2B ---
NEXT_PUBLIC_API_BASE_URL=http://localhost:8000
```

## 3. File ownership matrix

Every path in the repo belongs to exactly one of five buckets. If a path you want to edit is not in your bucket, you do not edit it.

### FROZEN - nobody edits after contracts-v1

```
backend/contracts/**
config/services.yaml
backend/api/main.py
backend/requirements.txt
backend/requirements-base.txt
.env.example
.gitignore
.github/workflows/**
scripts/check-ownership.sh
scripts/ownership.txt
plan-v2/**
backend/__init__.py
backend/api/__init__.py
backend/api/routes/__init__.py
backend/app/__init__.py
backend/app/loop/__init__.py
backend/app/summary/__init__.py
backend/app/verify/__init__.py
backend/app/retrieval/__init__.py
backend/app/corpus/__init__.py
backend/tests/__init__.py
```

Every __init__.py in the tree is created empty at freeze and frozen. They are the classic silent conflict: two lanes each adding one export, same line, same file, for no benefit. Nothing is ever exported from an __init__.py in this repo except backend/contracts/__init__.py, which is written once at freeze.

### Card 1 - branch-1

```
backend/snowflake/**
backend/app/llm/**                    (new package; replaces app/llm_client.py)
backend/app/retrieval/**
backend/app/corpus/**
backend/api/routes/economics.py
backend/api/routes/conditions.py
backend/requirements-snowflake.txt
config/snowflake.yaml                 (new)
snowflake/sql/**                      (new, top level: DDL + semantic model)
backend/tests/snowflake/**            (new dir)
backend/tests/test_hybrid_retrieval.py
backend/tests/test_retrieval_gold_set.py
backend/tests/test_corpus_coverage.py
backend/tests/test_build_corpus.py
backend/tests/test_fetch_pubmed.py
backend/tests/test_llm_client.py       (rewrite in place, keep filename)
backend/tests/test_api_conditions.py
backend/measurement/**
```

### Card 2A - branch-2, Python only

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
config/evermind.yaml                  (new)
backend/tests/memory/**               (new dir)
backend/tests/test_search_loop.py
backend/tests/test_loop_prompts.py
backend/tests/test_summary_generate.py
backend/tests/test_citation_check.py
backend/tests/test_pipeline_integration.py
backend/tests/test_api_query.py
backend/tests/test_api_demo.py
backend/tests/test_query_stream.py
backend/tests/test_multiturn_session.py
backend/tests/test_seed.py
backend/seed.py
```

### Card 2B - branch-2, TypeScript + Markdown only

```
frontend/**
docs/**
README.md
package.json                          (root, VitePress only)
package-lock.json                     (root, moves only with the above)
```

### Untouched by everyone

```
backend/api/routes/atlas.py
backend/api/limiter.py
backend/tests/test_api_atlas.py
backend/tests/test_api_health.py
backend/data/corpus.json
backend/.gitkeep
.claude/settings.local.json
LICENSE
```

If backend/api/routes/atlas.py genuinely needs a change, it goes through the Decisions.md process, not a unilateral edit.

### Git-ignored at freeze - never committed by anyone

```
backend/data/seed_output.json    # regenerated per run; a committed copy is a guaranteed conflict
obsidian/                        # the coordination vault, synced outside git
.env
compress_trace.jsonl             # v1 artifact
```

seed_output.json is the sneakiest conflict in the repo: both Card 1 and Card 2A will run python -m backend.seed repeatedly and it changes every time. Add it to .gitignore and git rm --cached it in the freeze commit.

### Coverage guarantee

Every path currently in the repository resolves to exactly one bucket above, or to the delete list in §2.1, or to the git-ignore list. This was verified mechanically against the working tree at freeze time. When a lane creates a genuinely new top-level path not covered here, that is a Decisions.md entry before the first commit, not after.

## 4. The HTTP contract between Card 2A and Card 2B

This is frozen at contracts-v1 as stub routes. Card 2B builds against these shapes from minute one using the fake profile; Card 2A must make them true. Card 2B never asks Card 2A to change a shape mid-build - if a shape is wrong, it is logged in Decisions.md and changed once, at an integration checkpoint, by both at the same time.

```
POST /query
  req  { query: string, session_id: string, user_id: string,
         personalize: boolean }
  res  { request_id, summary_markdown, citations: Citation[],
         papers: ScoredPaperDTO[], trace: TraceRound[],
         region: BrainRegion | null,
         memory: { applied: boolean, seen_filtered: number,
                   profile_used: boolean, distilled_context: string },
         cost: { total_tokens: number, cost_usd: number,
                 by_call_site: Record<string, {tokens:number, cost_usd:number}> } }

GET  /memory/profile?user_id=
  res  { user_id, specialty, conditions_explored: string[],
         query_count, distilled_context, seen_pmid_count }

POST /memory/specialty        { user_id, specialty }        -> 204
POST /memory/forget           { user_id }                   -> 204
GET  /memory/thread?user_id=&session_id=
  res  { session_id, user_id, queries: string[], pmids_shown: string[] }

GET  /economics/summary?window=24h
  res  { total_requests, total_tokens, total_cost_usd,
         by_call_site: {call_site, requests, tokens, cost_usd}[],
         by_hour: {hour_iso, tokens, cost_usd}[] }

GET  /economics/request/{request_id}
  res  { request_id, calls: {call_site, prompt_tokens, completion_tokens,
         cost_usd, latency_ms, degraded}[], total_cost_usd }

POST /economics/ask           { question: string }
  res  { answer: string, sql: string, rows: object[] }   # Cortex Analyst

GET  /health
  res  { status, ports: { retrieval: Health, llm: Health,
         memory: Health, ledger: Health } }
```

ScoredPaperDTO mirrors ScoredPaper field-for-field in camelCase. Citation is { index: number, pmid: string, supported: boolean, note: string | null }.

Type generation, not hand-typing. Card 2B generates frontend/src/lib/api-types.ts by running the backend on the fake profile and pulling /openapi.json. Command lives in frontend/package.json as npm run types:gen. This is how 2B stays honest about 2A's shapes without reading 2A's Python.

## 5. Git protocol

```bash
# once, by Bryan
git checkout main && git pull
# ... make the freeze commit ...
git tag contracts-v1 && git push origin main --tags

# teammate
git checkout -b branch-1 contracts-v1

# Bryan (both 2A and 2B live here)
git checkout -b branch-2 contracts-v1
```

Rules, in order of importance:

Never git rebase a shared branch. branch-1 and branch-2 are both shared (2A and 2B share the second one).
Never merge branch-1 into branch-2 or vice versa. Integration happens only through main, only at a checkpoint, only with both operators present.
Commit prefix is mandatory and is how we audit lanes: [c1], [c2a], [c2b]. A commit touching files from two lanes is a bug; split it.
On branch-2, 2A and 2B alternate pushes. Before pushing, git pull --ff-only origin branch-2. Because the language rule guarantees disjoint paths, a fast-forward should always be possible. If --ff-only fails, someone broke the language rule. Stop, do not force, find the offending file.
No git push --force on branch-1, branch-2, or main. Ever.
Never edit a FROZEN file to "just make it work." Add an adapter in your own package instead.

### Ownership tripwire

scripts/check-ownership.sh is created at freeze and installed as a pre-commit hook on every machine. It reads scripts/ownership.txt (glob per lane), takes git diff --cached --name-only, and exits non-zero if a staged path falls outside $NEULIT_LANE. Set NEULIT_LANE=c1|c2a|c2b in each operator's shell profile.

```bash
#!/usr/bin/env bash
set -euo pipefail
LANE="${NEULIT_LANE:?set NEULIT_LANE to c1, c2a, or c2b}"
bad=0
while read -r path; do
  if ! grep -q "^${LANE} " scripts/ownership.txt || \
     ! git check-ignore -q "$path" 2>/dev/null && \
     ! awk -v l="$LANE" '$1==l {print $2}' scripts/ownership.txt \
       | while read -r g; do case "$path" in $g) exit 7;; esac; done; [ $? -ne 7 ]
  then
    echo "OWNERSHIP VIOLATION: $LANE may not edit $path"; bad=1
  fi
done < <(git diff --cached --name-only)
exit $bad
```

(Adjust to taste at freeze time; the point is that the hook exists and is loud. A hook that is 90% right and blocks 90% of collisions beats a convention nobody enforces.)

## 6. Integration checkpoints

Three, and only three. Nothing merges to main between them.

| # | Trigger | Card 1 must have | Card 2A must have | Card 2B must have | Merge action |
|---|---|---|---|---|---|
| CP1 | ~25% elapsed | PAPERS + CONDITIONS loaded in Snowflake, Cortex Search Service responding, health() green | EverOSMemory passing its own unit tests against EverOS | npm run types:gen working against fake profile, memory panel rendering fake data | Nothing merges. Each lane posts health() output to Obsidian Handoff-Log.md. |
| CP2 | ~60% elapsed | CortexLLMClient + SnowflakeLedger complete; NEULIT_PROFILE=live_no_memory runs a full query end to end | pipeline.py + memory re-rank complete; NEULIT_PROFILE=live_no_snowflake runs end to end | All UI built against fake profile; docs skeleton done | branch-1 → main, then branch-2 → main, in that order, same sitting. Resolve any conflict by ownership matrix - the owner's version wins, no discussion. |
| CP3 | ~85% elapsed | /economics/* incl. Cortex Analyst working | /query returns real memory + cost blocks | Dashboard + memory UI reading real data; docs finished | Both branches → main. Freeze feature work. Remaining time is demo + README. |

Rule for CP2 and CP3: run NEULIT_PROFILE=live and the full pytest suite on main after merging, before anyone starts again. If main is red, nobody branches off it.

## 7. Obsidian vault - the only cross-lane channel

Cards 2A and 2B are both driven through Obsidian MCP. Vault at obsidian/ (git-ignored, synced separately, never committed). Four notes, and Card 1's operator gets write access too:

| Note | Purpose | Who writes |
|---|---|---|
| Contracts.md | Verbatim copy of §2.2 and §4. Read-only reference. | Bryan at freeze |
| Handoff-Log.md | Append-only. One entry per completed unit of work: [lane] [timestamp] what changed, which contract surface it touches, what the other lane can now rely on. | all three |
| Decisions.md | Any deviation from this document. Must state: what changed, why, which lanes are affected, which tag it lands under. | all three |
| Blockers.md | "I need a change in a file I don't own." Names the file, the change, and the owner. Owner makes the change, replies in-line, deletes the entry. | all three |

Card 2A and Card 2B must both append to Handoff-Log.md before ending a work session. This is the mechanism by which Claude Code and Codex stay coherent without reading each other's diffs. Neither agent should ever be told "go look at what the other one did in git" - they read the log.

Instruction wording rule (this is what keeps two agents from drifting). Every task given to Claude Code or Codex must open with a literal preamble:

> You are operating as Card 2A (Claude Code, Python only) on branch branch-2. You may edit only the paths listed under "Card 2A" in plan-v2/00-SHARED-CONTRACTS.md. You may not edit any .ts, .tsx, or docs/** file. If your task appears to require editing a file you do not own, stop and append an entry to Blockers.md instead. Before you begin, read Handoff-Log.md.

Swap 2A / Claude Code / Python for 2B / Codex / TypeScript as appropriate. Do not paraphrase this preamble - reuse it exactly, every time. Ambiguity in the preamble is where merge conflicts get born.

## 8. Definition of done, shared by all three cards

A card is done when all of the following are true:

Every function it owns is exercised by a test that runs green under NEULIT_PROFILE=fake with no credentials in the environment.
Its live path has been run at least once against real credentials and the output pasted into Handoff-Log.md.
health() for every port it owns returns {"ok": true}.
Its failure path degrades: with the backing service unreachable, /query still returns a 200 with a usable response and a truthful degraded flag.
git diff --name-only contracts-v1...HEAD contains zero paths outside its ownership bucket.
No file in the FROZEN list appears in that diff.
