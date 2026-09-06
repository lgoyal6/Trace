"""EverOS-backed MemoryPort. SERVER ONLY.

Satisfies backend.contracts.ports.MemoryPort, so it drops straight into
config/services.yaml as the `memory:` entry for any profile.

TWO MODES, and the demo can run in either:

  MOCK   default whenever MEMEX_MOCK=1 or no API key is present. Pure
         in-process dicts. Zero network. `forget()` really deletes.
  REAL   HTTP against EverOS (`EVEROS_BASE_URL`, default the Cloud endpoint).
         stdlib urllib only - adding `requests` to their requirements.txt at
         merge time is a dependency fight nobody needs at 14:30.

Every real-mode call degrades to the mock store on any failure. A memory
service that is down must slow the answer, never break it.

--- GOTCHAS, PORTED FROM THE TYPESCRIPT CLIENT ------------------------------

These are hard-won. Do not "simplify" them away.

  1. The api_key must be passed EXPLICITLY on every call
     (`Authorization: Bearer ...`). There is no ambient/implicit auth; a
     missing header does not 401 loudly in every path, it silently returns
     empty results, which reads as "the memory layer found nothing" and wastes
     an hour.
  2. FLUSH -> SEARCH LAG IS 10-15 SECONDS. Writes are not visible to search
     immediately. Never write-then-immediately-read on the demo path. Call
     `prewarm()` at startup and let the lag elapse before the first query.
  3. `no_extraction` from flush is NORMAL, not an error. Short exchanges
     (a turn or two) have no episode boundary to detect. Treat it as a no-op;
     do not log it as a failure and do not retry.
  4. edit/delete are CLOUD-ONLY. They 403/404 against self-hosted OSS. Mock
     mode honours delete unconditionally, which is what makes the "destroy the
     memory on stage" beat safe to rehearse without credentials.
  5. Correct search hits score near 0.35, not near 1.0. A 0.35 is a good hit.
     Do not add a 0.8 relevance threshold; it filters out everything.
"""
from __future__ import annotations

import json
import os
import time
import urllib.error
import urllib.request
from collections import defaultdict
from typing import Any, Sequence

try:  # merged repo
    from backend.contracts.models import ResearcherProfile, SessionThread
except ImportError:  # pragma: no cover - standalone staging use
    from dataclasses import dataclass, field

    @dataclass(frozen=True)
    class ResearcherProfile:  # type: ignore[no-redef]
        user_id: str
        specialty: str | None
        conditions_explored: list[str] = field(default_factory=list)
        query_count: int = 0
        distilled_context: str = ""

    @dataclass(frozen=True)
    class SessionThread:  # type: ignore[no-redef]
        session_id: str
        user_id: str
        queries: list[str] = field(default_factory=list)
        pmids_shown: list[str] = field(default_factory=list)


EVEROS_CLOUD_BASE_URL = "https://api.evermind.ai"

#: Gotcha 2. Seconds between a write and that write being searchable.
FLUSH_TO_SEARCH_LAG_SECONDS = 15

#: Gotcha 5. Correct hits sit near this, not near 1.0.
EXPECTED_GOOD_SCORE = 0.35

#: <= 600 chars per the frozen ResearcherProfile contract.
DISTILLED_CONTEXT_LIMIT = 600

_TIMEOUT_SECONDS = 8.0


def _base_url() -> str:
    return os.environ.get("EVEROS_BASE_URL", EVEROS_CLOUD_BASE_URL).rstrip("/")


def _api_key() -> str:
    return os.environ.get("EVEROS_API_KEY") or os.environ.get("EVEROS_CLOUD_API_KEY") or ""


# --- transport policy -------------------------------------------------------
#
# Every call below sends `Authorization: Bearer <key>`, and the plain
# `urllib.request.urlopen` this used to call follows up to 10 redirects while
# `HTTPRedirectHandler.redirect_request` copies every header except
# content-length/content-type onto the new target. So a 302 from EVEROS_BASE_URL
# handed the API key to whoever wrote the Location header. Two guards, both
# small, neither of which needs `safe_http.fetch` (which is GET-only and cannot
# carry these POSTs):
#
#   1. no redirects at all. This client talks to one configured host; a
#      redirect is not a feature it needs, and refusing is strictly safer than
#      re-checking each hop.
#   2. the destination is checked against safe_http's address policy before
#      the request is written, so an EVEROS_BASE_URL pointing into private
#      space does not get the key either.


