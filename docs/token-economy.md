---
title: Token Economy
---

# Token economy

![One query becomes six ledger events](/diagrams/token-ledger.svg)

Every Cortex COMPLETE invocation records call site, prompt and completion tokens, model, USD cost, latency, degradation state, request, session, user, and timestamp. Aggregated views power the dashboard. The Analyst-shaped endpoint currently degrades cleanly because the dedicated Cortex Analyst REST integration is not implemented.

## Cost computation

The ledger prices usage from `MODEL_PRICING`; the browser displays recorded cost rather than recomputing history under today's price. The seed DDL uses 1.8 credits per million input tokens, 9.0 credits per million output tokens, and $2 per credit for the two seeded Claude models. The cheap-tier `mistral-7b` row uses 0.03 input and 0.15 output credits per million tokens; the output rate is published, while the input rate remains an estimate. Production setup must reconcile every row against the account's Cortex rate card.

## Measured result

Card 1's credential-free measurement used 28 retrieval queries and 280 paper abstracts. Extractive selection reduced the estimated prompt context from 64,947 to 42,401 tokens, a 34.71% reduction. On one representative summary-shaped call, the same measured reduction changed computed cost from $0.009000 to $0.00775044, an estimated 13.88% compression-only reduction.

The cache exercise issued each of the 28 HyDE calls twice: 28 misses followed by 28 hits, or 50% across 56 calls. That is an intentionally synthetic repeat pattern that verifies the cache path; it is not an organic production hit-rate claim.

## Call-site routing

The same 56-call measurement routed HyDE from the strong-tier Claude model to `mistral-7b`. Computed cost fell from $0.064512 to $0.0010752, a $0.0634368 or 98.33% reduction for that measured call volume and token shape. This is a real execution of the routing and pricing code, but the cheap-tier input rate is estimated and the list price has not been reconciled with account billing. It is not a production savings claim.

These figures come directly from `backend/measurement/results/decision.md`. They do **not** represent live Snowflake consumption. A later live handoff verified Cortex Search, COMPLETE, and ledger round-trips, but the measurement gate was not rerun against that account. Real cost per query, live call-site latency, account pricing, and Cortex Search recall parity therefore remain unmeasured. The dashboard continues to show only ledger values returned by the running backend.

## What the dashboard proves

The useful question is not only how many tokens the agent used, but which pipeline decision spent them. Call-site bars expose that distribution; per-request cost ties it to the answer a researcher just received.
