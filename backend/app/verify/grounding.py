"""Deterministic claim-level grounding: does every assertion in the summary
trace to a retrieved record, and does that record actually say it?

--- WHY THIS EXISTS ALONGSIDE citation_check.py -----------------------------

`citation_check.py` asks an LLM whether each cited abstract supports its
claim. Two things it structurally cannot do:

1. It only ever sees the claims that already carry a `[N]` marker.
   `summary.generate._extract_citations` builds that list by enumerating the
   markers it FINDS, so a sentence with no marker produces no citation, is
   never sent to the judge, and is counted by nothing. A summary that is half
   unsourced assertion therefore reports a perfect support rate. That is the
   defect this module was written against; `test_grounding.py` reproduces it.

2. Under the `fake` profile the judge and the summariser are the same stand-in,
   so its verdict is the summariser marking its own homework. Under the `live`
   profile it is a paid call that can time out, and `check_citations` returns
   `supported=None` on any failure. Neither gives a check you can rely on.

So this module is lexical, local, free, and always runs. It does not replace
the judge; the judge is the semantic opinion and this is the arithmetic that
holds when the judge is unavailable or self-interested.

--- WHAT "GROUNDED" MEANS HERE ---------------------------------------------

A claim is grounded in a cited record when enough of its CONTENT words occur
in that record's abstract, and every number it states occurs there too.

The stopword filter is the difference between a check and a rubber stamp.
`contracts.fakes._tokenize` is a bare word set, so "Rituximab is the standard
first-line treatment and cures most patients" overlaps an abstract about
shoulder surgery on `is`, `the`, `and`, `most` and `patients` alone. Content
words only, and the overlap has to clear a threshold that was MEASURED on a
labelled set rather than picked (see `backend/measurement/run_grounding_eval.py`
and the `dev`/`test` numbers it writes).

The numeric rule is separate and absolute because containment is blind to the
substitution that matters most in a clinical claim: swapping "eight patients"
for "eighteen patients" changes one token out of a dozen, which no threshold
on a fraction will ever catch, and it changes the meaning of the sentence
completely. So any number in the claim that is absent from the abstract fails
it outright, regardless of how well the words line up.

--- THE FOUR VERDICTS ------------------------------------------------------

    grounded     cites at least one record whose abstract carries the claim
    unsupported  cites a record, and that record does not carry the claim
    uncited      states something and cites nothing
    dangling     cites an [N] that is not in the retrieved set at all

`uncited` and `dangling` are the two that nothing in the repo could see
before. They are separate because they are different failures: `uncited` is a
model asserting from its own weights, `dangling` is a model inventing a source
slot that was never offered to it.
"""
from __future__ import annotations

import re
from dataclasses import dataclass
from typing import Literal, Sequence

from backend.contracts.models import ScoredPaper

Verdict = Literal["grounded", "unsupported", "uncited", "dangling"]

#: Fraction of a claim's content words that must appear in the cited abstract.
#: Measured, not chosen: `backend/measurement/run_grounding_eval.py` sweeps
#: eleven values on the `dev` fold and this is the one that maximises F1
#: there (dev F1 0.9945). Reported on the held-out `test` fold, which the
#: sweep never saw: precision 0.9989, recall 0.9968, F1 0.9979 over 1624
#: cases, 1 false positive and 3 false negatives. Rerun that script after
#: changing this; it says outright whether the shipped value is still the
#: dev-best one.
SUPPORT_THRESHOLD = 0.55

#: A fragment with fewer content words than this is prose scaffolding (a
#: heading, a list bullet, "In summary:") rather than an assertion, and is
#: reported as `skipped` instead of being charged as an uncited claim. Kept
#: low on purpose: three content words is already enough to assert something.
MIN_CLAIM_CONTENT_WORDS = 3

_WORD_RE = re.compile(r"[a-z0-9]+")
_MARKER_RE = re.compile(r"\[(\d+)\]")
_SENTENCE_SPLIT_RE = re.compile(r"(?<=[.!?])\s+")
_NUMBER_RE = re.compile(r"\b\d+(?:\.\d+)?\b")

#: Function words plus the handful of biomedical-boilerplate nouns that occur
#: in almost every abstract ("patients", "study", "results"). Without them a
#: claim about one disease scores overlap against an abstract about another
#: purely on connective tissue.
_STOPWORDS = frozenset("""
a about after all also an and any are as at be because been before being between both but by
can could did do does doing done during each either few for from further had has have having he
her here hers him his how i if in into is it its itself just may me might more most must my no
nor not of off on once only or other our ours out over own per same she should so some such than
that the their theirs them then there these they this those through to too under until up upon
us very was we were what when where which while who whom why will with within would you your
associated cases clinical cohort compared conclusion conclusions data demonstrated findings
following group groups included including initial method methods objective observed outcome
outcomes patient patients performed present presented purpose report reported reports research
result results review showed shown shows significant significantly studied studies study
subjects underwent using
""".split())