class _NoRedirect(urllib.request.HTTPRedirectHandler):
    """Refuse every redirect rather than re-sending the bearer token."""

    def redirect_request(self, req, fp, code, msg, headers, newurl):  # noqa: D102
        return None


def _no_redirect_opener() -> urllib.request.OpenerDirector:
    return urllib.request.build_opener(_NoRedirect)


def _destination_allowed(url: str) -> bool:
    """safe_http's scheme + resolved-address policy, without its transport."""
    try:
        from backend.app.net.safe_http import FetchPolicyError, assert_destination_allowed
    except ImportError:  # pragma: no cover - standalone staging use
        return True
    try:
        assert_destination_allowed(url)
        return True
    except FetchPolicyError:
        return False
    except Exception:
        return False


def _scope() -> dict:
    return {
        "app_id": os.environ.get("EVEROS_APP_ID", "default"),
        "project_id": os.environ.get("EVEROS_PROJECT_ID", "default"),
    }


def mock_mode() -> bool:
    """Mock unless explicitly told otherwise AND a key exists."""
    if os.environ.get("MEMEX_MOCK", "") in ("1", "true", "TRUE"):
        return True
    return not _api_key()


class EverOSMemory:
    """MemoryPort over EverOS, with a process-local mirror that is also the
    fallback store and the whole of mock mode."""

    def __init__(self) -> None:
        self._profiles: dict[str, ResearcherProfile] = {}
        self._threads: dict[tuple[str, str], SessionThread] = {}
        self._seen: dict[str, set[str]] = defaultdict(set)
        self._mock = mock_mode()
        self._remote_errors = 0
        self._prewarmed_at: float | None = None

    # -- transport ----------------------------------------------------------

    def _post(self, path: str, payload: dict) -> dict | None:
        """One EverOS call. Returns None on any failure - callers degrade."""
        if self._mock:
            return None
        key = _api_key()
        # Gotcha 1: explicit key on every single call. Never ambient.
        headers = {
            "Content-Type": "application/json",
            "Authorization": f"Bearer {key}",
        }
        body = json.dumps({**_scope(), **payload}).encode()
        url = f"{_base_url()}{path}"
        if not _destination_allowed(url):
            self._remote_errors += 1
            return None
        req = urllib.request.Request(url, data=body, headers=headers, method="POST")
        try:
            with _no_redirect_opener().open(req, timeout=_TIMEOUT_SECONDS) as resp:
                return json.loads(resp.read().decode() or "{}")
        except (urllib.error.URLError, urllib.error.HTTPError, ValueError, OSError):
            # Degrade silently to the local mirror. A dead memory service must
            # not surface as a 500 on /query during a live demo.
            self._remote_errors += 1
            return None

    def prewarm(self) -> dict:
        """Warm the index at startup and record when.

        Gotcha 2: search cannot see a write for 10-15s. Calling this at boot
        means the lag is spent before anyone asks a question, rather than
        during the demo.
        """
        self._prewarmed_at = time.time()
        self._post("/api/v2/memory/search", {"query": "prewarm", "top_k": 1})
        return {"prewarmed": True, "lag_seconds": FLUSH_TO_SEARCH_LAG_SECONDS}

    def _flush(self, user_id: str, session_id: str) -> None:
        result = self._post(
            "/api/v2/memory/flush", {"user_id": user_id, "session_id": session_id}
        )
        # Gotcha 3: no_extraction is a normal outcome for a short exchange.
        # Explicitly a no-op, not a warning, not a retry.
        if result and result.get("status") == "no_extraction":
            return

    # -- MemoryPort ---------------------------------------------------------

    def get_profile(self, user_id: str) -> ResearcherProfile:
        remote = self._post("/api/v2/memory/get", {"user_id": user_id, "kind": "profile"})
        local = self._profiles.get(user_id)
        if remote:
            distilled = str(remote.get("distilled_context") or "")[:DISTILLED_CONTEXT_LIMIT]
            if distilled:
                base = local or ResearcherProfile(user_id=user_id, specialty=None)
                return ResearcherProfile(
                    user_id=user_id,
                    specialty=remote.get("specialty") or base.specialty,
                    conditions_explored=list(base.conditions_explored),
                    query_count=base.query_count,
                    distilled_context=distilled,
                )
        return local or ResearcherProfile(user_id=user_id, specialty=None)

    def get_thread(self, user_id: str, session_id: str) -> SessionThread:
        return self._threads.get(
            (user_id, session_id), SessionThread(session_id=session_id, user_id=user_id)
        )

    def record_query(
        self,
        user_id: str,
        session_id: str,
        query: str,
        matched_conditions: Sequence[str],
    ) -> None:
        thread = self.get_thread(user_id, session_id)
        self._threads[(user_id, session_id)] = SessionThread(
            session_id=session_id,
            user_id=user_id,
            queries=[*thread.queries, query],
            pmids_shown=list(thread.pmids_shown),
        )
        profile = self.get_profile(user_id)
        explored = list(profile.conditions_explored)
        for condition in matched_conditions:
            if condition and condition not in explored:
                explored.append(condition)
        self._profiles[user_id] = ResearcherProfile(
            user_id=user_id,
            specialty=profile.specialty,
            conditions_explored=explored,
            query_count=profile.query_count + 1,
            distilled_context=profile.distilled_context or self._distil(explored, profile),
        )
        self._post(
            "/api/v2/memory/add",
            {
                "user_id": user_id,
                "session_id": session_id,
                "messages": [{"role": "user", "content": query}],
            },
        )
        self._flush(user_id, session_id)

    def record_papers_shown(
        self, user_id: str, session_id: str, pmids: Sequence[str]
    ) -> None:
        thread = self.get_thread(user_id, session_id)
        self._threads[(user_id, session_id)] = SessionThread(
            session_id=session_id,
            user_id=user_id,
            queries=list(thread.queries),
            pmids_shown=[*thread.pmids_shown, *pmids],
        )
        self._seen[user_id] |= set(pmids)

    def seen_pmids(self, user_id: str) -> set[str]:
        return set(self._seen.get(user_id, set()))

    def set_specialty(self, user_id: str, specialty: str) -> None:
        profile = self.get_profile(user_id)
        self._profiles[user_id] = ResearcherProfile(
            user_id=user_id,
            specialty=specialty,
            conditions_explored=list(profile.conditions_explored),
            query_count=profile.query_count,
            distilled_context=profile.distilled_context,
        )

    def forget(self, user_id: str) -> None:
        """Destroy everything about `user_id`.

        Gotcha 4: delete is Cloud-only and will 403 on self-hosted OSS. The
        local wipe below is unconditional, so mock mode - and any degraded real
        mode - still honours the delete. That is what makes the on-stage
        destruction beat safe.
        """
        self._post("/api/v2/memory/delete", {"user_id": user_id, "delete_all": True})
        self._profiles.pop(user_id, None)
        self._seen.pop(user_id, None)
        for key in [k for k in self._threads if k[0] == user_id]:
            self._threads.pop(key, None)

    def health(self) -> dict:
        mode = "mock" if self._mock else "live"
        detail = (
            f"everos memory ({mode}), profiles={len(self._profiles)}, "
            f"threads={len(self._threads)}, remote_errors={self._remote_errors}"
        )
        # Mock mode is deliberately ok:true - it is a supported operating mode,
        # not a degradation, and a red /health during a demo reads as broken.
        return {"ok": True, "detail": detail, "mode": mode}

    # -- helpers ------------------------------------------------------------

    @staticmethod
    def _distil(explored: list[str], profile: ResearcherProfile) -> str:
        """Cheap deterministic distillation, <= 600 chars per the contract.

        The real path is a `memory_distill` LLM call. This is what stands in
        when there is no key, and it is deliberately factual rather than
        generated so the warm prompt is reproducible between rehearsals.
        """
        if not explored:
            return ""
        specialty = f"{profile.specialty} specialist. " if profile.specialty else ""
        return (specialty + "Previously explored: " + ", ".join(explored) + ".")[
            :DISTILLED_CONTEXT_LIMIT
        ]
