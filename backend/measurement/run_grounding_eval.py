"""C31 - how good is the grounding check itself?

Run with:
    python -m backend.measurement.run_grounding_eval

Zero credentials, no network, no model. Writes
backend/measurement/results/grounding_eval.json.

--- WHAT THIS MEASURES, AND WHAT IT DOES NOT -------------------------------

`backend/app/verify/grounding.py` decides whether a claim is carried by the
record it cites. That decision has a threshold in it, and a threshold nobody
measured is a threshold somebody guessed. So this file builds a labelled set
out of the real corpus and reports precision, recall and F1 for the
`grounded` verdict, plus the confusion matrix, plus the sweep that chose the
threshold.

It measures the CHECK. It does not measure whether a language model writes
grounded summaries: that would need a model, and under the `fake` profile the
summariser is a local extractor whose output is verbatim corpus text, so it
would score a perfect 1.0 and mean nothing.

--- THE SPLIT -------------------------------------------------------------

Reuses `run_retrieval_eval.make_folds`, which splits by PMID (not by record)
under the label `c25-v1`. The threshold is swept on `dev` and reported on
`test`, and no document appears on both sides. Sweeping and reporting on one
set would be choosing the number that flatters the number.

--- THE LABELLED SET ------------------------------------------------------

Every case is generated deterministically from real abstracts. Six classes,
four of them negative, because the negatives are the whole point: a check that
never says no is not a check.

  verbatim        POSITIVE  a real sentence from the abstract it cites, with
                            the marker inside the sentence ("text [1].").
  marker_after    POSITIVE  the same sentence with the marker after the
                            terminal period ("text. [1]"), the citation style
                            a model writes unless told otherwise. It is its
                            own class because the splitter used to hand it
                            back as `uncited`, which is a false alarm that
                            forces abstention on a correctly cited answer.
  paraphrase_k    POSITIVE  the same sentence with k% of its content words
                            replaced by out-of-vocabulary tokens. This is the
                            class that actually tests the threshold: a real
                            summariser rewords, and a check that only accepts
                            verbatim quotation would reject every honest
                            paraphrase. k runs 10/20/30/40.
  misattributed   NEGATIVE  a real sentence from paper A, cited to paper B.
                            The deliberate negative case: the record exists,
                            it was really retrieved, and it does not say this.
  same_condition  NEGATIVE  the hard version of the same thing. The sentence
                            comes from a DIFFERENT paper about the SAME
                            condition, so it shares the disease name, the
                            imaging modality and most of the vocabulary with
                            the record it is cited to. Precision measured only
                            against lexically distant negatives would be a
                            number about the test set.
  off_topic       NEGATIVE  a sentence from a DIFFERENT CONDITION's paper,
                            cited to this one. The record simply does not
                            mention the subject of the claim.
  numeric_flip    NEGATIVE  a real sentence from the cited abstract with one
                            number changed. Word overlap stays near 1.0, so
                            no threshold on a fraction can catch it; the
                            numeric rule is what has to.
  uncited         NEGATIVE  a real assertion carrying no [N] at all.

`paraphrase_40` is labelled positive on purpose and is expected to be the
class the check loses. It is kept in the scored set rather than quietly
dropped, because reporting recall over only the easy positives would be
reporting a number about the test set rather than about the check.
"""
from __future__ import annotations

import hashlib
import json
import re
import statistics
import time
from dataclasses import dataclass
from pathlib import Path

from backend.app.verify.grounding import (
    SUPPORT_THRESHOLD,
    check_grounding,
    content_words,
)
from backend.contracts.models import Paper, ScoredPaper
from backend.measurement.run_retrieval_eval import Fold, assert_disjoint, make_folds

RESULTS_DIR = Path(__file__).resolve().parent / "results"
RESULTS_PATH = RESULTS_DIR / "grounding_eval.json"

#: Thresholds swept on the dev fold. The value the product ships is whichever
#: of these maximises dev F1; `SUPPORT_THRESHOLD` is expected to equal it and
#: the run says so explicitly if it does not.
THRESHOLD_SWEEP = (0.4, 0.45, 0.5, 0.55, 0.6, 0.65, 0.7, 0.75, 0.8, 0.85, 0.9)

PARAPHRASE_RATES = (10, 20, 30, 40)

#: A claim needs enough content words for a percentage substitution to mean
#: anything; below this the rates round to the same mutation.
MIN_SENTENCE_CONTENT_WORDS = 8

_SENT_SPLIT_RE = re.compile(r"(?<=[.!?])\s+")
_NUMBER_RE = re.compile(r"\b\d+(?:\.\d+)?\b")


# ============================================================================
# Deterministic case construction
# ============================================================================


def _rank(seed: str) -> int:
    return int(hashlib.sha256(seed.encode()).hexdigest(), 16)


def sentences_of(paper: Paper) -> list[str]:
    return [
        s.strip()
        for s in _SENT_SPLIT_RE.split(paper.abstract.strip())
        if len(content_words(s)) >= MIN_SENTENCE_CONTENT_WORDS
    ]


