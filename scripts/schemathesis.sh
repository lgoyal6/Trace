#!/bin/bash
# Run Schemathesis against the committed contract and a real, isolated Trace.
#
# backend/tests/ drives the app in-process; this drives it over a socket, which is the
# only way to observe what Schemathesis observes - byte-level bodies, response headers
# and multi-request sequences. The server is backend/tests/isolated_server.py, so
# NEULIT_PROFILE=fake resolves every port to backend.contracts.fakes: no Snowflake
# account, no EverOS key, no PubMed traffic, and the generated corpus - which includes
# POST /memory/forget - cannot delete anything real.
#
#     scripts/schemathesis.sh            # the standard run
#     PORT=8899 EXAMPLES=1000 scripts/schemathesis.sh
#
# Two reports are expected and are not defects. Schemathesis treats an undeclared query
# parameter as a schema violation the API must reject; OpenAPI has no way to forbid one,
# and refusing every unknown query parameter would break any client that appends a
# cache-buster. They are left standing rather than silenced so that the next person sees
# the same output this one did.
set -euo pipefail

PORT="${PORT:-8732}"
EXAMPLES="${EXAMPLES:-300}"
SEED="${SEED:-20260905}"
ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"

NEULIT_PROFILE=fake python "$ROOT/backend/tests/isolated_server.py" --port "$PORT" &
SERVER=$!
trap 'kill $SERVER 2>/dev/null || true' EXIT

for _ in $(seq 1 60); do
    curl -sf "http://127.0.0.1:$PORT/health" >/dev/null 2>&1 && break
    sleep 0.5
done

# uvx keeps the tool out of the service's own dependency set: Schemathesis is a client,
# it has no business in the image, and pinning the version here is what makes two runs
# comparable.
uvx schemathesis@4.25.2 run "$ROOT/frontend/.openapi.json" \
    --url "http://127.0.0.1:$PORT" \
    --checks all \
    --phases examples,coverage,fuzzing,stateful \
    -n "$EXAMPLES" \
    --seed "$SEED" \
    --continue-on-failure \
    "$@"
