"""C22 - provenance, retention, deletion.

Two halves, in this order:

  PROVENANCE  can a stored record name the fetched document version that
              produced it, and can an unchanged document skip reprocessing?
  DELETION    does a deleted or redacted record stop appearing in the store,
              in the derived index, in the caches, and in the exports?

Every test here is credential-free. `_RecordingSession` captures the SQL a
Snowflake load would issue without connecting to anything, and the retrieval
tests run against `FakeRetrieval` over a temporary corpus so nothing under
`backend/data/` is written.

Backup retention is deliberately NOT tested as "the record is gone". It is
tested as "the receipt says the record is still in backups and does not
pretend otherwise", because that is the true statement. See
`test_receipt_never_claims_a_backup_was_purged`.
"""
from __future__ import annotations

import hashlib
import json
from pathlib import Path
from unittest.mock import patch

import pytest

from backend.app.corpus import build_corpus as build_corpus_mod
from backend.app.corpus.build_corpus import build_corpus, load_to_snowflake
from backend.app.corpus.conditions import Condition
from backend.app.corpus.fetch_pubmed import fetch_abstracts_with_provenance
from backend.app.corpus.provenance import (
    DERIVED_FIELDS,
    PARSER_VERSION,
    Manifest,
    SourceDocument,
    content_hash,
    describe_source,
    record_hash,
)
from backend.app.corpus.retention import (
    BACKUP_STORES,
    DELETE,
    REDACT,
    REDACTION_PLACEHOLDER,
    TOMBSTONE_PATH_ENV,
    RetentionFilteredRetrieval,
    TombstoneLog,
    active_log,
    delete_record,
    enforced,
    invalidate_caches,
    purge_corpus_file,
    snowflake_purge_statements,
)
from backend.contracts.fakes import FakeRetrieval

FIXTURE_XML = b"<PubmedArticleSet><PubmedArticle>fixture</PubmedArticle></PubmedArticleSet>"

FAKE_CONDITION = Condition(
    name="Test condition", rarity="rare", pubmed_query="test query",
    region_literature="Test region", atlas_label="Test Atlas Label",
    target_count=2,
)

PAPERS = [
    {"pmid": "1", "title": "T1", "abstract": "A1"},
    {"pmid": "2", "title": "T2", "abstract": "A2"},
]


@pytest.fixture(autouse=True)
def _isolated_tombstone_log(tmp_path, monkeypatch):
    """Every test gets its own empty log, and no test can leave one behind."""
    monkeypatch.setenv(TOMBSTONE_PATH_ENV, str(tmp_path / "tombstones.json"))
    invalidate_caches()
    yield
    invalidate_caches()


# ============================================================================
# PROVENANCE
# ============================================================================


def test_content_hash_identifies_the_document_version():
    assert content_hash(FIXTURE_XML) == hashlib.sha256(FIXTURE_XML).hexdigest()
    assert content_hash(FIXTURE_XML) != content_hash(FIXTURE_XML + b" ")


def test_record_hash_is_insensitive_to_key_order_and_extra_local_fields():
    a = {"pmid": "1", "title": "T", "abstract": "A", "condition": "X"}
    b = {"abstract": "A", "pmid": "1", "condition": "Y", "title": "T"}
    # `condition` is local curation, not document content, so re-tagging a
    # paper must not read as the source document having changed.
    assert record_hash(a) == record_hash(b)
    assert "condition" not in DERIVED_FIELDS


def test_record_hash_changes_when_the_document_text_changes():
    base = {"pmid": "1", "title": "T", "abstract": "A"}
    assert record_hash(base) != record_hash({**base, "abstract": "A."})


def test_fetch_reports_the_document_version_it_parsed():
    with patch("backend.app.corpus.fetch_pubmed.fetch", return_value=FIXTURE_XML):
        papers, source = fetch_abstracts_with_provenance(["1"])
    assert isinstance(source, SourceDocument)
    assert source.content_sha256 == hashlib.sha256(FIXTURE_XML).hexdigest()
    assert source.byte_length == len(FIXTURE_XML)
    assert source.parser_version == PARSER_VERSION
    assert "id=1" in source.url


