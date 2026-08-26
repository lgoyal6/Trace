# NeuLitTrace v2 - Snowflake + EverMind rebuild plan

Paritok is removed. Snowflake replaces retrieval, inference, and the cost story. EverMind adds the memory layer.

## Read in this order

| File | Who reads it |
|---|---|
| `00-SHARED-CONTRACTS.md` | **Everyone, first, before writing any code.** Defines the freeze commit, the ports, the fakes, the file-ownership matrix, the git protocol, and the Obsidian coordination surface. |
| `01-PHASE-CARD-1-snowflake-platform.md` | Teammate. **50%.** `branch-1`, solo, Python + SQL. |
| `02-PHASE-CARD-2A-evermind-memory.md` | Bryan's Claude Code. **25%.** `branch-2`, Python only. |
| `03-PHASE-CARD-2B-experience-layer.md` | Bryan's Codex. **25%.** `branch-2`, TypeScript + Markdown only. |

## The four decisions that shaped this

1. **Prompt compression is not replaced.** The token-economy story moves from *compressing* spend to *measuring* it: every LLM call writes a priced row to a Snowflake `TOKEN_LEDGER`, and Cortex Analyst answers cost questions in natural language.
2. **Snowflake Cortex `COMPLETE` is the only inference path.** Groq and Gemini are deleted. One provider is what makes the ledger's numbers trustworthy.
3. **Retrieval moves fully into Cortex Search.** `rank-bm25` and `sentence-transformers` are deleted; the rarity boost survives unchanged as a post-retrieval re-rank.
4. **EverOS stores a researcher profile and session thread.** Personalization is capped so it can never outrank rarity - that cap is the design decision, not an implementation detail.

## Why the plan is shaped this way

Three workstreams, two branches, one repo, one weekend. Merge conflicts are the predictable failure, so the plan prevents them structurally rather than by asking people to be careful:

- **A freeze commit lands first**, containing the ports, the DI registry, every stub file, the split requirements, and `.env.example`. After it, nobody ever edits a wiring file to plug their work in.
- **`backend/contracts/fakes.py` ships in that same commit**, so all three lanes start at minute zero instead of two of them waiting on Snowflake.
- **File ownership is exhaustive and verified** - every path in the repo resolves to exactly one owner, with zero overlaps.
- **Lanes are separated by language.** Card 2A writes Python, Card 2B writes TypeScript. They share a branch and cannot open the same file.
- **A pre-commit tripwire** fails any commit touching a path outside the operator's lane.
- **Agent instructions use a fixed, verbatim preamble.** Paraphrasing it is how an agent talks itself into a small edit in the other lane, and that edit is the conflict.

## Start here

```bash
git checkout main && git pull
# make the freeze commit per 00-SHARED-CONTRACTS.md §2
git tag contracts-v1 && git push origin main --tags
git checkout -b branch-1 contracts-v1   # teammate
git checkout -b branch-2 contracts-v1   # Bryan
```
