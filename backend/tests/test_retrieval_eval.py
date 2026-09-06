"""C25 - tests for the retrieval evaluation.

The point of these is that the EVALUATION is trustworthy, not that the system
scores well. An eval whose split leaks, whose metrics are vacuous, or whose
baseline is sandbagged produces numbers that are worse than no numbers, so
each of those is pinned here:

  * the split is by document and provably disjoint, including for the 17
    PMIDs that appear in more than one record;
  * the ranking metrics agree with worked examples computed by hand;
  * the citation-support metric can actually fail (the shuffled-attribution
    control), so a 1.0 means something;
  * the baseline arm is the shipped scorer, not a weakened one;
  * the ordering comparison holds the candidate pool fixed.
"""
from __future__ import annotations

import math

import pytest

from backend.contracts.fakes import FakeRetrieval, _load_corpus, _tokenize
from backend.contracts.models import Paper, ResearcherProfile, ScoredPaper
from backend.measurement.run_gate import GOLD_SET
from backend.measurement.run_retrieval_eval import (
    CANDIDATE_DEPTH,
    EXPANSIONS,
    FINAL_DEPTH,
    ORDERINGS,
    Fold,
    FoldRetrieval,
    assert_disjoint,
    claim_is_supported,
    compare,
    evaluate_fold,
    expand_fakellm,
    expand_prf,
    fold_of,
    gold_for_fold,
    make_folds,
    measure_citation_support,
    mrr_at,
    ndcg_at,
    order_lexical,
    order_rarity,
    order_rarity_memory,
    precision_at,
    recall,
)


def _paper(pmid: str, *, condition: str = "C", is_rare: bool = False,
           title: str = "t", abstract: str = "a") -> Paper:
    return Paper(pmid=pmid, title=title, abstract=abstract, journal="J", year=2020,
                 condition=condition, is_rare=is_rare, url="u")


def _scored(pmids: list[str], **kwargs) -> list[ScoredPaper]:
    return [
        ScoredPaper(paper=_paper(p, **kwargs), score=float(len(pmids) - i),
                    lexical_score=0.0, semantic_score=0.0, rarity_multiplier=1.0)
        for i, p in enumerate(pmids)
    ]


# ============================================================================
# The split
# ============================================================================


def test_the_split_is_disjoint_at_document_level():
    folds = make_folds()
    stats = assert_disjoint(folds)
    assert stats["shared_documents"] == 0
    assert folds["dev"].pmids & folds["test"].pmids == set()
    assert stats["dev_documents"] + stats["test_documents"] == len(
        {p.pmid for p in _load_corpus()}
    )


def test_the_corpus_really_does_repeat_pmids_across_records():
    """If this ever stops being true the record-versus-document distinction
    stops mattering, and the split rationale should be revisited rather than
    silently kept."""
    papers = _load_corpus()
    assert len(papers) == 329
    assert len({p.pmid for p in papers}) == 312


def test_a_repeated_document_lands_wholly_on_one_side():
    """The trap: split by record and the same source document informs both
    sides. Every record of a repeated PMID must share a fold."""
    folds = make_folds()
    by_pmid: dict[str, set[str]] = {}
    for name, fold in folds.items():
        for paper in fold.papers:
            by_pmid.setdefault(paper.pmid, set()).add(name)

    repeated = [p for p, names in by_pmid.items() if len(names) > 1]
    assert repeated == [], f"{len(repeated)} document(s) straddle the split"

    counts = {}
    for paper in _load_corpus():
        counts[paper.pmid] = counts.get(paper.pmid, 0) + 1
    assert any(c > 1 for c in counts.values()), "no repeated pmid left to test"


def test_the_split_is_deterministic_and_label_sensitive():
    assert make_folds()["dev"].pmids == make_folds()["dev"].pmids
    assert fold_of("12345", label="c25-v1") == fold_of("12345", label="c25-v1")
    shuffled = make_folds(label="different-label")
    assert shuffled["dev"].pmids != make_folds()["dev"].pmids


def test_assert_disjoint_actually_catches_a_leak():
    """The guard has to be able to fail, or it proves nothing."""
    shared = _paper("999")
    leaky = {"dev": Fold("dev", [shared]), "test": Fold("test", [shared])}
    with pytest.raises(AssertionError, match="document leak"):
        assert_disjoint(leaky)


def test_a_query_with_no_relevant_document_in_a_fold_is_dropped_not_zeroed():
    """Scoring it 0 would measure the split, not the retriever."""
    fold = Fold("dev", [_paper("1", condition="Scalp angiosarcoma")])
    specs = gold_for_fold(fold)
    assert {s.condition for s in specs} == {"Scalp angiosarcoma"}
    assert len(specs) < len(GOLD_SET)


# ============================================================================
# Metric math, against hand-computed values
# ============================================================================


def test_recall_is_set_based_and_order_free():
    ranked = _scored(["a", "b", "c"])
    assert recall(ranked, {"a", "z"}) == 0.5
    assert recall(list(reversed(ranked)), {"a", "z"}) == 0.5