def test_fetch_with_no_ids_reports_no_document():
    papers, source = fetch_abstracts_with_provenance([])
    assert (papers, source) == ([], None)


def _build_once(tmp_path, *, raw: bytes, parser_version: str = PARSER_VERSION):
    source = describe_source("https://example.invalid/efetch?id=1,2", raw,
                             parser_version=parser_version)
    with patch("backend.app.corpus.build_corpus.search_pmids", return_value=["1", "2"]), \
         patch("backend.app.corpus.build_corpus.fetch_abstracts_with_provenance",
               return_value=(PAPERS, source)), \
         patch("backend.app.corpus.build_corpus.time.sleep"):
        return build_corpus([FAKE_CONDITION], tmp_path / "corpus.json")


def test_every_built_record_names_the_document_that_produced_it(tmp_path):
    records = _build_once(tmp_path, raw=FIXTURE_XML)
    manifest = Manifest.load(tmp_path / "corpus_manifest.json")

    audit = manifest.audit(records)
    assert audit["records"] == 2
    assert audit["traced"] == 2
    assert audit["untraced"] == 0

    prov = manifest.provenance_for("1")
    assert prov is not None
    assert prov.source_sha256 == hashlib.sha256(FIXTURE_XML).hexdigest()
    assert prov.source_url == "https://example.invalid/efetch?id=1,2"
    assert prov.parser_version == PARSER_VERSION
    assert prov.fetched_at_iso
    # And the binding is checkable in the other direction: the stored record
    # still hashes to what the manifest says was derived.
    assert prov.record_sha256 == record_hash(records[0])


def test_a_stored_record_edited_after_derivation_shows_as_drifted(tmp_path):
    records = _build_once(tmp_path, raw=FIXTURE_XML)
    manifest = Manifest.load(tmp_path / "corpus_manifest.json")
    tampered = [{**records[0], "abstract": "silently rewritten"}, records[1]]

    audit = manifest.audit(tampered)
    assert audit["drifted"] == 1
    assert audit["drifted_pmids"] == ["1"]


def test_the_existing_corpus_has_no_provenance_and_the_audit_says_so():
    """The honest baseline, pinned. backend/data/corpus.json predates the
    manifest, so an empty manifest must report every record as untraced rather
    than quietly reporting a clean audit."""
    corpus = json.loads(
        (Path(__file__).resolve().parent.parent / "data" / "corpus.json").read_text()
    )
    audit = Manifest().audit(corpus)
    assert audit["records"] == len(corpus) == 329
    assert audit["traced"] == 0
    assert audit["untraced"] == 329


def _derivation_spy(monkeypatch):
    calls: list[str] = []
    original = Manifest.record_derived

    def spy(self, record, doc):
        calls.append(str(record["pmid"]))
        return original(self, record, doc)

    monkeypatch.setattr(Manifest, "record_derived", spy)
    return calls


def test_unchanged_document_and_same_parser_skips_reprocessing(tmp_path, monkeypatch):
    calls = _derivation_spy(monkeypatch)

    _build_once(tmp_path, raw=FIXTURE_XML)
    assert calls == ["1", "2"], "first run must derive both records"

    calls.clear()
    _build_once(tmp_path, raw=FIXTURE_XML)
    assert calls == [], "same bytes, same parser: nothing to re-derive"


def test_changed_document_forces_reprocessing(tmp_path, monkeypatch):
    calls = _derivation_spy(monkeypatch)
    _build_once(tmp_path, raw=FIXTURE_XML)
    calls.clear()

    _build_once(tmp_path, raw=FIXTURE_XML + b"<!-- edited -->")
    assert calls == ["1", "2"]


def test_a_parser_version_bump_forces_reprocessing_of_unchanged_bytes(tmp_path, monkeypatch):
    """A content hash on its own is not enough. Identical bytes read by a
    different parser produce a different record, so the skip has to be
    conditional on both."""
    calls = _derivation_spy(monkeypatch)
    _build_once(tmp_path, raw=FIXTURE_XML)
    calls.clear()

    _build_once(tmp_path, raw=FIXTURE_XML, parser_version="pubmed-efetch/2")
    assert calls == ["1", "2"]


