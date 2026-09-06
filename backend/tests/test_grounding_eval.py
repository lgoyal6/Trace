"""Guards the grounding evaluation.

An eval is a claim about a system's quality, so it needs its own tests more
than most code does: a labelled set that is secretly all-positive, a split
that leaks, or a metric that cannot fail would all report excellent numbers
about nothing.

Also covers the `uncited` counter added to `run_retrieval_eval`, which is the
measurement half of the same defect `test_grounding.py` covers in the product:
a summary that is half unsourced assertion used to report a support rate of
1.0 because the metric iterated the citation list, and the citation list is
built by enumerating the markers that exist.
"""
from __future__ import annotations

from backend.app.verify.grounding import SUPPORT_THRESHOLD
from backend.measurement.run_grounding_eval import (
    PARAPHRASE_RATES,
    THRESHOLD_SWEEP,
    Case,
    build_cases,
    confusion,
    numeric_flip,
    paraphrase,
    predict,
)
from backend.measurement.run_retrieval_eval import make_folds, measure_citation_support
from backend.app.summary.generate import SourcedCitation
from backend.contracts.models import Paper, ScoredPaper


# ============================================================================
# The labelled set
# ============================================================================


def _cases(fold_name: str = "test"):
    folds = make_folds()
    return build_cases(folds[fold_name]), {p.pmid: p for p in folds[fold_name].papers}


def test_the_labelled_set_contains_both_labels_and_every_class():
    cases, _ = _cases()

    labels = {c.label for c in cases}
    classes = {c.case_class for c in cases}

    assert labels == {True, False}, "a set with one label cannot measure precision"
    assert {"verbatim", "marker_after", "misattributed", "off_topic",
            "same_condition", "numeric_flip", "uncited"} <= classes
    assert {f"paraphrase_{r}" for r in PARAPHRASE_RATES} <= classes


def test_the_negatives_are_a_real_share_of_the_set():
    cases, _ = _cases()

    negatives = sum(1 for c in cases if not c.label)

    assert negatives / len(cases) > 0.25, (
        "a set that is nearly all positive would report high precision by "
        "having almost nothing to get wrong"
    )


def test_case_construction_is_deterministic():
    folds = make_folds()
    first = build_cases(folds["dev"])
    second = build_cases(folds["dev"])

    assert [(c.case_class, c.claim, c.cited_pmid) for c in first] == \
           [(c.case_class, c.claim, c.cited_pmid) for c in second]


def test_the_dev_and_test_case_sets_share_no_document():
    dev_cases, _ = _cases("dev")
    test_cases, _ = _cases("test")

    assert not ({c.cited_pmid for c in dev_cases} & {c.cited_pmid for c in test_cases})


def test_paraphrase_substitutes_rather_than_deletes():
    """Deleting words shrinks the denominator of the overlap ratio and leaves
    the score unchanged, which would make the whole paraphrase class inert."""
    sentence = "Contrast enhanced imaging revealed marked enhancement along the affected nerve roots"

    mutated = paraphrase(sentence, 30, seed="s")

    assert len(mutated.split()) == len(sentence.split())
    assert mutated != sentence


def test_a_higher_paraphrase_rate_changes_more_words():
    sentence = "Contrast enhanced imaging revealed marked enhancement along the affected nerve roots"

    light = paraphrase(sentence, 10, seed="s").split()
    heavy = paraphrase(sentence, 40, seed="s").split()
    original = sentence.split()

    changed_light = sum(1 for a, b in zip(original, light) if a != b)
    changed_heavy = sum(1 for a, b in zip(original, heavy) if a != b)

    assert changed_heavy > changed_light


def test_numeric_flip_changes_a_number_and_nothing_else():
    sentence = "Enhancement was seen in eight of 12 patients"

    flipped = numeric_flip(sentence, seed="s")

    assert flipped is not None
    assert flipped != sentence
    assert len(flipped.split()) == len(sentence.split())
    assert "19" in flipped