def test_precision_at_k():
    ranked = _scored(["a", "b", "c", "d", "e"])
    assert precision_at(ranked, {"a", "c"}, 5) == pytest.approx(0.4)
    assert precision_at(ranked, {"a", "c"}, 2) == pytest.approx(0.5)


def test_mrr_is_the_reciprocal_of_the_first_hit():
    ranked = _scored(["a", "b", "c"])
    assert mrr_at(ranked, {"b"}, 10) == pytest.approx(0.5)
    assert mrr_at(ranked, {"c"}, 10) == pytest.approx(1 / 3)
    assert mrr_at(ranked, {"zzz"}, 10) == 0.0


def test_ndcg_matches_a_hand_computed_value():
    # One relevant document at rank 2: DCG = 1/log2(3), IDCG = 1/log2(2) = 1.
    ranked = _scored(["a", "b", "c"])
    assert ndcg_at(ranked, {"b"}, 10) == pytest.approx(1 / math.log2(3))
    # Perfect ordering scores 1.0.
    assert ndcg_at(ranked, {"a", "b"}, 10) == pytest.approx(1.0)


def test_ndcg_does_not_penalise_a_query_with_few_relevant_documents():
    """Ideal DCG uses min(k, |relevant|), so a condition with 4 papers is not
    marked down for not filling 10 slots."""
    ranked = _scored(["a", "b", "c", "d", "e", "f", "g", "h", "i", "j"])
    assert ndcg_at(ranked, {"a"}, 10) == pytest.approx(1.0)


def test_metrics_are_nan_when_nothing_is_relevant():
    ranked = _scored(["a"])
    assert math.isnan(recall(ranked, set()))
    assert math.isnan(ndcg_at(ranked, set(), 10))


# ============================================================================
# Retrieval and ordering, kept apart
# ============================================================================


def test_the_baseline_scorer_is_the_shipped_one_not_a_weakened_stand_in():
    """A baseline that is worse than what ships makes any win meaningless."""
    corpus = _load_corpus()
    fold = Fold("all", corpus)
    ours = [sp.paper.pmid for sp in FoldRetrieval(fold).candidates("angiosarcoma scalp", depth=10)]
    theirs = [
        sp.paper.pmid
        for sp in FakeRetrieval().search("angiosarcoma scalp", top_k=10, apply_rarity=False)
    ]
    assert ours == theirs


def test_an_ordering_can_never_change_recall():
    """The structural reason retrieval and ordering are scored separately."""
    fold = make_folds()["test"]
    candidates = FoldRetrieval(fold).candidates("angiosarcoma scalp")
    relevant = fold.relevant("Scalp angiosarcoma")
    profile = ResearcherProfile(user_id="u", specialty=None, conditions_explored=["Scalp angiosarcoma"])

    for ranked in (
        order_lexical(candidates),
        order_rarity(candidates),
        order_rarity_memory(candidates, profile=profile, seen=set()),
    ):
        assert recall(ranked, relevant) == recall(candidates, relevant)
        assert {sp.paper.pmid for sp in ranked} == {sp.paper.pmid for sp in candidates}


def test_the_rarity_ordering_actually_moves_rare_papers_up():
    candidates = [
        ScoredPaper(paper=_paper("common", is_rare=False), score=10.0,
                    lexical_score=10.0, semantic_score=0.0, rarity_multiplier=1.0),
        ScoredPaper(paper=_paper("rare", is_rare=True), score=8.0,
                    lexical_score=8.0, semantic_score=0.0, rarity_multiplier=1.0),
    ]
    assert [sp.paper.pmid for sp in order_lexical(candidates)] == ["common", "rare"]
    assert [sp.paper.pmid for sp in order_rarity(candidates)] == ["rare", "common"]


def test_the_memory_ordering_demotes_a_seen_paper():
    """SEEN_DEMOTION is 0.6, so the demotion only reorders a pair whose scores
    are within that factor. 2.0 and 1.8 are; 2.0 and 1.0 are not, and asserting
    on the wider pair would have been asserting on a coincidence."""
    candidates = [
        ScoredPaper(paper=_paper("seen"), score=2.0, lexical_score=2.0,
                    semantic_score=0.0, rarity_multiplier=1.0),
        ScoredPaper(paper=_paper("fresh"), score=1.8, lexical_score=1.8,
                    semantic_score=0.0, rarity_multiplier=1.0),
    ]
    profile = ResearcherProfile(user_id="u", specialty=None)
    assert [sp.paper.pmid for sp in order_lexical(candidates)] == ["seen", "fresh"]
    ranked = order_rarity_memory(candidates, profile=profile, seen={"seen"})
    assert [sp.paper.pmid for sp in ranked] == ["fresh", "seen"]