def test_needs_reprocess_names_the_reason():
    manifest = Manifest()
    doc = describe_source("u", FIXTURE_XML)
    assert manifest.needs_reprocess(doc) == (True, "no prior fetch recorded for this url")

    manifest.record_fetch(doc)
    assert manifest.needs_reprocess(doc) == (False, "unchanged document, same parser")

    changed = describe_source("u", FIXTURE_XML + b"x")
    assert manifest.needs_reprocess(changed) == (True, "document content changed")

    rebuilt = describe_source("u", FIXTURE_XML, parser_version="pubmed-efetch/2")
    should, reason = manifest.needs_reprocess(rebuilt)
    assert should is True and "parser version changed" in reason


def test_parser_version_is_pinned():
    """Pinned so changing what `_parse_articles` extracts without bumping the
    version is at least a visible diff in this file."""
    assert PARSER_VERSION == "pubmed-efetch/1"


# ============================================================================
# DELETION AND REDACTION
# ============================================================================


@pytest.fixture
def corpus_copy(tmp_path):
    real = Path(__file__).resolve().parent.parent / "data" / "corpus.json"
    dest = tmp_path / "corpus.json"
    dest.write_text(real.read_text())
    return dest


def _first_pmid(path: Path) -> str:
    return json.loads(path.read_text())[0]["pmid"]


def test_delete_removes_the_record_from_the_store_and_the_export(corpus_copy):
    victim = _first_pmid(corpus_copy)
    before = len(json.loads(corpus_copy.read_text()))

    log = TombstoneLog()
    log.add(victim, DELETE, "subject request")
    result = purge_corpus_file(corpus_copy, log)

    after = json.loads(corpus_copy.read_text())
    assert result["before"] == before
    assert victim not in {r["pmid"] for r in after}
    # corpus.json is both the store and the shipped export, so one purge
    # covers both; the text is gone from the file, not just hidden.
    assert victim not in corpus_copy.read_text()


def test_redact_keeps_the_row_but_removes_the_document_text(corpus_copy):
    victim = _first_pmid(corpus_copy)
    log = TombstoneLog()
    log.add(victim, REDACT, "rights holder complaint")
    purge_corpus_file(corpus_copy, log)

    after = {r["pmid"]: r for r in json.loads(corpus_copy.read_text())}
    assert victim in after, "a redaction must not silently change a condition's paper count"
    assert after[victim]["title"] == REDACTION_PLACEHOLDER
    assert after[victim]["abstract"] == REDACTION_PLACEHOLDER
    assert after[victim]["redacted"] is True
    # Local curation survives: it is not document content.
    assert after[victim]["condition"]


def test_delete_beats_redact_whatever_order_they_arrive_in():
    for order in ((REDACT, DELETE), (DELETE, REDACT)):
        log = TombstoneLog()
        for mode in order:
            log.add("42", mode, "reason")
        assert log.mode_for("42") == DELETE
        assert log.deleted_pmids() == {"42"}
        assert log.redacted_pmids() == set()


def test_tombstone_rejects_an_unknown_mode():
    with pytest.raises(ValueError):
        TombstoneLog().add("42", "archive", "reason")


def test_tombstone_log_round_trips_through_disk(tmp_path):
    path = tmp_path / "t.json"
    log = TombstoneLog()
    log.add("1", DELETE, "a")
    log.add("2", REDACT, "b")
    log.save(path)

    reloaded = TombstoneLog.load(path)
    assert reloaded.deleted_pmids() == {"1"}
    assert reloaded.redacted_pmids() == {"2"}
    assert [e.reason for e in reloaded.entries] == ["a", "b"]


# -- the index / read path ---------------------------------------------------


def test_a_deleted_record_is_never_served_by_retrieval():
    inner = FakeRetrieval()
    victim = inner.search("angiosarcoma scalp", top_k=5)[0].paper.pmid

    log = TombstoneLog()
    log.add(victim, DELETE, "subject request")
    filtered = RetentionFilteredRetrieval(inner, log)

    assert victim in {sp.paper.pmid for sp in inner.search("angiosarcoma scalp", top_k=5)}
    assert victim not in {sp.paper.pmid for sp in filtered.search("angiosarcoma scalp", top_k=5)}


