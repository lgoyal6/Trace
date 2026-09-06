"""Serve the Trace API over real HTTP against the in-process fakes.

Schemathesis drives an HTTP client, so checking actual request and response behaviour
needs a socket rather than a TestClient. The isolation is the same one
`backend/tests/test_api_contract.py` relies on: `NEULIT_PROFILE=fake` resolves every
port to `backend.contracts.fakes`, so there is no Snowflake account, no EverOS key and
no PubMed traffic, and the generated corpus - which includes `POST /memory/forget` -
cannot delete anything real.

The rate limiter is switched off for the same reason that module switches it off: the
limits are real behaviour, but 30/minute on `/memory/profile` and 5/minute on
`/memory/forget` would turn a generated run into a study of 429s rather than of the
routes. `backend/tests/test_api_contract.py` keeps the limiter tests.

    NEULIT_PROFILE=fake python backend/tests/isolated_server.py --port 8732
"""

import argparse
import os
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

os.environ.setdefault("NEULIT_PROFILE", "fake")


def build_app():
    from backend.api.limiter import limiter
    from backend.api.main import app
    from backend.contracts.registry import get_services

    get_services.cache_clear()
    limiter.enabled = False

    services = get_services()

    @app.get("/__memory_store", include_in_schema=False)
    def _dump_memory():
        """Harness-only introspection, deliberately absent from the schema.

        It exists so an authorization check can read what was actually stored without
        going back through `/memory/profile`, which is the route under test. Reading
        the answer back through the code path that granted access lets a scoping bug
        agree with itself.
        """
        memory = services.memory
        return {
            "profiles": sorted(getattr(memory, "_profiles", {})),
            "threads": sorted("|".join(k) for k in getattr(memory, "_threads", {})),
        }

    return app


def main_cli() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--port", type=int, default=8732)
    args = ap.parse_args()

    import uvicorn

    uvicorn.run(build_app(), host="127.0.0.1", port=args.port, log_level="warning")
    return 0


if __name__ == "__main__":
    raise SystemExit(main_cli())