def content_words(text: str) -> set[str]:
    """Lowercase alphanumeric tokens with stopwords and citation markers gone."""
    stripped = _MARKER_RE.sub(" ", text)
    return {w for w in _WORD_RE.findall(stripped.lower()) if w not in _STOPWORDS}


def numbers_in(text: str) -> set[str]:
    """Bare numeric literals in the text, markers excluded.

    `[3]` is a source slot, not a quantity, so it must not be read as one.
    """
    return set(_NUMBER_RE.findall(_MARKER_RE.sub(" ", text)))


def overlap_ratio(claim: str, abstract: str) -> float:
    """Fraction of the claim's content words that occur in the abstract.

    0.0 for a claim with no content words at all, so an empty or
    scaffolding-only fragment can never be scored as grounded by accident.
    """
    claim_words = content_words(claim)
    if not claim_words:
        return 0.0
    return len(claim_words & content_words(abstract)) / len(claim_words)


def numbers_check_out(claim: str, abstract: str) -> bool:
    """Every number the claim states also appears in the abstract."""
    return numbers_in(claim) <= numbers_in(abstract)


def claim_is_grounded(claim: str, abstract: str, *, threshold: float = SUPPORT_THRESHOLD) -> bool:
    return numbers_check_out(claim, abstract) and overlap_ratio(claim, abstract) >= threshold


@dataclass(frozen=True)
class RecordProvenance:
    """Where one retrieved record came from, kept per record rather than per
    request so a citation can name its own rank.

    `retrieval_rank` is the 1-based position in the ordering the answer was
    actually built from (post re-rank, post retention filter), NOT the `[N]`
    marker. They coincide today and are still stored apart, because `[N]` is a
    position in a prompt string: it is assigned when the abstracts are
    numbered for the summary call and it is whatever that call site decided.
    A rank that is only recoverable by trusting a prompt's numbering is not
    provenance.
    """

    pmid: str
    source: str
    retrieval_rank: int
    score: float


@dataclass(frozen=True)
class ClaimVerdict:
    position: int              # 1-based position of the claim in the summary
    text: str
    cited_indices: tuple[int, ...]
    cited_pmids: tuple[str, ...]
    verdict: Verdict
    best_overlap: float
    numeric_conflict: bool


@dataclass(frozen=True)
class GroundingReport:
    claims: tuple[ClaimVerdict, ...]
    skipped_fragments: int
    records_available: int
    threshold: float

    @property
    def grounded(self) -> int:
        return sum(1 for c in self.claims if c.verdict == "grounded")

    @property
    def unsupported(self) -> int:
        return sum(1 for c in self.claims if c.verdict == "unsupported")

    @property
    def uncited(self) -> int:
        return sum(1 for c in self.claims if c.verdict == "uncited")

    @property
    def dangling(self) -> int:
        return sum(1 for c in self.claims if c.verdict == "dangling")

    @property
    def total_claims(self) -> int:
        return len(self.claims)

    @property
    def grounded_rate(self) -> float:
        return round(self.grounded / self.total_claims, 4) if self.claims else 0.0

    @property
    def ungrounded(self) -> tuple[ClaimVerdict, ...]:
        """Every claim that does not trace to a record that supports it."""
        return tuple(c for c in self.claims if c.verdict != "grounded")

    @property
    def answerable(self) -> bool:
        """False means the answer must not be served as written.

        Three ways to fail, and all three are the same failure wearing
        different clothes -- an answer that is not coming from the corpus:

          * nothing was retrieved, so there is nothing it could be coming from;
          * something was retrieved but the text asserts nothing traceable;
          * not one claim traces to a record that supports it, which is what
            answering from the model's own weights looks like from outside.
        """
        if self.records_available == 0:
            return False
        if not self.claims:
            return False
        return self.grounded > 0

    def as_dict(self) -> dict:
        return {
            "total_claims": self.total_claims,
            "grounded": self.grounded,
            "unsupported": self.unsupported,
            "uncited": self.uncited,
            "dangling": self.dangling,
            "skipped_fragments": self.skipped_fragments,
            "records_available": self.records_available,
            "grounded_rate": self.grounded_rate,
            "answerable": self.answerable,
            "threshold": self.threshold,
        }


#: A fragment that is nothing but citation markers, e.g. "[1]" or "[2][3]".
_MARKERS_ONLY_RE = re.compile(r"^(?:\s*\[\d+\]\s*)+[.,;]?$")


