---
title: Migration from v1
---

# Migration from v1

Version 1 optimized one large prompt through a proxy compression path. That path was removed. Version 2 starts with measurement across every inference call, Snowflake-native retrieval and inference, and visible EverMind personalization.

## The trade-off

Version 1 could claim a 40.9% token reduction on one controlled summary call. Version 2 knows the cost and purpose of every call, which is the prerequisite for reducing the right one. This is a strategy change, not a like-for-like swap.

After measurement was in place, Card 1 added a smaller extractive context compressor and a TTL prompt cache. The credential-free gate measured a 34.71% context-token reduction across 280 abstracts and a 50% hit rate only under a synthetic repeat pattern. Compression still requires Card 2A wiring at the summary and citation-check prompt assembly points before it affects live queries.

## Retrieval parity

The credential-free v2 baseline measured recall@10 of 0.60 overall and 0.67 for rare conditions across 28 queries. This is `FakeRetrieval`, not a live Cortex Search comparison, so it establishes a reproducible floor but does not prove that retrieval quality moved up or down. Live Cortex parity remains unmeasured until the Snowflake search service is loaded and active.

## Removed

- The v1 compression proxy path.
- Groq and Gemini inference clients.
- In-memory indexes as the live retrieval system.

## Added

- Cortex Search, Cortex COMPLETE, the token ledger, and a guarded Analyst API surface whose dedicated REST adapter remains unimplemented.
- EverOS profile, thread, deduplication, and bounded memory re-ranking.
- Honest UI states for independent dependency degradation.
