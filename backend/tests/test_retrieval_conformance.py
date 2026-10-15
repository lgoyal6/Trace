"""RetrievalPort conformance, run against every adapter that can run offline.

The frozen contract (backend/contracts/ports.py) is the capability seam that
makes the retrieval backend swappable. This suite pins what any adapter must
do, plus the product guarantees Trace layers on top of it (retention and
researcher-access filtering, content-hash provenance), and runs the same
assertions against each adapter:

    fake           FakeRetrieval, in-process token overlap      offline
    local_vector   LocalVectorRetriever, SQLite + hashed vectors offline
    cortex_replay  CortexSearchRetriever with `_raw_search` replaying corpus
                   rows in the Cortex Search response shape. This exercises
                   the adapter's own fusion, exclusion, rarity and truncation
                   code; it is NOT a Snowpark/SDK or live-service run.

Capabilities an adapter cannot provide offline are skipped with the reason
stated, never passed: Cortex `get_by_pmids` and `closest_conditions` issue SQL
through a live Snowpark session.
"""
from __future__ import annotations

import json
from pathlib import Path

import pytest

from backend.app.corpus.access import AccessFilteredRetrieval, AccessPolicy, AccessScope
from backend.app.corpus.provenance import record_hash
from backend.app.corpus.retention import TombstoneLog, enforced
from backend.app.verify.grounding import provenance_for
from backend.contracts.fakes import FakeRetrieval, _tokenize
from backend.contracts.models import Paper, ScoredPaper
from backend.snowflake import retrieval as cortex_module
from backend.snowflake.retrieval import CortexSearchRetriever
from backend.vectorstore import LocalVectorRetriever

CORPUS = json.loads((Path(__file__).resolve().parents[1] / "data" / "corpus.json").read_text())
QUERY = "progressive supranuclear palsy midbrain hypometabolism"
NEEDS_SESSION = "requires a live Snowpark session (Snowflake credentials); BLOCKED offline"


def _cortex_replay(monkeypatch):
    """Cortex adapter fed corpus rows in the Search service's response shape."""
    unique = {}
    for row in CORPUS:
        unique.setdefault(str(row["pmid"]), row)

    def raw_search(self, query, limit):
        q = _tokenize(query)
        scored = []
        for pmid, row in unique.items():
            overlap = len(q & (_tokenize(row["title"]) | _tokenize(row["abstract"])))
            if overlap:
                scored.append((overlap, pmid, row))
        scored.sort(key=lambda t: (t[0], t[1]), reverse=True)
        return [{
            "PMID": pmid, "TITLE": row["title"], "ABSTRACT": row["abstract"], "JOURNAL": "",
            "PUB_YEAR": 0, "CONDITION": row["condition"], "IS_RARE": row.get("rarity") == "rare",
            "URL": f"https://pubmed.ncbi.nlm.nih.gov/{pmid}/",
            "@scores": {"cosine_similarity": overlap / 10.0, "text_match": float(overlap)},
        } for overlap, pmid, row in scored[:limit]]

    monkeypatch.setattr(cortex_module, "snowflake_available", lambda: True)
    monkeypatch.setattr(CortexSearchRetriever, "_raw_search", raw_search)
    return CortexSearchRetriever()


@pytest.fixture(params=["fake", "local_vector", "cortex_replay"])
def adapter(request, tmp_path, monkeypatch):
    name = request.param
    if name == "fake":
        port = FakeRetrieval()
    elif name == "local_vector":
        port = LocalVectorRetriever(db_path=tmp_path / "vectors.sqlite3")
    else:
        port = _cortex_replay(monkeypatch)
    return name, port


def _ids(results):
    return [sp.paper.pmid for sp in results]


def test_search_shape_bound_and_order(adapter):
    _, port = adapter
    results = port.search(QUERY, top_k=7)
    assert 0 < len(results) <= 7
    assert all(isinstance(sp, ScoredPaper) and isinstance(sp.paper, Paper) for sp in results)
    scores = [sp.score for sp in results]
    assert scores == sorted(scores, reverse=True)
    assert port.search(QUERY, top_k=0) == []