def test_numeric_flip_returns_none_when_there_is_no_number():
    assert numeric_flip("Enhancement was seen along the nerve roots", seed="s") is None


# ============================================================================
# The metric can fail
# ============================================================================


def test_the_metric_rejects_every_deliberate_negative_class():
    """The controls this eval exists for. If any of these classes started
    scoring as grounded, the number the eval reports would be meaningless.
    """
    cases, index = _cases()
    negatives = [c for c in cases if not c.label]

    grounded = [c for c in negatives if predict(c, index, SUPPORT_THRESHOLD) == "grounded"]

    assert len(grounded) / len(negatives) < 0.02, (
        f"{len(grounded)}/{len(negatives)} negatives scored as grounded"
    )


def test_a_threshold_of_zero_destroys_precision():
    """Proof the threshold is load-bearing: drop it and the metric stops being
    able to say no."""
    cases, index = _cases("dev")

    strict = confusion(cases, index, SUPPORT_THRESHOLD)
    permissive = confusion(cases, index, 0.0)

    assert permissive["false_positive"] > strict["false_positive"]
    assert permissive["precision"] < strict["precision"]


def test_the_sweep_covers_the_shipped_threshold():
    assert SUPPORT_THRESHOLD in THRESHOLD_SWEEP, (
        "a shipped value outside the sweep was never compared against anything"
    )


def test_confusion_counts_add_up():
    cases, index = _cases("dev")

    m = confusion(cases, index, SUPPORT_THRESHOLD)

    assert m["true_positive"] + m["false_positive"] + m["true_negative"] + m["false_negative"] == m["n"]
    assert m["n"] + m["skipped"] == len(cases)


def test_predict_runs_the_shipped_entry_point():
    """A metric that bypassed the claim splitter and the marker parser would
    not be measuring what ships. The marker-after-period class only passes if
    the real `split_claims` is in the path."""
    cases, index = _cases()
    marker_after = [c for c in cases if c.case_class == "marker_after"]

    verdicts = {predict(c, index, SUPPORT_THRESHOLD) for c in marker_after}

    assert verdicts == {"grounded"}


# ============================================================================
# The uncited counter in run_retrieval_eval
# ============================================================================


def _result_with(markdown: str):
    def sp(pmid, abstract):
        return ScoredPaper(
            paper=Paper(pmid=pmid, title="t", abstract=abstract, journal="j", year=2020,
                        condition="c", is_rare=False, url="u"),
            score=1.0, lexical_score=1.0, semantic_score=0.0, rarity_multiplier=1.0,
        )

    class R:
        summary_markdown = markdown
        papers = [sp("111", "MRI showed nerve root enhancement in eight patients."),
                  sp("222", "FDG PET detected hypermetabolic nerve segments.")]
        citations = [SourcedCitation(index=1, pmid="111"), SourcedCitation(index=2, pmid="222")]

    return R()


def test_unsourced_assertions_are_counted_against_the_support_rate():
    """Two of these four sentences come from nowhere, and the metric used to
    report claims=2, support_rate=1.0."""
    result = _result_with(
        "MRI showed nerve root enhancement in eight patients [1]. "
        "FDG PET detected hypermetabolic nerve segments [2]. "
        "Rituximab is the standard first-line treatment and cures most sufferers. "
        "Median survival is eleven months without therapy."
    )

    measured = measure_citation_support(result)

    assert measured["claims"] == 4
    assert measured["uncited"] == 2
    assert measured["support_rate"] == 0.5


def test_a_fully_cited_summary_still_scores_full_support():
    result = _result_with(
        "MRI showed nerve root enhancement in eight patients [1]. "
        "FDG PET detected hypermetabolic nerve segments [2]."
    )

    measured = measure_citation_support(result)

    assert measured["uncited"] == 0
    assert measured["support_rate"] == 1.0
