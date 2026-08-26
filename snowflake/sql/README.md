# Snowflake DDL - run order

Run once, by hand, against the hackathon account. Requires `SNOWFLAKE_*` env
vars (see `.env.example` / `config/snowflake.yaml`) or an equivalent
`snowsql` connection profile.

## Order

1. `01_setup.sql` - warehouse, database, schema, `NEULIT_APP` role, grants (incl. `SNOWFLAKE.CORTEX_USER`).
2. `02_tables.sql` - `PAPERS`, `CONDITIONS`, `TOKEN_LEDGER`, `MODEL_PRICING` (+ seed row for the default model).
3. Load data: `python -m backend.app.corpus.build_corpus --to-snowflake` (reads `backend/data/corpus.json`, writes `PAPERS`/`CONDITIONS`, embeds `CONDITION_VEC`).
4. `03_search_service.sql` - Cortex Search Service over `PAPERS.SEARCH_BLOB`. Poll `SHOW CORTEX SEARCH SERVICES IN SCHEMA NEULIT.CORE;` until `ACTIVE`.
5. `04_views.sql` - cost views read by `/economics/*` and Cortex Analyst.
6. `semantic_model.yaml` - upload/reference for a Cortex Analyst semantic model stage (`live` profile only).

## snowsql command line

```bash
snowsql -a "$SNOWFLAKE_ACCOUNT" -u "$SNOWFLAKE_USER" \
  -r NEULIT_APP -w NEULIT_WH -d NEULIT -s CORE \
  -f snowflake/sql/01_setup.sql

snowsql -a "$SNOWFLAKE_ACCOUNT" -u "$SNOWFLAKE_USER" \
  -r NEULIT_APP -w NEULIT_WH -d NEULIT -s CORE \
  -f snowflake/sql/02_tables.sql

# then run the Python loader (step 3 above), then:

snowsql -a "$SNOWFLAKE_ACCOUNT" -u "$SNOWFLAKE_USER" \
  -r NEULIT_APP -w NEULIT_WH -d NEULIT -s CORE \
  -f snowflake/sql/03_search_service.sql

snowsql -a "$SNOWFLAKE_ACCOUNT" -u "$SNOWFLAKE_USER" \
  -r NEULIT_APP -w NEULIT_WH -d NEULIT -s CORE \
  -f snowflake/sql/04_views.sql
```

## Verification gates

- `SELECT COUNT(*) FROM NEULIT.CORE.PAPERS;` → 329
- `SELECT COUNT(*) FROM NEULIT.CORE.CONDITIONS;` → 14, with `SELECT COUNT(*) FROM NEULIT.CORE.CONDITIONS WHERE IS_RARE;` → 10
- `SHOW CORTEX SEARCH SERVICES IN SCHEMA NEULIT.CORE;` → state `ACTIVE`

Paste all of the above into `Handoff-Log.md` at CP1. **Not run in this
sandbox** - no live Snowflake credentials are available here; see
`Blockers.md`.
