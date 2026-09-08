---
title: API Reference
---

# API Reference

The live OpenAPI document is available at `/openapi.json`. The frontend regenerates TypeScript definitions with `npm run types:gen`.

## Query

### `POST /query` and `POST /query/stream`

Request: `{ query, session_id, user_id, personalize }`.

The response contains `request_id`, `summary_markdown`, citations, scored papers, retrieval trace, an optional region, memory effects, and a cost block. The stream emits stage events followed by the same response under `done`.

## Memory

| Endpoint | Purpose |
|---|---|
| `GET /memory/profile?user_id=` | Specialty, conditions, counts, distilled context |
| `POST /memory/specialty` | Set `{ user_id, specialty }` |
| `POST /memory/forget` | Clear `{ user_id }` |
| `GET /memory/thread?user_id=&session_id=` | Queries and PMIDs shown |

## Economics

| Endpoint | Purpose |
|---|---|
| `GET /economics/summary?window=24h` | Requests, tokens, USD, call sites, hours |
| `GET /economics/request/{request_id}` | Per-call usage, latency, cost, degradation |
| `POST /economics/ask` | Staged Analyst contract; clean unavailable response until the dedicated REST integration exists |

The frozen summary contract does not expose median cost per query. The UI marks that headline unavailable instead of presenting an average as a median.

The current live handoff confirms that the Analyst-over-COMPLETE call shape is invalid. The endpoint degrades without a 500, but real natural-language answers require the separate Cortex Analyst REST integration and a staged semantic model.

## Health and reference data

- `GET /health` returns retrieval, LLM, memory, and ledger states.
- `GET /conditions` returns corpus condition metadata.
- `GET /demo-contrast` returns fixed naive and rarity-weighted rankings.
- `GET /atlas`, `/atlas/query`, and `/atlas/{condition_name}` return atlas views.
