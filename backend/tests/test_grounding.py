"""Grounded retrieval: every claim traces to a retrieved record, the record
that served it is recoverable at answer time, and a claim attributed to a
record that does not carry it is caught.

The negative cases are the point of the file. A grounding check that only ever
sees honest summaries is not a check, so each defence here is exercised with
the specific attack it exists to stop:

  * a claim with no [N] at all                   -> `uncited`
  * a claim cited to a record that contradicts it -> `unsupported`
  * a claim cited to a record that never mentions it -> `unsupported`
  * a claim citing an [N] that was never offered  -> `dangling`
  * retrieval returning nothing usable            -> abstention, not an answer

Every one of these passed silently before `backend/app/verify/grounding.py`
existed; the reproductions are in `.agent-work/c31/`.
"""
from __future__ import annotations

import re

import pytest

from backend.app import pipeline
from backend.app.summary.generate import _extract_citations
from backend.app.verify.grounding import (
    ABSTENTION_MARKDOWN,
    SUPPORT_THRESHOLD,
    RecordProvenance,
    check_grounding,
    content_words,
    numbers_check_out,
    overlap_ratio,
    provenance_for,
    source_name,
    split_claims,
)
from backend.contracts.models import Paper, ScoredPaper
from backend.contracts.registry import Services, get_services

# --- fixtures ---------------------------------------------------------------

NERVE_ABSTRACT = (
    "Contrast-enhanced MRI showed nerve root enhancement in eight patients with "
    "neurolymphomatosis. FDG PET demonstrated hypermetabolic nerve segments "
    "corresponding to the enhancing roots."
)
SHOULDER_ABSTRACT = (
    "Arthroscopic rotator cuff repair produced good shoulder function at twelve "
    "months in ninety consecutive patients treated at a single centre."
)


def paper(pmid: str, abstract: str, condition: str = "Neurolymphomatosis") -> ScoredPaper:
    return ScoredPaper(
        paper=Paper(
            pmid=pmid, title=f"Title {pmid}", abstract=abstract, journal="J",
            year=2020, condition=condition, is_rare=True,
            url=f"https://pubmed.ncbi.nlm.nih.gov/{pmid}/",
        ),
        score=1.0, lexical_score=1.0, semantic_score=0.0, rarity_multiplier=1.0,
    )


@pytest.fixture
def records() -> list[ScoredPaper]:
    return [paper("111", NERVE_ABSTRACT), paper("222", SHOULDER_ABSTRACT, "Rotator cuff tear")]


# ============================================================================
# A claim with no supporting record is detectable
# ============================================================================


def test_a_claim_carrying_no_citation_is_reported_as_uncited(records):
    """The defect this module was written against.

    `_extract_citations` enumerates the markers it finds, so an unmarked
    sentence produces no citation and used to be invisible to every check in
    the repo.
    """
    markdown = (
        "Contrast-enhanced MRI showed nerve root enhancement in eight patients [1]. "
        "Rituximab is the standard first-line treatment and cures most sufferers."
    )

    report = check_grounding(markdown, records)

    assert report.total_claims == 2
    assert report.uncited == 1
    assert [c.verdict for c in report.claims] == ["grounded", "uncited"]
    assert "Rituximab" in report.ungrounded[0].text


def test_the_citation_list_alone_cannot_see_an_uncited_claim(records):
    """Names the blind spot rather than assuming it.

    If this ever starts failing because `_extract_citations` grew the ability
    to report unmarked claims, `check_grounding` is no longer load-bearing and
    somebody should be told.
    """
    markdown = (
        "Contrast-enhanced MRI showed nerve root enhancement in eight patients [1]. "
        "Rituximab is the standard first-line treatment and cures most sufferers."
    )

    citations = _extract_citations(markdown, records)

    assert len(citations) == 1, "the citation list only ever contains markers that exist"
    assert check_grounding(markdown, records).uncited == 1


def test_a_summary_that_asserts_nothing_traceable_is_not_answerable(records):
    markdown = (
        "Rituximab is the standard first-line treatment. "
        "Median survival is eleven months without therapy."
    )

    report = check_grounding(markdown, records)

    assert report.uncited == 2
    assert report.grounded == 0
    assert report.answerable is False