def test_one_result_slot_per_document(adapter, request):
    name, port = adapter
    if name == "fake":
        # Found by this suite: FakeRetrieval scores corpus RECORDS, and 17 PMIDs
        # appear twice in corpus.json under different conditions, so one paper
        # can fill two top_k slots. fakes.py is frozen at contracts-v1, so the
        # divergence is recorded as a strict xfail rather than patched.
        request.applymarker(pytest.mark.xfail(strict=True, reason="FakeRetrieval is record-level, not document-level"))
    results = port.search(QUERY, top_k=10)
    assert len(set(_ids(results))) == len(results)


def test_search_is_deterministic(adapter):
    _, port = adapter
    assert _ids(port.search(QUERY, top_k=10)) == _ids(port.search(QUERY, top_k=10))


def test_exclusion_is_applied_before_truncation(adapter):
    _, port = adapter
    first = _ids(port.search(QUERY, top_k=5))
    after = _ids(port.search(QUERY, top_k=5, exclude_pmids=first[:2]))
    assert not set(first[:2]) & set(after)
    assert len(after) == 5


def test_rarity_can_be_disabled(adapter):
    _, port = adapter
    assert all(sp.rarity_multiplier == 1.0 for sp in port.search(QUERY, top_k=10, apply_rarity=False))


def test_health_shape(adapter):
    name, port = adapter
    if name == "cortex_replay":
        pytest.skip(NEEDS_SESSION)
    health = port.health()
    assert isinstance(health["ok"], bool) and health["ok"] is True
    assert isinstance(health["detail"], str) and health["detail"]


def test_lookup_returns_requested_known_records_only(adapter):
    name, port = adapter
    if name == "cortex_replay":
        pytest.skip(NEEDS_SESSION)
    wanted = _ids(port.search(QUERY, top_k=3))
    got = port.get_by_pmids([*wanted, "pmid-that-does-not-exist"])
    # Order is not part of the contract: FakeRetrieval returns corpus order,
    # LocalVectorRetriever request order. Membership is.
    assert {p.pmid for p in got} == set(wanted)
    assert port.get_by_pmids([]) == []


def test_closest_conditions_shape(adapter):
    name, port = adapter
    if name == "cortex_replay":
        pytest.skip(NEEDS_SESSION)
    matches = port.closest_conditions(QUERY, top_n=2)
    assert len(matches) == 2 and matches[0].similarity >= matches[1].similarity


# --- product guarantees, identical for every adapter --------------------------


def test_retention_wrapper_never_serves_a_tombstoned_record(adapter):
    name, port = adapter
    first = _ids(port.search(QUERY, top_k=5))
    log = TombstoneLog()
    log.add(first[0], "delete", "conformance")
    wrapped = enforced(port, log)
    served = _ids(wrapped.search(QUERY, top_k=5))
    assert first[0] not in served and len(served) == 5
    if name != "cortex_replay":
        assert wrapped.get_by_pmids([first[0]]) == []


def test_access_wrapper_denies_outsiders_and_serves_members(adapter):
    name, port = adapter
    first = _ids(port.search(QUERY, top_k=5))
    policy = AccessPolicy(labels={first[0]: "trial-team"}, known_labels=frozenset({"trial-team"}))
    outsider = AccessFilteredRetrieval(port, policy, AccessScope("u-out"))
    member = AccessFilteredRetrieval(port, policy, AccessScope("u-in", frozenset({"trial-team"})))
    assert first[0] not in _ids(outsider.search(QUERY, top_k=5))
    assert len(outsider.search(QUERY, top_k=5)) == 5
    assert first[0] in _ids(member.search(QUERY, top_k=5))  # valid control
    if name != "cortex_replay":
        assert outsider.get_by_pmids([first[0]]) == []
        assert [p.pmid for p in member.get_by_pmids([first[0]])] == [first[0]]


def test_provenance_hash_names_the_served_text(adapter):
    _, port = adapter
    results = port.search(QUERY, top_k=3)
    for prov, sp in zip(provenance_for(results, "x"), results):
        assert prov.content_hash == record_hash(
            {"pmid": sp.paper.pmid, "title": sp.paper.title, "abstract": sp.paper.abstract})
