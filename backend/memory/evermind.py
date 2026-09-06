"""MemoryPort implementation backed by the EverOS REST API.

Every write is namespaced `{EVEROS_NAMESPACE}:{user_id}` (session-scoped
writes append `:{session_id}`), so one user's profile/thread/seen-set can
never leak into another's.

Degradation is mandatory: with EVEROS_API_KEY/EVEROS_BASE_URL unset or the
service unreachable, every read returns an empty default and every write is a
logged no-op. A memory outage must never fail a search.
"""
from __future__ import annotations

import logging
import os
import threading
import time
from typing import Sequence
from uuid import uuid4

import httpx

from backend.contracts.models import ResearcherProfile, SessionThread
from backend.memory.profile import distill_profile, should_distill

logger = logging.getLogger(__name__)

_SEEN_CACHE_TTL_SECONDS = 60.0
_REQUEST_TIMEOUT_SECONDS = 5.0


def _destination_allowed(url: str) -> bool:
    """`backend/app/net/safe_http`'s scheme and address policy, applied to the
    configured base URL. `safe_http.fetch` itself is GET-only and this client
    needs GET/POST/PUT/DELETE, so the policy is reused rather than the
    transport. See `safe_http.assert_destination_allowed` for what that does
    and does not cover.
    """
    from backend.app.net.safe_http import FetchPolicyError, assert_destination_allowed

    try:
        assert_destination_allowed(url)
        return True
    except FetchPolicyError:
        return False
    except Exception:  # resolution failure at import/boot time must not raise
        logger.warning("EverOS destination check failed for %r", url, exc_info=True)
        return False


