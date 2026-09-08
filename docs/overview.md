---
title: Overview
---

# NeuLitTrace

NeuLitTrace is a literature-verification tool for rare PET and neuroimaging findings. A researcher describes a finding; the system retrieves relevant papers, writes a sourced summary, verifies its citations, and shows how memory and inference cost affected the answer.

It is a research aid, not a diagnostic tool. The atlas visualizes regions described by the literature rather than interpreting an uploaded scan.

## Stack

- Next.js and React for the application.
- FastAPI behind frozen HTTP contracts.
- Snowflake Cortex Search for retrieval and Cortex COMPLETE for six model call sites.
- A Snowflake token ledger and aggregate views for cost inspection, plus a staged Analyst API contract that returns unavailable until the dedicated REST integration exists.
- EverMind EverOS for the researcher profile, thread, seen-paper history, and personalization.

## What makes the system inspectable

The result UI includes citations, retrieval rounds, memory-applied state, seen-paper filtering, paper-level memory multipliers, and a collapsed cost breakdown. The economy route aggregates the same ledger by call site and hour.

## Local quickstart

Run the backend with `NEULIT_PROFILE=fake` for a credential-free demo, then start the frontend:

```bash
cd frontend
npm install
npm run types:gen
npm run dev
```

The generated `src/lib/api-types.ts` is the frontend contract source of truth.

## Current scope

The corpus contains 329 papers across 14 conditions. Retrieval freshness, authentication, and multi-tenant memory are intentionally outside this demo scope; see the repository README for limitations.