def test_filtering_a_deleted_record_does_not_shrink_the_result_set():
    """Dropping after the fact would silently hand back top_k - 1 papers."""
    inner = FakeRetrieval()
    victim = inner.search("angiosarcoma scalp", top_k=5)[0].paper.pmid
    log = TombstoneLog()
    log.add(victim, DELETE, "subject request")

    filtered = RetentionFilteredRetrieval(inner, log)
    assert len(filtered.search("angiosarcoma scalp", top_k=5)) == 5


def test_a_deleted_record_is_never_served_by_get_by_pmids():
    inner = FakeRetrieval()
    victim = inner.search("angiosarcoma scalp", top_k=1)[0].paper.pmid
    log = TombstoneLog()
    log.add(victim, DELETE, "subject request")

    assert len(inner.get_by_pmids([victim])) == 1
    assert RetentionFilteredRetrieval(inner, log).get_by_pmids([victim]) == []


def test_a_redacted_record_is_served_without_its_text():
    inner = FakeRetrieval()
    victim = inner.search("angiosarcoma scalp", top_k=1)[0].paper.pmid
    log = TombstoneLog()
    log.add(victim, REDACT, "rights holder complaint")

    (paper,) = RetentionFilteredRetrieval(inner, log).get_by_pmids([victim])
    assert paper.abstract == REDACTION_PLACEHOLDER
    assert paper.title == REDACTION_PLACEHOLDER


def test_an_empty_log_leaves_the_port_untouched():
    """The default path must cost nothing: no wrapper, same object."""
    inner = FakeRetrieval()
    assert enforced(inner) is inner


# -- caches ------------------------------------------------------------------


def test_a_deletion_takes_effect_without_a_process_restart(tmp_path, monkeypatch):
    """The tombstone log is read through a cache; a deletion written while the
    process is running must still be seen.

    Three reads, deliberately: an absent log, a log with one entry that is
    then cached, and the same file after a second entry is appended. Only the
    third exercises cache invalidation - the first two would pass even with a
    cache that never expires, which is exactly the bug this guards."""
    path = tmp_path / "tombstones.json"
    monkeypatch.setenv(TOMBSTONE_PATH_ENV, str(path))

    assert len(active_log()) == 0

    log = TombstoneLog()
    log.add("42", DELETE, "first request")
    log.save(path)
    assert active_log().deleted_pmids() == {"42"}  # this read populates the cache

    log.add("43", DELETE, "second request, same file")
    log.save(path)
    assert active_log().deleted_pmids() == {"42", "43"}


def test_purging_the_corpus_evicts_the_in_process_copy(tmp_path, monkeypatch):
    """The measured pre-existing bug: FakeRetrieval reads corpus.json through
    an lru_cache, so removing a record from disk left it being served."""
    from backend.contracts import fakes

    corpus = tmp_path / "corpus.json"
    corpus.write_text((Path(fakes._CORPUS_PATH)).read_text())
    monkeypatch.setattr(fakes, "_CORPUS_PATH", corpus)
    fakes._load_corpus.cache_clear()

    victim = _first_pmid(corpus)
    assert len(FakeRetrieval().get_by_pmids([victim])) == 1  # warms the cache

    receipt = delete_record(
        victim, "subject request", corpus_path=corpus,
        tombstone_path=tmp_path / "tombstones.json",
        manifest_path=tmp_path / "corpus_manifest.json",
    )

    assert FakeRetrieval().get_by_pmids([victim]) == []
    assert any("lru_cache" in o.detail for o in receipt.outcomes)
    fakes._load_corpus.cache_clear()


# -- the resurrection case ---------------------------------------------------


class _RecordingSession:
    def __init__(self):
        self.calls: list[tuple[str, list]] = []

    def sql(self, text, params=None):
        self.calls.append((text, list(params or [])))
        return self

    def collect(self):
        return []