# ============================================================================
# A record that does not support the claim attributed to it is caught
# ============================================================================


def test_a_claim_cited_to_a_record_that_never_mentions_it_is_unsupported(records):
    """The deliberate negative case: the record was really retrieved, the
    marker really resolves, and the abstract is about something else."""
    markdown = "Contrast-enhanced MRI showed nerve root enhancement in eight patients [2]."

    report = check_grounding(markdown, records)

    assert report.claims[0].verdict == "unsupported"
    assert report.claims[0].cited_pmids == ("222",)
    assert report.claims[0].best_overlap < SUPPORT_THRESHOLD
    assert report.answerable is False


def test_a_claim_whose_number_contradicts_its_source_is_unsupported(records):
    """Word overlap stays at 1.0 here. Only the numeric rule can catch it, so
    this is the test that proves the numeric rule is doing work."""
    markdown = "Contrast-enhanced MRI showed nerve root enhancement in 47 patients [1]."

    report = check_grounding(markdown, records)

    claim = report.claims[0]
    assert claim.verdict == "unsupported"
    assert claim.numeric_conflict is True
    assert claim.best_overlap >= SUPPORT_THRESHOLD, (
        "the words all match; if this drops below the threshold the test is "
        "passing for the wrong reason and no longer isolates the numeric rule"
    )


def test_a_claim_citing_a_record_that_was_never_retrieved_is_dangling(records):
    markdown = "Rituximab produced durable remission in most treated cases [7]."

    report = check_grounding(markdown, records)

    assert report.claims[0].verdict == "dangling"
    assert report.claims[0].cited_indices == (7,)
    assert report.claims[0].cited_pmids == ()
    assert report.answerable is False


def test_a_claim_cited_to_two_records_is_grounded_if_either_carries_it(records):
    markdown = "FDG PET demonstrated hypermetabolic nerve segments [1][2]."

    report = check_grounding(markdown, records)

    assert report.claims[0].verdict == "grounded"
    assert set(report.claims[0].cited_pmids) == {"111", "222"}


# ============================================================================
# Claim splitting
# ============================================================================


def test_a_marker_after_the_terminal_period_still_cites_its_claim(records):
    """Regression for a false alarm.

    "Claim text. [1]" is ordinary citation style. Splitting on the period left
    the claim with no marker, so a verbatim quotation of the cited abstract
    came back `uncited` and forced the whole answer into abstention. Measured
    at 0.0127 accuracy on the `verbatim` class of the grounding eval before
    the fix.
    """
    inline = "FDG PET demonstrated hypermetabolic nerve segments [1]."
    trailing = "FDG PET demonstrated hypermetabolic nerve segments. [1]"

    assert check_grounding(inline, records).claims[0].verdict == "grounded"
    assert check_grounding(trailing, records).claims[0].verdict == "grounded"
    assert len(split_claims(trailing)) == 1


def test_headings_and_bullets_are_not_charged_as_uncited_claims(records):
    markdown = (
        "## Findings\n"
        "- Contrast-enhanced MRI showed nerve root enhancement in eight patients [1]\n"
        "- FDG PET demonstrated hypermetabolic nerve segments [1]\n"
    )

    report = check_grounding(markdown, records)

    assert report.total_claims == 2
    assert report.uncited == 0
    assert report.skipped_fragments == 1  # the heading


def test_stopwords_do_not_let_an_unrelated_claim_clear_the_threshold():
    """`contracts.fakes._tokenize` is a bare word set. A claim scored with it
    borrows `is`, `the`, `and`, `most` and `patients` from any abstract."""
    claim = "Rituximab is the standard first-line treatment and cures most patients"

    from backend.contracts.fakes import _tokenize

    naive = len(_tokenize(claim) & _tokenize(SHOULDER_ABSTRACT)) / len(_tokenize(claim))
    filtered = overlap_ratio(claim, SHOULDER_ABSTRACT)

    assert naive > filtered
    assert filtered < SUPPORT_THRESHOLD


# ============================================================================
# Provenance survives the pipeline
# ============================================================================


