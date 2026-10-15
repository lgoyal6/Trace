"""Check retained retrieval wrappers and condition aggregates after deletion.
Uses retention.py with a small inner port and an isolated on-disk tombstone log.
Explicit fixture logs stay caller-owned; default logs must refresh on each read.
"""
from backend.app.corpus.retention import DELETE, REDACT, TOMBSTONE_PATH_ENV, TombstoneLog, enforced, invalidate_caches

class Inner:
    def get_by_pmids(self, pmids):
        from types import SimpleNamespace
        return [SimpleNamespace(pmid=p) for p in pmids]
    def closest_conditions(self, query, top_n=3):
        return ["aggregate includes deleted source"]


def test_existing_wrapper_observes_a_later_deletion(tmp_path, monkeypatch):
    path = tmp_path / "tombstones.json"
    monkeypatch.setenv(TOMBSTONE_PATH_ENV, str(path))
    invalidate_caches()
    try:
        log = TombstoneLog()
        log.add("first", DELETE, "initial")
        log.save(path)
        port = enforced(Inner())
        assert port.get_by_pmids(["first"]) == []
        assert len(port.get_by_pmids(["later"])) == 1
        log.add("later", DELETE, "later request")
        log.save(path)
        assert port.get_by_pmids(["later"]) == []
    finally:
        invalidate_caches()


def test_deleted_sources_fail_closed_on_unfilterable_aggregates():
    log = TombstoneLog()
    log.add("victim", DELETE, "request")
    assert enforced(Inner(), log).closest_conditions("query") == []


def test_redaction_preserves_countable_condition_aggregates():
    log = TombstoneLog()
    log.add("victim", REDACT, "redaction")
    assert enforced(Inner(), log).closest_conditions("query") == Inner().closest_conditions("query")