def test_a_reload_does_not_resurrect_a_deleted_record(tmp_path, monkeypatch):
    """The classic deletion bug. `load_to_snowflake` truncates and reloads, so
    a load from any corpus.json that predates the deletion (a git checkout, a
    restored backup, a re-fetch) would put the record straight back into
    PAPERS and from there into the search index. The tombstone log outlives
    all three, so it is the authority, not the file."""
    real = Path(__file__).resolve().parent.parent / "data" / "corpus.json"
    stale = tmp_path / "corpus.json"
    stale.write_text(real.read_text())
    victim = _first_pmid(stale)

    tombstones = tmp_path / "tombstones.json"
    log = TombstoneLog()
    log.add(victim, DELETE, "subject request")
    log.save(tombstones)
    monkeypatch.setenv(TOMBSTONE_PATH_ENV, str(tombstones))

    session = _RecordingSession()
    monkeypatch.setattr(build_corpus_mod, "load_to_snowflake", load_to_snowflake)
    with patch("backend.snowflake.session.snowflake_available", return_value=True), \
         patch("backend.snowflake.session.get_session", return_value=session):
        summary = load_to_snowflake(stale)

    loaded_params = [p for text, params in session.calls
                     if "INSERT INTO NEULIT.CORE.PAPERS" in text for p in params]
    assert victim not in loaded_params, "deleted record was reloaded into PAPERS"
    assert summary["papers"] == len(json.loads(stale.read_text())) - 1


def test_purge_statements_bind_pmids_and_refresh_the_index():
    log = TombstoneLog()
    log.add("111", DELETE, "subject request")
    log.add("222", REDACT, "rights holder complaint")

    statements = snowflake_purge_statements(log)
    joined = " ".join(sql for sql, _ in statements)

    assert "DELETE FROM NEULIT.CORE.PAPERS" in joined
    assert "UPDATE NEULIT.CORE.PAPERS" in joined
    # The index is a materialisation of PAPERS with TARGET_LAG='1 hour';
    # purging the base table is not purging the index.
    assert "ALTER CORTEX SEARCH SERVICE NEULIT.CORE.PAPERS_SEARCH REFRESH" in joined
    # A pmid arrives off the network and is never interpolated.
    assert "111" not in joined and "222" not in joined
    assert "111" in [p for _, params in statements for p in params]


def test_no_purge_statements_when_nothing_is_tombstoned():
    assert snowflake_purge_statements(TombstoneLog()) == []


def test_execute_snowflake_purge_runs_and_verifies_bound_deletion():
    from backend.app.corpus.retention import execute_snowflake_purge

    class Session:
        def __init__(self): self.calls = []
        def sql(self, sql, params=None):
            self.calls.append((sql, params or []))
            rows = [(0,)] if sql.startswith("SELECT COUNT") else []
            return type("Result", (), {"collect": lambda self: rows})()

    log = TombstoneLog()
    log.add("111", DELETE, "subject request")
    session = Session()
    receipt = execute_snowflake_purge(log, session=session)
    assert receipt["remaining_deleted_rows"] == 0
    assert receipt["index_refresh_requested"] is True
    assert any("ALTER CORTEX SEARCH SERVICE" in sql for sql, _ in session.calls)
    assert all("111" not in sql for sql, _ in session.calls)


# -- backups, stated separately ---------------------------------------------


def test_receipt_never_claims_a_backup_was_purged(tmp_path):
    corpus = tmp_path / "corpus.json"
    corpus.write_text(json.dumps([{"pmid": "1", "title": "T", "abstract": "A",
                                   "condition": "C", "rarity": "rare"}]))
    receipt = delete_record(
        "1", "subject request", corpus_path=corpus,
        tombstone_path=tmp_path / "t.json", manifest_path=tmp_path / "m.json",
    )

    assert {o.status for o in receipt.outcomes} <= {"purged", "pending", "read-path-filtered"}
    assert receipt.backups, "the receipt must enumerate what still holds the record"
    summary = receipt.summary()
    assert "backups (NOT purged by this operation)" in summary
    for backup in receipt.backups:
        assert backup.purgeable_by_this_code is False
        assert backup.name in summary