def test_retrieval_rank_comes_from_the_retrieved_order_not_the_marker(records):
    """The prompt's [N] and the retrieval rank are allowed to disagree, and
    when they do the citation must report the rank."""
    reversed_order = list(reversed(records))
    provenance = provenance_for(reversed_order, "test.Backend")
    markdown = "Contrast-enhanced MRI showed nerve root enhancement in eight patients [2]."

    citations = _extract_citations(markdown, records, provenance=provenance)

    assert citations[0].index == 2, "the prompt slot"
    assert citations[0].pmid == "222"
    assert citations[0].retrieval_rank == 1, "222 was first in the retrieved order"
    assert citations[0].source == "test.Backend"


def test_source_name_names_the_backend_not_the_retention_filter():
    from backend.app.corpus.retention import RetentionFilteredRetrieval
    from backend.contracts.fakes import FakeRetrieval

    inner = FakeRetrieval()

    assert source_name(inner) == "backend.contracts.fakes.FakeRetrieval"
    assert source_name(RetentionFilteredRetrieval(inner)) == source_name(inner)


def test_the_pipeline_carries_source_and_rank_onto_every_citation():
    result = pipeline.run_query(
        "neurolymphomatosis CNS involvement nerve enhancement MRI",
        user_id="c31", session_id="s", personalize=False,
    )

    assert result.citations, "the fake corpus answers this query"
    assert result.retrieval_provenance
    ranks = {r.pmid: r.retrieval_rank for r in result.retrieval_provenance}
    for citation in result.citations:
        assert citation.source == "backend.contracts.fakes.FakeRetrieval"
        assert citation.retrieval_rank == ranks[citation.pmid]
        assert citation.retrieval_rank >= 1


# ============================================================================
# Nothing relevant retrieved -> abstain
# ============================================================================


class _EmptyRetrieval:
    """A healthy backend with nothing to say. Healthy on purpose: an outage
    already degrades, and the interesting case is a working retriever that
    simply found no match."""

    def search(self, query, *, secondary_query=None, top_k=10, exclude_pmids=(), apply_rarity=True):
        return []

    def match_conditions(self, query, top_k=5):
        return []

    def closest_conditions(self, query, top_n=3):
        return []

    def get_by_pmids(self, pmids):
        return []

    def health(self):
        return {"ok": True, "detail": "empty"}


@pytest.fixture
def with_retrieval(monkeypatch):
    def _install(port):
        base = get_services()
        patched = Services(
            retrieval=port, llm=base.llm, memory=base.memory, ledger=base.ledger
        )
        monkeypatch.setattr(pipeline, "get_services", lambda: patched)
    return _install


def test_an_empty_retrieval_abstains_instead_of_answering(with_retrieval):
    with_retrieval(_EmptyRetrieval())

    result = pipeline.run_query(
        "neurolymphomatosis CNS involvement", user_id="c31", session_id="s", personalize=False
    )

    assert result.abstained is True
    assert result.summary_markdown == ABSTENTION_MARKDOWN
    assert result.citations == []
    assert result.grounding is not None and result.grounding.answerable is False
    assert result.grounding.records_available == 0


def test_the_abstention_text_is_a_constant_not_a_model_output(with_retrieval):
    """Asking the model that just failed to ground itself to explain the
    failure would be one more ungrounded sentence."""
    with_retrieval(_EmptyRetrieval())

    result = pipeline.run_query(
        "neurolymphomatosis CNS involvement", user_id="c31", session_id="s", personalize=False
    )

    assert not re.search(r"\[\d+\]", result.summary_markdown)
    assert result.summary_markdown == ABSTENTION_MARKDOWN


def test_a_grounded_answer_is_not_abstained():
    result = pipeline.run_query(
        "neurolymphomatosis CNS involvement nerve enhancement MRI",
        user_id="c31", session_id="s", personalize=False,
    )

    assert result.abstained is False
    assert result.grounding is not None
    assert result.grounding.grounded > 0
    assert result.grounding.uncited == 0
    assert result.grounding.dangling == 0


# ============================================================================
# Compression must not be able to decide what counts as grounded
# ============================================================================


