-- NeuLitTrace v2 - Card 1. Run after 02_tables.sql and after PAPERS is loaded
-- (backend/app/corpus/build_corpus.py --to-snowflake).
USE ROLE NEULIT_APP;
USE WAREHOUSE NEULIT_WH;
USE DATABASE NEULIT;
USE SCHEMA CORE;

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

-- Poll until ACTIVE before assuming retrieval is broken:
-- SHOW CORTEX SEARCH SERVICES IN SCHEMA NEULIT.CORE;

-- During corpus-tuning, TARGET_LAG='1 hour' means reloading PAPERS does not
-- immediately update the index. Either re-run this CREATE OR REPLACE, or
-- temporarily set TARGET_LAG = '1 minute' and set it back afterward - a
-- short lag burns credits continuously.
