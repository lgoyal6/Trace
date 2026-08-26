"""Scarcity multiplier - what a rare-condition memory is worth over a common one.

A fact about Alzheimer's is cheap: 40 papers in the corpus say it, and any of
them will re-derive it. A fact about Neurolymphomatosis is expensive: 4 papers
exist, so the context that carries it is nearly irreplaceable. Price should
follow that, and it should follow it from the data rather than from a constant
someone picked.

--- THE FORMULA -------------------------------------------------------------

Inverse document frequency, normalised so the most common condition sits at
exactly 1.0 (no boost) and everything scarcer rises above it:

    multiplier(c) = ln(N / n_c) / ln(N / n_max)

    N      total papers in the corpus
    n_c    papers for condition c
    n_max  papers for the most-covered condition

n_max in the denominator is what makes 1.0 the floor rather than an arbitrary
clamp: for the most common condition the numerator and denominator are the same
expression, so the ratio is 1.0 by construction, not by rounding.

Measured against backend/data/corpus.json on 2026-08-07 (N=329, 14 conditions,
n_max=40):

    1.000  Acute ischemic stroke MCA territory      n=40
    1.000  Alzheimer's disease probable typical     n=40
    1.284  Progressive supranuclear palsy           n=22
    1.533  Primary progressive aphasia semantic     n=13
    1.900  Scalp angiosarcoma                       n=6
    1.987  Primary angiitis of CNS                  n=5
    2.093  Neurolymphomatosis CNS involvement       n=4   <- the demo shock target

So the observed range is 1.000-2.093. Those bounds are a *property of this
corpus*, not a hard-coded clamp - swap the corpus and the top end moves. The
only clamp applied is a floor at 1.0, which guards the degenerate case of a
condition appearing more often than n_max (impossible by definition, but a
malformed corpus should not produce a discount).
"""
from __future__ import annotations

import functools
import json
import math
import os
from collections import Counter
from pathlib import Path

#: Applied when the corpus cannot be read or the condition is unknown. A
#: missing condition must never make a memory *cheaper* than a common one, and
#: it must never take the demo down.
NEUTRAL_MULTIPLIER = 1.0


def _corpus_path() -> Path:
    """Locate corpus.json.

    Resolves relative to this file's grandparent (``backend/``) so it works
    both inside the merged repo and from the staging tree. ``MEMEX_CORPUS_PATH``
    overrides for tests and for the seed script.
    """
    override = os.environ.get("MEMEX_CORPUS_PATH")
    if override:
        return Path(override)
    return Path(__file__).resolve().parent.parent / "data" / "corpus.json"


@functools.lru_cache(maxsize=4)
def _condition_counts(path_str: str) -> tuple[dict[str, int], int, int]:
    """(counts per condition, N, n_max). Cached - the corpus does not change."""
    with open(path_str) as fh:
        rows = json.load(fh)
    counts = Counter(row["condition"] for row in rows if row.get("condition"))
    total = len(rows)
    n_max = max(counts.values()) if counts else 1
    return dict(counts), total, n_max


class ScarcityIndex:
    """IDF scarcity multipliers over a paper corpus.

    Construct once per process; every lookup after that is a dict hit.
    """

    def __init__(self, corpus_path: Path | str | None = None) -> None:
        self._path = Path(corpus_path) if corpus_path else _corpus_path()
        try:
            counts, total, n_max = _condition_counts(str(self._path))
        except (OSError, ValueError, KeyError):
            # A corpus we cannot read means every condition prices neutrally.
            # Degrading is correct here: a broken data file must not be able to
            # invent a 2x price.
            counts, total, n_max = {}, 0, 1
        self._counts = counts
        self._total = total
        self._n_max = n_max
        self._denominator = (
            math.log(total / n_max) if total > 0 and n_max > 0 and total > n_max else 0.0
        )

    # -- lookups ------------------------------------------------------------

    @property
    def total_papers(self) -> int:
        """N."""
        return self._total

    @property
    def max_condition_papers(self) -> int:
        """n_max."""
        return self._n_max

    def paper_count(self, condition: str) -> int:
        """n_c. Zero when the condition is not in the corpus."""
        return self._counts.get(condition, 0)

    def multiplier(self, condition: str) -> float:
        """Scarcity multiplier for `condition`, floored at 1.0."""
        n_c = self._counts.get(condition, 0)
        if n_c <= 0 or self._denominator <= 0:
            return NEUTRAL_MULTIPLIER
        raw = math.log(self._total / n_c) / self._denominator
        return max(NEUTRAL_MULTIPLIER, raw)

    def multiplier_for_papers(self, conditions: list[str]) -> float:
        """Scarcity for a *set* of retrieved papers.

        Takes the maximum rather than the mean: a bundle containing one
        4-paper condition is as hard to replace as that condition, and
        averaging it against common neighbours would price away exactly the
        thing being sold.
        """
        if not conditions:
            return NEUTRAL_MULTIPLIER
        return max(self.multiplier(c) for c in conditions)

    def table(self) -> list[dict]:
        """Every condition with its count and multiplier, rarest first.

        This is what the dashboard renders, and what a judge reads to check
        the claim that the price comes from the corpus.
        """
        rows = [
            {
                "condition": condition,
                "paper_count": count,
                "multiplier": self.multiplier(condition),
            }
            for condition, count in self._counts.items()
        ]
        rows.sort(key=lambda r: (-r["multiplier"], r["condition"]))
        return rows

    def health(self) -> dict:
        ok = self._total > 0
        return {
            "ok": ok,
            "detail": (
                f"scarcity index: N={self._total}, conditions={len(self._counts)}, "
                f"n_max={self._n_max}"
                if ok
                else f"corpus unreadable at {self._path}; all multipliers neutral"
            ),
        }


@functools.lru_cache(maxsize=1)
def get_index() -> ScarcityIndex:
    """Process-wide singleton."""
    return ScarcityIndex()


def multiplier(condition: str) -> float:
    """Convenience wrapper over the singleton index."""
    return get_index().multiplier(condition)