def paraphrase(sentence: str, rate_pct: int, *, seed: str) -> str:
    """Replace `rate_pct` of the sentence's content words with tokens that are
    not in the corpus, keeping word order and every number.

    Substitution rather than deletion: deleting words shrinks the denominator
    of the overlap ratio and would leave the score unchanged, which would make
    the whole paraphrase class inert. Numbers are preserved so this class
    tests the threshold and only the threshold, with the numeric rule held
    constant.
    """
    tokens = sentence.split()
    swappable = [
        i for i, t in enumerate(tokens)
        if content_words(t) and not _NUMBER_RE.search(t)
    ]
    if not swappable:
        return sentence
    n = max(1, round(len(swappable) * rate_pct / 100))
    chosen = sorted(swappable, key=lambda i: _rank(f"{seed}:{i}"))[:n]
    out = list(tokens)
    for j, i in enumerate(chosen):
        out[i] = f"zzq{j}xw"
    return " ".join(out)


def numeric_flip(sentence: str, *, seed: str) -> str | None:
    """Change one number. Returns None when the sentence states none."""
    numbers = _NUMBER_RE.findall(sentence)
    if not numbers:
        return None
    target = numbers[_rank(seed) % len(numbers)]
    try:
        replacement = str(int(target) + 7)
    except ValueError:
        replacement = str(round(float(target) + 7, 2))
    return re.sub(rf"\b{re.escape(target)}\b", replacement, sentence, count=1)


@dataclass(frozen=True)
class Case:
    case_class: str
    label: bool          # True == genuinely grounded in the cited record
    claim: str           # includes its [N] marker, or none for `uncited`
    cited_pmid: str
    record_pmids: tuple[str, ...]


def build_cases(fold: Fold, *, limit: int | None = None) -> list[Case]:
    """One case of every class per usable document on this fold.

    A "usable" document has at least one sentence long enough to mutate. The
    partner document a misattribution points at is chosen by a stable hash so
    a rerun compares like with like, and the off-topic partner is drawn from a
    different `condition` so the two negatives stay distinct failures rather
    than both being "some other paper".
    """
    papers = sorted(
        (p for p in fold.papers if sentences_of(p)), key=lambda p: (p.pmid, p.condition)
    )
    if not papers:
        return []
    if limit is not None:
        papers = papers[:limit]

    by_condition: dict[str, list[Paper]] = {}
    for p in papers:
        by_condition.setdefault(p.condition, []).append(p)
    conditions = sorted(by_condition)

    cases: list[Case] = []
    for idx, paper in enumerate(papers):
        sentence = sentences_of(paper)[0]
        partner = papers[(idx + 1 + _rank(paper.pmid) % max(1, len(papers) - 1)) % len(papers)]
        if partner.pmid == paper.pmid:
            partner = papers[(idx + 1) % len(papers)]

        other_conditions = [c for c in conditions if c != paper.condition]
        off_topic_paper = None
        if other_conditions:
            pick = other_conditions[_rank(f"ot:{paper.pmid}") % len(other_conditions)]
            off_topic_paper = by_condition[pick][0]

        pair = (paper.pmid, partner.pmid)

        stem = sentence.rstrip().rstrip(".!?")
        cases.append(Case("verbatim", True, f"{stem} [1].", paper.pmid, pair))
        cases.append(Case("marker_after", True, f"{stem}. [1]", paper.pmid, pair))
        for rate in PARAPHRASE_RATES:
            mutated = paraphrase(stem, rate, seed=f"{paper.pmid}:{rate}")
            cases.append(Case(f"paraphrase_{rate}", True, f"{mutated} [1].", paper.pmid, pair))

        partner_sentence = sentences_of(partner)[0].rstrip().rstrip(".!?")
        cases.append(Case("misattributed", False, f"{partner_sentence} [1].", paper.pmid, pair))

        same_condition_pool = [
            p for p in by_condition[paper.condition] if p.pmid != paper.pmid
        ]
        if same_condition_pool:
            sibling = same_condition_pool[_rank(f"sc:{paper.pmid}") % len(same_condition_pool)]
            sibling_sentence = sentences_of(sibling)[0].rstrip().rstrip(".!?")
            if content_words(sibling_sentence) != content_words(sentence):
                cases.append(Case(
                    "same_condition", False, f"{sibling_sentence} [1].", paper.pmid, pair
                ))

        if off_topic_paper is not None:
            ot_sentence = sentences_of(off_topic_paper)[0].rstrip().rstrip(".!?")
            cases.append(Case("off_topic", False, f"{ot_sentence} [1].", paper.pmid, pair))

        flipped = numeric_flip(stem, seed=paper.pmid)
        if flipped is not None:
            cases.append(Case("numeric_flip", False, f"{flipped} [1].", paper.pmid, pair))

        cases.append(Case("uncited", False, sentence, paper.pmid, pair))

    return cases


# ============================================================================
# Scoring
# ============================================================================


def _scored(paper: Paper, rank: int) -> ScoredPaper:
    return ScoredPaper(
        paper=paper, score=1.0 / rank, lexical_score=1.0 / rank,
        semantic_score=0.0, rarity_multiplier=1.0,
    )