def test_fail_safe_is_declared_as_not_operator_controllable():
    """Snowflake Fail-safe is a fixed 7 day window that an account cannot
    shorten, query, or purge. Claiming a deletion reaches it would be false."""
    failsafe = next(b for b in BACKUP_STORES if "Fail-safe" in b.name)
    assert failsafe.operator_controllable is False
    assert failsafe.purgeable_by_this_code is False
    assert "7 days" in failsafe.retention


def test_receipt_marks_the_snowflake_purge_as_pending_not_done(tmp_path):
    """No Snowflake account is reachable from here. The receipt has to say
    'statements generated, not executed', not 'purged'."""
    corpus = tmp_path / "corpus.json"
    corpus.write_text(json.dumps([{"pmid": "1", "title": "T", "abstract": "A"}]))
    receipt = delete_record(
        "1", "subject request", corpus_path=corpus,
        tombstone_path=tmp_path / "t.json", manifest_path=tmp_path / "m.json",
    )
    snowflake = next(o for o in receipt.outcomes if "PAPERS_SEARCH" in o.store)
    assert snowflake.status == "pending"
    assert "not executed" in snowflake.detail


def test_deleting_a_record_removes_its_provenance_row(tmp_path):
    _build_once(tmp_path, raw=FIXTURE_XML)
    manifest_path = tmp_path / "corpus_manifest.json"
    assert Manifest.load(manifest_path).provenance_for("1") is not None

    delete_record(
        "1", "subject request", corpus_path=tmp_path / "corpus.json",
        tombstone_path=tmp_path / "t.json", manifest_path=manifest_path,
    )
    assert Manifest.load(manifest_path).provenance_for("1") is None
    assert Manifest.load(manifest_path).provenance_for("2") is not None


def test_dry_run_changes_nothing_on_disk(tmp_path):
    corpus = tmp_path / "corpus.json"
    corpus.write_text(json.dumps([{"pmid": "1", "title": "T", "abstract": "A"}]))
    tombstones = tmp_path / "t.json"

    receipt = delete_record(
        "1", "subject request", corpus_path=corpus, tombstone_path=tombstones,
        manifest_path=tmp_path / "m.json", dry_run=True,
    )
    assert json.loads(corpus.read_text())[0]["pmid"] == "1"
    assert not tombstones.exists()
    assert receipt.pmid == "1"


# -- end to end --------------------------------------------------------------


def test_a_deleted_paper_never_reaches_a_summary_or_a_citation(tmp_path, monkeypatch):
    from backend.app.pipeline import run_query

    baseline = run_query("angiosarcoma scalp FDG PET", "u1", "s1", personalize=False)
    assert baseline.papers, "fixture query must retrieve something to delete"
    victim = baseline.papers[0].paper.pmid

    tombstones = tmp_path / "tombstones.json"
    log = TombstoneLog()
    log.add(victim, DELETE, "subject request")
    log.save(tombstones)
    monkeypatch.setenv(TOMBSTONE_PATH_ENV, str(tombstones))
    invalidate_caches()

    after = run_query("angiosarcoma scalp FDG PET", "u2", "s2", personalize=False)
    assert victim not in {sp.paper.pmid for sp in after.papers}
    assert victim not in {c.pmid for c in after.citations}
    assert victim not in after.summary_markdown
    assert all(victim not in entry.retrieved_pmids for entry in after.trace)


def test_a_deleted_paper_is_not_served_by_the_demo_route(tmp_path, monkeypatch):
    from fastapi.testclient import TestClient

    from backend.api.main import app

    client = TestClient(app)
    before = client.get("/demo-contrast").json()
    victim = before["weighted"][0]["pmid"]

    tombstones = tmp_path / "tombstones.json"
    log = TombstoneLog()
    log.add(victim, DELETE, "subject request")
    log.save(tombstones)
    monkeypatch.setenv(TOMBSTONE_PATH_ENV, str(tombstones))
    invalidate_caches()

    after = client.get("/demo-contrast").json()
    served = {p["pmid"] for p in after["weighted"]} | {p["pmid"] for p in after["naive"]}
    assert victim not in served