# A four-sentence abstract built so compression at `compress_top_n=1` keeps
# the first sentence (it carries the query terms) and drops the fourth. The
# fourth is a real, checkable statement from the same source paper, so a claim
# quoting it is genuinely grounded -- unless grounding is pointed at the
# compressed copy, in which case a token-saving dial has just decided that a
# true claim is unsupported.
COMPRESSIBLE_ABSTRACT = (
    "Neurolymphomatosis with nerve enhancement was assessed by MRI in this series. "
    "Baseline demographics were recorded for the enrolled cohort. "
    "Follow-up continued until the end of the observation window. "
    "Histology confirmed diffuse large B cell infiltration of the epineurium."
)
DROPPED_SENTENCE_CLAIM = (
    "Histology confirmed diffuse large B cell infiltration of the epineurium [1]."
)


class _OneRecordRetrieval:
    def __init__(self, abstract: str) -> None:
        self._sp = paper("333", abstract)

    def search(self, query, *, secondary_query=None, top_k=10, exclude_pmids=(), apply_rarity=True):
        return [self._sp]

    def match_conditions(self, query, top_k=5):
        return []

    def closest_conditions(self, query, top_n=3):
        return []

    def get_by_pmids(self, pmids):
        return [self._sp.paper]

    def health(self):
        return {"ok": True, "detail": "one record"}


def test_grounding_reads_the_uncompressed_abstract(monkeypatch):
    """A claim quoting a sentence that compression dropped is still grounded.

    Compression exists to shrink the prompt. Letting it also shrink the text a
    claim is checked against would mean turning the token dial changes which
    answers the system is willing to serve, which is a correctness change
    wearing a cost change's clothes.
    """
    import json

    from backend.app.retrieval.policy import policy_for_label
    from backend.tests._stub_llm import StubLLM

    llm = StubLLM({
        "hyde": json.dumps({"expanded_query": "neurolymphomatosis nerve MRI"}),
        "relevance_check": json.dumps({"relevant": True, "confidence": 0.9, "note": "ok"}),
        "summary": json.dumps({"summary_markdown": DROPPED_SENTENCE_CLAIM}),
        "citation_check": json.dumps({"supported": True, "note": None}),
    })
    base = get_services()
    patched = Services(
        retrieval=_OneRecordRetrieval(COMPRESSIBLE_ABSTRACT),
        llm=llm, memory=base.memory, ledger=base.ledger,
    )
    monkeypatch.setattr(pipeline, "get_services", lambda: patched)

    result = pipeline.run_query(
        "neurolymphomatosis nerve enhancement MRI",
        user_id="c31", session_id="s", personalize=False,
        policy=policy_for_label("generous"),
    )

    prompt = next(m for site, m in llm.calls if site == "summary")
    prompt_text = " ".join(getattr(m, "content", "") for m in prompt)
    assert "Histology confirmed" not in prompt_text, (
        "the fixture is wrong: compression was supposed to drop this sentence, "
        "so the test cannot tell the two abstracts apart"
    )

    assert result.grounding is not None
    assert result.grounding.claims[0].verdict == "grounded"
    assert result.abstained is False


# ============================================================================
# The API carries it
# ============================================================================


def test_the_query_response_exposes_grounding_and_provenance():
    from fastapi.testclient import TestClient

    from backend.api.main import app

    client = TestClient(app)
    response = client.post("/query", json={
        "query": "neurolymphomatosis CNS involvement nerve enhancement MRI",
        "session_id": "s1", "user_id": "u1", "personalize": False,
    })

    assert response.status_code == 200
    body = response.json()
    assert body["abstained"] is False
    assert body["grounding"]["answerable"] is True
    assert body["grounding"]["threshold"] == SUPPORT_THRESHOLD
    assert body["retrieval_provenance"], "the record set the answer was built from"
    for record in body["retrieval_provenance"]:
        assert record["source"] == "backend.contracts.fakes.FakeRetrieval"
        assert record["retrieval_rank"] >= 1
    for citation in body["citations"]:
        assert citation["source"]
        assert citation["retrieval_rank"] >= 1


# ============================================================================
# The primitives
# ============================================================================


def test_a_citation_marker_is_not_read_as_a_quantity():
    assert numbers_check_out("Nerve enhancement was seen [3].", NERVE_ABSTRACT)
    assert "3" not in content_words("[3]")


def test_provenance_ranks_are_one_based_and_dense(records):
    provenance = provenance_for(records, "src")

    assert [r.retrieval_rank for r in provenance] == [1, 2]
    assert all(isinstance(r, RecordProvenance) for r in provenance)