def predict(case: Case, fold_index: dict[str, Paper], threshold: float) -> str:
    """The product's own verdict for one case, through `check_grounding`.

    Runs the real entry point rather than `claim_is_grounded` directly, so the
    claim splitter, the marker parser and the scaffolding filter are all in the
    measured path. A metric that bypasses them would not be measuring what
    ships.
    """
    papers = [_scored(fold_index[case.cited_pmid], 1)]
    report = check_grounding(case.claim, papers, threshold=threshold)
    if not report.claims:
        return "skipped"
    return report.claims[0].verdict


def confusion(cases: list[Case], fold_index: dict[str, Paper], threshold: float) -> dict:
    tp = fp = tn = fn = skipped = 0
    per_class: dict[str, dict[str, int]] = {}
    for case in cases:
        verdict = predict(case, fold_index, threshold)
        bucket = per_class.setdefault(
            case.case_class, {"n": 0, "correct": 0, "grounded": 0}
        )
        bucket["n"] += 1
        if verdict == "skipped":
            skipped += 1
            continue
        predicted_grounded = verdict == "grounded"
        bucket["grounded"] += int(predicted_grounded)
        bucket["correct"] += int(predicted_grounded == case.label)
        if case.label and predicted_grounded:
            tp += 1
        elif case.label and not predicted_grounded:
            fn += 1
        elif not case.label and predicted_grounded:
            fp += 1
        else:
            tn += 1

    precision = tp / (tp + fp) if (tp + fp) else 0.0
    recall = tp / (tp + fn) if (tp + fn) else 0.0
    f1 = 2 * precision * recall / (precision + recall) if (precision + recall) else 0.0
    total = tp + fp + tn + fn
    return {
        "threshold": threshold,
        "n": total,
        "skipped": skipped,
        "true_positive": tp,
        "false_positive": fp,
        "true_negative": tn,
        "false_negative": fn,
        "precision": round(precision, 4),
        "recall": round(recall, 4),
        "f1": round(f1, 4),
        "accuracy": round((tp + tn) / total, 4) if total else 0.0,
        "per_class": {
            name: {
                "n": b["n"],
                "predicted_grounded": b["grounded"],
                "accuracy": round(b["correct"] / b["n"], 4) if b["n"] else 0.0,
            }
            for name, b in sorted(per_class.items())
        },
    }


def main() -> dict:
    started = time.time()
    folds = make_folds()
    split = assert_disjoint(folds)

    report: dict = {"split": split, "sweep_thresholds": list(THRESHOLD_SWEEP)}

    dev_cases = build_cases(folds["dev"])
    dev_index = {p.pmid: p for p in folds["dev"].papers}
    sweep = [confusion(dev_cases, dev_index, t) for t in THRESHOLD_SWEEP]
    best = max(sweep, key=lambda r: (r["f1"], r["precision"]))
    report["dev"] = {"cases": len(dev_cases), "sweep": sweep, "best_by_f1": best}

    test_cases = build_cases(folds["test"])
    test_index = {p.pmid: p for p in folds["test"].papers}
    report["test"] = {
        "cases": len(test_cases),
        "at_shipped_threshold": confusion(test_cases, test_index, SUPPORT_THRESHOLD),
        "at_dev_best_threshold": confusion(test_cases, test_index, best["threshold"]),
    }
    report["shipped_threshold"] = SUPPORT_THRESHOLD
    report["shipped_threshold_is_dev_best"] = (
        abs(SUPPORT_THRESHOLD - best["threshold"]) < 1e-9
    )

    # Latency of the check itself, on the held-out cases, so the cost of
    # running it on every answer is a number rather than an assumption.
    timings = []
    for case in test_cases[:400]:
        t0 = time.perf_counter()
        predict(case, test_index, SUPPORT_THRESHOLD)
        timings.append((time.perf_counter() - t0) * 1000)
    report["check_latency_ms"] = {
        "n": len(timings),
        "mean": round(statistics.fmean(timings), 4) if timings else None,
        "p95": round(sorted(timings)[int(len(timings) * 0.95) - 1], 4) if timings else None,
    }
    report["wall_seconds"] = round(time.time() - started, 2)

    RESULTS_DIR.mkdir(parents=True, exist_ok=True)
    RESULTS_PATH.write_text(json.dumps(report, indent=2, sort_keys=True) + "\n")
    return report


if __name__ == "__main__":
    r = main()
    print(json.dumps({
        "split": r["split"],
        "dev_cases": r["dev"]["cases"],
        "dev_best": {k: r["dev"]["best_by_f1"][k] for k in ("threshold", "precision", "recall", "f1")},
        "shipped_threshold": r["shipped_threshold"],
        "shipped_is_dev_best": r["shipped_threshold_is_dev_best"],
        "test": {k: r["test"]["at_shipped_threshold"][k]
                 for k in ("n", "precision", "recall", "f1", "accuracy",
                           "true_positive", "false_positive", "true_negative", "false_negative")},
        "test_per_class": r["test"]["at_shipped_threshold"]["per_class"],
        "check_latency_ms": r["check_latency_ms"],
    }, indent=2))
    print(f"\nwrote {RESULTS_PATH}")