def test_prf_expansion_adds_terms_without_dropping_the_query():
    fold = make_folds()["test"]
    retrieval = FoldRetrieval(fold)
    expanded = expand_prf(retrieval, "angiosarcoma scalp")
    assert expanded.startswith("angiosarcoma scalp")
    assert len(_tokenize(expanded)) > len(_tokenize("angiosarcoma scalp"))


def test_the_fakellm_expansion_is_the_same_string_for_every_query():
    """Why the fakellm arm measures a constant, not HyDE."""
    assert expand_fakellm("scalp angiosarcoma") == expand_fakellm("Parkinson's disease PET")


# ============================================================================
# Citation support
# ============================================================================


def test_claim_support_is_a_real_comparison_against_the_cited_text():
    abstract = "FDG PET showed hypermetabolic uptake in the left temporal lobe."
    assert claim_is_supported("FDG PET showed hypermetabolic uptake [1].", abstract)
    assert not claim_is_supported("The patient responded to immunotherapy [1].", abstract)


def test_an_empty_claim_is_not_counted_as_supported():
    assert not claim_is_supported("", "anything at all")


class _Result:
    def __init__(self, markdown, papers, citations):
        self.summary_markdown = markdown
        self.papers = papers
        self.citations = citations


class _Citation:
    def __init__(self, index, pmid):
        self.index, self.pmid = index, pmid


def _fixture_result():
    papers = [
        ScoredPaper(paper=_paper("1", abstract="Alpha beta gamma delta finding."),
                    score=1.0, lexical_score=0.0, semantic_score=0.0, rarity_multiplier=1.0),
        ScoredPaper(paper=_paper("2", abstract="Epsilon zeta eta theta result."),
                    score=0.9, lexical_score=0.0, semantic_score=0.0, rarity_multiplier=1.0),
    ]
    markdown = "Alpha beta gamma delta finding [1]. Epsilon zeta eta theta result [2]."
    return _Result(markdown, papers, [_Citation(1, "1"), _Citation(2, "2")])


def test_correct_attribution_scores_full_support():
    measured = measure_citation_support(_fixture_result())
    assert measured == {"claims": 2, "supported": 2, "unsupported": 0,
                        "unresolvable": 0, "uncited": 0, "support_rate": 1.0}


def test_the_shuffled_attribution_control_makes_the_metric_fail():
    """Without this, a support rate of 1.0 could mean the metric is broken.
    Rotating every marker onto the wrong paper must drop it."""
    measured = measure_citation_support(_fixture_result(), shuffle_attribution=True)
    assert measured["supported"] == 0
    assert measured["support_rate"] == 0.0


def test_a_citation_pointing_outside_the_paper_list_is_unresolvable_not_supported():
    result = _fixture_result()
    result.citations = [_Citation(99, "nope")]
    measured = measure_citation_support(result)
    assert measured["unresolvable"] == 1
    assert measured["supported"] == 0


# ============================================================================
# The report
# ============================================================================


def test_evaluate_fold_reports_every_arm_and_keeps_the_pool_fixed():
    fold = make_folds()["test"]
    report = evaluate_fold(fold)

    assert set(report["retrieval_stage"]) == set(EXPANSIONS)
    assert set(report["ordering_stage"]) == set(ORDERINGS)
    assert report["queries_scored"] > 0
    for row in report["ordering_stage"].values():
        assert row["candidate_pool"] == "expansion=none, identical across orderings"
    for row in report["retrieval_stage"].values():
        assert 0.0 <= row[f"recall_at_{CANDIDATE_DEPTH}"] <= 1.0
        assert row[f"recall_at_{FINAL_DEPTH}"] <= row[f"recall_at_{CANDIDATE_DEPTH}"]


def test_the_comparison_states_a_verdict_for_every_stage():
    folds = make_folds()
    dev, test = evaluate_fold(folds["dev"]), evaluate_fold(folds["test"])
    comparison = compare(dev, test)

    assert comparison["held_out_fold"] == "test"
    assert set(comparison["verdicts"]) == {
        "expansion_beats_raw_query",
        "rarity_rerank_beats_lexical_order",
        "rarity_rerank_beats_lexical_order_on_rare_queries",
        "memory_rerank_beats_rarity_alone",
        "fakellm_hyde_changes_anything",
    }
    assert all(isinstance(v, bool) for v in comparison["verdicts"].values())


def test_the_published_negative_result_still_holds():
    """The finding, pinned. If a change to retrieval or re-ranking makes any
    of these true, this test failing is the notification that the published
    conclusion is stale and must be rewritten rather than quietly kept."""
    folds = make_folds()
    comparison = compare(evaluate_fold(folds["dev"]), evaluate_fold(folds["test"]))
    verdicts = comparison["verdicts"]

    assert verdicts["expansion_beats_raw_query"] is False
    assert verdicts["rarity_rerank_beats_lexical_order"] is False
    assert verdicts["memory_rerank_beats_rarity_alone"] is False
    # The one place the fancy path does win, and the reason the pooled number
    # alone would have been an unfair verdict.
    assert verdicts["rarity_rerank_beats_lexical_order_on_rare_queries"] is True
