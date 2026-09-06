"""FastAPI dependency accessors. get_services() itself is already a cached
singleton (backend.contracts.registry, FROZEN); this only exposes it via
Depends() so routes that touch ports directly (not through pipeline.run_query,
which resolves services on its own) can be swapped in tests via
app.dependency_overrides.

The retrieval port is wrapped in the retention filter on the way out, so a
tombstoned document cannot be served by a route that reaches the port
directly (`GET /demo-contrast` is the one that does today). `run_query`
applies the same wrapper on its own path. `retention.enforced` returns the
port unchanged when no tombstone exists, so with an empty log this is exactly
`get_services()` and the singleton's identity is preserved.
"""
from __future__ import annotations

from dataclasses import replace

from backend.app.corpus.retention import enforced as retention_enforced
from backend.contracts.registry import Services, get_services


def get_services_dep() -> Services:
    services = get_services()
    retrieval = retention_enforced(services.retrieval)
    if retrieval is services.retrieval:
        return services
    return replace(services, retrieval=retrieval)
