# LlamaParse to Cortex Search path

`backend.app.corpus.layout_parse.parse_layout_llamaparse` calls the exact
`llama_parse.LlamaParse` SDK when `LLAMA_CLOUD_API_KEY` is present. Every chunk
keeps its document id, LlamaParse page number, source SHA-256, and authoritative
job id from the SDK's `JobResult`. Current clients use `parse` followed by
`get_markdown_documents(split_by_page=True)`; older injected clients retain the
`load_data` fallback. Missing page metadata stays page 0 and cannot be
presented as page-grounded evidence.

`backend.snowflake.layout_retrieval` installs `NEULIT.CORE.LAYOUT_CHUNKS` and
`NEULIT.CORE.LAYOUT_SEARCH`, loads chunks with bound values, queries via
`SNOWFLAKE.CORTEX.SEARCH_PREVIEW`, deletes every chunk for a document, refreshes
the search service, and verifies zero base rows remain. Its evaluation helper
reports document-page recall at k, elapsed time, and the before/after aggregate
from `CORTEX_SEARCH_DAILY_USAGE_HISTORY`. That credit delta is explicitly a
daily service aggregate, not a per-query charge. Callers can provide a bounded
warehouse instead of being forced onto `NEULIT_WH`; all object and warehouse
identifiers are allowlisted before interpolation.
Existing services are retained and refreshed instead of replaced on every
ingest, and serving auto-suspends after 60 idle seconds. Usage queries include
the database and schema as well as the service name.

The contract suite uses injected clients and sessions. A separate live
LlamaParse 0.6.94 run parsed permitted document PMC2395620 into 16 chunks across
pages 1 through 4. Every chunk kept a page, the source hash matched, and one
authoritative job id covered the result. A four-query page-citation BM25 check
scored LlamaParse at 3/4 top-1 and 0.875 MRR, compared with 4/4 and 1.0 for the
local layout PyMuPDF parser. LlamaParse also took 5.446 seconds from its cached
parse versus 0.377 seconds for the selected local layout parser. The negative
result is retained, so Trace does not claim that LlamaParse improved retrieval.

The selected local parser was then loaded into a live Snowflake Cortex Search
service as 15 chunks across the same four pages. The same four citation queries
scored 4/4 top-1, 1.0 MRR, and 373.066 ms p95. A permitted-record purge deleted
all 15 base chunks, refreshed the service, and returned no indexed result on the
first post-refresh check. The isolated X-Small warehouse consumed exactly
0.045 compute credits and zero cloud-services credits. The immediate Cortex
daily-usage view had no row; because that view can lag, this is not a zero-cost
claim. The service, table, temporary schema, and warehouse were dropped after
the proof.