def split_claims(markdown: str) -> list[str]:
    """Sentence-level claims out of a markdown summary.

    Splits on line breaks first so a bulleted list is not glued into one
    sentence by the absence of terminal punctuation, then on sentence-final
    punctuation within each line. Leading markdown bullets and heading hashes
    are stripped; emphasis markers are left alone because they do not affect
    word extraction.

    A fragment that is ONLY citation markers is folded back onto the claim in
    front of it. "Claim text. [1]" is an ordinary citation style and it is
    what a model writes unless told otherwise; splitting on the period leaves
    the claim with no marker and the marker with no claim, so a verbatim
    quotation of the cited abstract came back `uncited` and one such claim was
    enough to push the whole answer into abstention. Measured at 0.0127
    accuracy on the `verbatim` class of `run_grounding_eval.py` before this
    existed, and that harness reproduces the measurement.
    """
    claims: list[str] = []
    for line in (markdown or "").splitlines():
        line = re.sub(r"^\s*(?:[-*+]|\d+[.)])\s+", "", line)
        line = re.sub(r"^\s*#+\s*", "", line).strip()
        if not line:
            continue
        for fragment in _SENTENCE_SPLIT_RE.split(line):
            fragment = fragment.strip()
            if not fragment:
                continue
            if claims and _MARKERS_ONLY_RE.match(fragment):
                claims[-1] = f"{claims[-1]} {fragment}"
            else:
                claims.append(fragment)
    return claims


def check_grounding(
    markdown: str,
    papers: Sequence[ScoredPaper],
    *,
    threshold: float = SUPPORT_THRESHOLD,
) -> GroundingReport:
    """Classify every claim in `markdown` against the records it cites.

    `papers` is the record set the `[N]` markers index into, in the same order
    it was numbered for the summary prompt. Abstracts are read from these
    records, so pass the UNCOMPRESSED set: a claim lifted from a compressed
    abstract is still contained in the original, and checking against the
    compressed copy would let compression quietly decide what counts as
    grounded.
    """
    by_index = {i + 1: sp.paper for i, sp in enumerate(papers)}
    verdicts: list[ClaimVerdict] = []
    skipped = 0

    for text in split_claims(markdown):
        if len(content_words(text)) < MIN_CLAIM_CONTENT_WORDS:
            skipped += 1
            continue

        indices = tuple(sorted({int(n) for n in _MARKER_RE.findall(text)}))
        position = len(verdicts) + 1

        if not indices:
            verdicts.append(ClaimVerdict(
                position=position, text=text, cited_indices=(), cited_pmids=(),
                verdict="uncited", best_overlap=0.0, numeric_conflict=False,
            ))
            continue

        resolved = [(i, by_index[i]) for i in indices if i in by_index]
        if not resolved:
            verdicts.append(ClaimVerdict(
                position=position, text=text, cited_indices=indices, cited_pmids=(),
                verdict="dangling", best_overlap=0.0, numeric_conflict=False,
            ))
            continue

        best_overlap = max(overlap_ratio(text, p.abstract) for _, p in resolved)
        any_numbers_ok = any(numbers_check_out(text, p.abstract) for _, p in resolved)
        grounded = any(claim_is_grounded(text, p.abstract, threshold=threshold) for _, p in resolved)
        verdicts.append(ClaimVerdict(
            position=position,
            text=text,
            cited_indices=indices,
            cited_pmids=tuple(p.pmid for _, p in resolved),
            verdict="grounded" if grounded else "unsupported",
            best_overlap=round(best_overlap, 4),
            numeric_conflict=not any_numbers_ok,
        ))

    return GroundingReport(
        claims=tuple(verdicts),
        skipped_fragments=skipped,
        records_available=len(papers),
        threshold=threshold,
    )


def source_name(port: object) -> str:
    """A stable label for the retrieval backend that served a record.

    Unwraps `RetentionFilteredRetrieval` (and any other `_inner` wrapper) so
    the label names the thing that actually did the search rather than the
    filter in front of it. Module-qualified because `FakeRetrieval` and
    `CortexSearchRetriever` answering the same question is precisely the
    distinction a reader of a stored answer needs to be able to make later.
    """
    seen = set()
    while hasattr(port, "_inner") and id(port) not in seen:
        seen.add(id(port))
        port = port._inner
    cls = type(port)
    return f"{cls.__module__}.{cls.__qualname__}"


def provenance_for(papers: Sequence[ScoredPaper], source: str) -> list[RecordProvenance]:
    """Rank-annotated provenance for the record set an answer was built from."""
    return [
        RecordProvenance(
            pmid=sp.paper.pmid, source=source, retrieval_rank=i + 1, score=sp.score
        )
        for i, sp in enumerate(papers)
    ]


#: Served instead of a summary when `GroundingReport.answerable` is False.
#: Fixed text on purpose: the abstention must not be generated by the model
#: that just failed to ground its answer.
ABSTENTION_MARKDOWN = (
    "No retrieved record supports an answer to this query. "
    "Nothing is stated here from model knowledge alone."
)