class EverOSMemory:
    def __init__(self) -> None:
        self._base_url = os.environ.get("EVEROS_BASE_URL", "").rstrip("/")
        self._api_key = os.environ.get("EVEROS_API_KEY", "")
        self._namespace = os.environ.get("EVEROS_NAMESPACE", "neulittrace")
        self._configured = bool(self._base_url and self._api_key)
        if self._configured and not _destination_allowed(self._base_url):
            # EVEROS_BASE_URL is an environment variable with no validation of
            # its own, and every request to it carries `Authorization: Bearer
            # <key>`. A base URL pointing at loopback or link-local space is a
            # credential handed to whatever is listening there. Refusing to
            # configure is the degradation this class already promises: every
            # read returns an empty default and every write is a logged no-op.
            logger.warning(
                "EVEROS_BASE_URL %r is not an allowed destination; EverOS memory disabled",
                self._base_url,
            )
            self._configured = False
        self._client = (
            # follow_redirects=False is httpx's default, but it is stated here
            # rather than relied on: httpx copies request headers onto a
            # redirect target, so a flipped default would hand the bearer
            # token to whoever writes the Location header.
            httpx.Client(timeout=_REQUEST_TIMEOUT_SECONDS, follow_redirects=False)
            if self._configured
            else None
        )
        # user_id -> (expires_at_monotonic, pmids). Explicit eviction on every
        # write, not just TTL expiry, so forget()/record_papers_shown() can
        # never serve a stale seen-set.
        self._seen_cache: dict[str, tuple[float, set[str]]] = {}
        self._seen_cache_lock = threading.Lock()

    def _ns(self, user_id: str, session_id: str | None = None) -> str:
        ns = f"{self._namespace}:{user_id}"
        return f"{ns}:{session_id}" if session_id else ns

    def _request(self, method: str, path: str, **kwargs) -> dict | None:
        """Returns the parsed JSON body, or None on any failure (unconfigured,
        network error, non-2xx, bad JSON). Never raises.
        """
        if not self._configured or self._client is None:
            return None
        try:
            response = self._client.request(
                method,
                f"{self._base_url}{path}",
                headers={"Authorization": f"Bearer {self._api_key}"},
                **kwargs,
            )
            response.raise_for_status()
            return response.json() if response.content else {}
        except Exception:
            logger.warning("EverOS request failed: %s %s", method, path, exc_info=True)
            return None

    def _evict_seen_cache(self, user_id: str) -> None:
        with self._seen_cache_lock:
            self._seen_cache.pop(user_id, None)

    def get_profile(self, user_id: str) -> ResearcherProfile:
        data = self._request("GET", f"/v1/profile/{self._ns(user_id)}")
        if data is None:
            return ResearcherProfile(user_id=user_id, specialty=None)
        return ResearcherProfile(
            user_id=user_id,
            specialty=data.get("specialty"),
            conditions_explored=list(data.get("conditions_explored", [])),
            query_count=int(data.get("query_count", 0)),
            distilled_context=str(data.get("distilled_context", "")),
        )

    def get_thread(self, user_id: str, session_id: str) -> SessionThread:
        data = self._request("GET", f"/v1/thread/{self._ns(user_id, session_id)}")
        if data is None:
            return SessionThread(session_id=session_id, user_id=user_id)
        return SessionThread(
            session_id=session_id,
            user_id=user_id,
            queries=list(data.get("queries", [])),
            pmids_shown=list(data.get("pmids_shown", [])),
        )

    def record_query(
        self, user_id: str, session_id: str, query: str, matched_conditions: Sequence[str]
    ) -> None:
        self._request(
            "POST",
            f"/v1/thread/{self._ns(user_id, session_id)}/query",
            json={"query": query, "matched_conditions": list(matched_conditions)},
        )
        self._maybe_redistill(user_id, specialty_changed=False)

    def record_papers_shown(self, user_id: str, session_id: str, pmids: Sequence[str]) -> None:
        self._request(
            "POST",
            f"/v1/thread/{self._ns(user_id, session_id)}/papers_shown",
            json={"pmids": list(pmids)},
        )
        self._evict_seen_cache(user_id)

    def seen_pmids(self, user_id: str) -> set[str]:
        now = time.monotonic()
        with self._seen_cache_lock:
            cached = self._seen_cache.get(user_id)
            if cached is not None and cached[0] > now:
                return set(cached[1])

        data = self._request("GET", f"/v1/seen/{self._ns(user_id)}")
        pmids = set(data.get("pmids", [])) if data is not None else set()

        with self._seen_cache_lock:
            self._seen_cache[user_id] = (now + _SEEN_CACHE_TTL_SECONDS, pmids)
        return set(pmids)

    def set_specialty(self, user_id: str, specialty: str) -> None:
        self._request(
            "PUT",
            f"/v1/profile/{self._ns(user_id)}",
            json={"specialty": specialty[:120]},
        )
        self._maybe_redistill(user_id, specialty_changed=True)

    def forget(self, user_id: str) -> None:
        self._request("DELETE", f"/v1/namespace/{self._ns(user_id)}")
        self._evict_seen_cache(user_id)

    def health(self) -> dict:
        if not self._configured:
            return {"ok": False, "detail": "EVEROS_API_KEY/EVEROS_BASE_URL not set"}
        if self._request("GET", "/v1/health") is None:
            return {"ok": False, "detail": "EverOS unreachable"}
        return {"ok": True, "detail": "EverOS reachable"}

    def _maybe_redistill(self, user_id: str, *, specialty_changed: bool) -> None:
        """Regenerates and persists distilled_context, but only on the
        multiple-of-3 query-count trigger or a specialty change. This is the
        one call site (`memory_distill`) EverOSMemory drives on its own,
        outside pipeline.py's request_id chain - MemoryPort.record_query has
        no request_id parameter to thread one through, so a fresh request_id
        is minted here for this side-channel call. See Decisions.md.
        """
        try:
            profile = self.get_profile(user_id)
            if not should_distill(profile.query_count, specialty_changed=specialty_changed):
                return
            from backend.contracts.registry import get_services

            new_context = distill_profile(
                get_services().llm,
                profile,
                request_id=str(uuid4()),
                session_id="memory-distill",
                user_id=user_id,
            )
            self._request(
                "PUT",
                f"/v1/profile/{self._ns(user_id)}",
                json={"distilled_context": new_context},
            )
        except Exception:
            logger.warning("EverOS distillation failed for %s", user_id, exc_info=True)
