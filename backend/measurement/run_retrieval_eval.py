"""C25 - retrieval evaluation, with retrieval and final ordering judged apart.

Run with:
    python -m backend.measurement.run_retrieval_eval

Zero credentials. Writes backend/measurement/results/retrieval_eval.json.

--- WHY THIS IS NOT run_gate.py OR run_policy_bench.py ----------------------

`run_gate.py` reports one number (recall@10) over the whole corpus with no
split, so a setting tuned on those 28 queries is scored on the same 28
queries. `run_policy_bench.py` sweeps two dials and measures prompt tokens.
Neither separates "did retrieval find the paper" from "did the ranker put it
near the top", and neither checks whether a rendered citation is actually
supported by the passage it points at. Those three are what this file adds.

--- THE SPLIT, AND THE TRAP IT AVOIDS --------------------------------------

`backend/data/corpus.json` holds 329 records but only 312 distinct PMIDs: 17
PMIDs appear twice, tagged to different conditions. Splitting by RECORD would
therefore put the same source document on both sides of the split, which is
the exact leak the split exists to prevent. So the split is by PMID: every
record of a document goes to the same side, and `assert_disjoint()` proves it
on every run rather than trusting the construction.

Two folds, `dev` and `test`, assigned by a stable hash of the PMID plus a
version label. Deterministic, so a rerun compares like with like; and because
the label is part of the hash, changing `SPLIT_LABEL` reshuffles the whole
split rather than letting anyone hand-move an awkward document.

--- RETRIEVAL VERSUS ORDERING ----------------------------------------------

The system does hybrid retrieval and then re-ranks: `snowflake/retrieval.py`
applies the rarity boost AFTER the search call, and `memory/rerank.py` applies
the memory multiplier after that. Those are ordering stages, not retrieval
stages, and mixing them into one recall number hides which one is doing the
work. So:

  RETRIEVAL STAGE   varies only the QUERY FORMULATION (raw / expanded), pulls
                    a candidate pool of CANDIDATE_DEPTH with no boosts, and is
                    scored with recall at both depths plus the zero-hit count.
                    Set-based, order-free. Both depths, because recall@30 over
                    a ~165 document fold saturates around 0.77 and would hide
                    an effect that recall@10 shows.

  ORDERING STAGE    takes THE SAME candidate pool and varies only the
                    ordering (lexical / +rarity / +rarity+memory), truncates
                    to FINAL_DEPTH, and is scored with nDCG@10, MRR@10 and
                    precision@5. Order-sensitive, and always broken out by
                    rare versus common query: the rarity boost exists to lift
                    rare-condition papers, so on a common-condition query it
                    lifts papers that are by definition irrelevant. A pooled
                    nDCG marks it down for doing exactly what it is for, and
                    reporting only the pooled number would be an unfair test.

One variable each. An ordering that cannot add a document cannot change
recall, and a retrieval that only widens the pool cannot be credited for
ranking.

--- THE HONEST LIMIT ON "HyDE" ---------------------------------------------

Trace's HyDE writes a hypothetical case report with Cortex COMPLETE. That
needs Snowflake credentials this environment does not have, so the live HyDE
number is BLOCKED, not estimated. Two stand-ins are measured instead, and
both are labelled in the output:

  prf       a local pseudo-relevance-feedback expander. Retrieves on the raw
            query, harvests the highest-IDF terms from the top few documents,
            and appends them. It is a real query-expansion technique and the
            closest credential-free analogue of HyDE, but it is NOT HyDE.
  fakellm   the actual `backend/app/loop/hyde.run_hyde`, driven by
            `FakeLLM`, whose canned reply is the constant string "fake
            expanded query for hyde" for EVERY query. I expected this arm to
            be inert; measured, it is worse than inert. The same constant
            terms are appended to all 28 queries, and that costs 0.069 of
            recall@30 on the held-out fold. So it is not a null arm, it is a
            noise arm, and it is reported that way. Anyone who runs the
            pipeline under the `fake` profile and reads a HyDE number is
            reading the cost of a constant, not the value of HyDE.

--- COST -------------------------------------------------------------------

Token counts are real counts of the real prompts the pipeline assembles.
The dollar figures are not real money: under the `fake` profile
`FakeLLM` prices every token at $0.000002, so the USD column is a unit-count
in disguise. It is reported because the RATIO between arms is meaningful and
the absolute value is not, and that is said in the output too. p95 latency is
in-process only: no Cortex Search call, no COMPLETE call, no network. Treat it
as a floor.
"""
from __future__ import annotations

import hashlib
import json
import math
import statistics
import time
from dataclasses import dataclass, field, replace
from pathlib import Path

from backend.app.loop.hyde import run_hyde
from backend.app.retrieval.rarity import rarity_boost
from backend.contracts.fakes import (
    COMMON_BOOST,
    RARE_BOOST,
    FakeLLM,
    _load_corpus,
    _tokenize,
)
from backend.contracts.models import Paper, ResearcherProfile, ScoredPaper
from backend.measurement.run_gate import GOLD_SET, RARE_CONDITIONS
from backend.memory.rerank import apply_memory

RESULTS_DIR = Path(__file__).resolve().parent / "results"
RESULTS_PATH = RESULTS_DIR / "retrieval_eval.json"

#: Bump to reshuffle the document split. Part of the hash on purpose: a split
#: nobody can hand-edit one document out of.
SPLIT_LABEL = "c25-v1"

#: How deep the candidate pool goes before any re-ranking. Matches the
#: over-fetch `snowflake/retrieval.py` already performs (`max(top_k*4, 40)`
#: at top_k=10 is 40; 30 is the widest depth every fold can fill).
CANDIDATE_DEPTH = 30

#: What the user finally sees ranked. `pipeline.RETRIEVAL_TOP_K` is 10.
FINAL_DEPTH = 10

#: PRF expansion: how many top documents to harvest terms from, and how many.
PRF_DOCS = 3
PRF_TERMS = 12


# ============================================================================
# The split
# ============================================================================


@dataclass(frozen=True)
class Fold:
    name: str
    papers: list[Paper]

    @property
    def pmids(self) -> set[str]:
        return {p.pmid for p in self.papers}

    def relevant(self, condition: str) -> set[str]:
        return {p.pmid for p in self.papers if p.condition == condition}


def fold_of(pmid: str, folds: int = 2, label: str = SPLIT_LABEL) -> int:
    digest = hashlib.sha256(f"{label}:{pmid}".encode()).hexdigest()
    return int(digest, 16) % folds


def make_folds(papers: list[Paper] | None = None, label: str = SPLIT_LABEL) -> dict[str, Fold]:
    """Split by PMID, never by record.

    A PMID that appears in several records (17 do) lands wholly on one side,
    so no source document can inform both the tuning side and the held-out
    side.
    """
    papers = list(papers if papers is not None else _load_corpus())
    buckets: dict[int, list[Paper]] = {0: [], 1: []}
    for paper in papers:
        buckets[fold_of(paper.pmid, label=label)].append(paper)
    return {"dev": Fold("dev", buckets[0]), "test": Fold("test", buckets[1])}


def assert_disjoint(folds: dict[str, Fold]) -> dict:
    """Proof, computed every run, that no document straddles the split."""
    dev, test = folds["dev"], folds["test"]
    overlap = dev.pmids & test.pmids
    if overlap:
        raise AssertionError(f"document leak across the split: {sorted(overlap)[:5]}")
    return {
        "dev_records": len(dev.papers),
        "test_records": len(test.papers),
        "dev_documents": len(dev.pmids),
        "test_documents": len(test.pmids),
        "shared_documents": 0,
        "split_label": SPLIT_LABEL,
    }


# ============================================================================
# Retrieval, scoped to one fold
# ============================================================================


class FoldRetrieval:
    """The same scorer `FakeRetrieval` uses, restricted to one fold.

    Deliberately reuses `contracts.fakes._tokenize` and the same RARE_BOOST /
    COMMON_BOOST constants rather than reimplementing them, so the baseline
    arm here is byte-for-byte the shipped fake retriever's scoring and cannot
    be accused of being sandbagged.
    """

    def __init__(self, fold: Fold) -> None:
        self._papers = fold.papers

    def candidates(self, query: str, *, depth: int = CANDIDATE_DEPTH) -> list[ScoredPaper]:
        """Unboosted candidate pool. This is the RETRIEVAL stage output."""
        query_tokens = _tokenize(query)
        scored: list[ScoredPaper] = []
        for paper in self._papers:
            overlap = float(len(query_tokens & (_tokenize(paper.title) | _tokenize(paper.abstract))))
            scored.append(
                ScoredPaper(
                    paper=paper, score=overlap, lexical_score=overlap,
                    semantic_score=0.0, rarity_multiplier=1.0,
                )
            )
        scored.sort(key=lambda sp: (sp.score, sp.paper.pmid), reverse=True)
        return scored[:depth]


def expand_prf(retrieval: FoldRetrieval, query: str) -> str:
    """Pseudo-relevance feedback. A local stand-in for HyDE, not HyDE.

    Highest-IDF terms from the top PRF_DOCS documents, appended to the query.
    IDF is computed over the fold, so a term that is everywhere adds nothing.
    """
    top = retrieval.candidates(query, depth=PRF_DOCS)
    if not top:
        return query
    n = len(retrieval._papers) or 1
    document_freq: dict[str, int] = {}
    for paper in retrieval._papers:
        for token in _tokenize(paper.title) | _tokenize(paper.abstract):
            document_freq[token] = document_freq.get(token, 0) + 1

    query_tokens = _tokenize(query)
    candidate_terms: set[str] = set()
    for sp in top:
        candidate_terms |= _tokenize(sp.paper.title) | _tokenize(sp.paper.abstract)
    candidate_terms -= query_tokens

    ranked = sorted(
        candidate_terms,
        key=lambda t: (math.log(n / (1 + document_freq.get(t, 0))), t),
        reverse=True,
    )
    return f"{query} {' '.join(ranked[:PRF_TERMS])}"


def expand_fakellm(query: str) -> str:
    """The real `run_hyde`, driven by FakeLLM. Inert by construction."""
    return run_hyde(
        FakeLLM(), query, request_id="c25-eval", session_id="c25-eval", user_id="c25-eval"
    )


# ============================================================================
# Ordering
# ============================================================================


def order_lexical(candidates: list[ScoredPaper], **_) -> list[ScoredPaper]:
    return list(candidates)


def order_rarity(candidates: list[ScoredPaper], **_) -> list[ScoredPaper]:
    """The shipped post-retrieval re-rank: score * rarity multiplier."""
    boosted = [
        replace(
            sp,
            score=sp.score * rarity_boost({"rarity": "rare" if sp.paper.is_rare else "common"}),
            rarity_multiplier=rarity_boost(
                {"rarity": "rare" if sp.paper.is_rare else "common"}
            ),
        )
        for sp in candidates
    ]
    boosted.sort(key=lambda sp: (sp.score, sp.paper.pmid), reverse=True)
    return boosted


def order_rarity_memory(
    candidates: list[ScoredPaper], *, profile: ResearcherProfile, seen: set[str]
) -> list[ScoredPaper]:
    """Rarity, then the real `memory.rerank.apply_memory`."""
    reranked, _demoted = apply_memory(order_rarity(candidates), profile, seen)
    return reranked


# ============================================================================
# Metrics
# ============================================================================


def recall(ranked: list[ScoredPaper], relevant: set[str]) -> float:
    if not relevant:
        return float("nan")
    found = {sp.paper.pmid for sp in ranked} & relevant
    return len(found) / len(relevant)


def ndcg_at(ranked: list[ScoredPaper], relevant: set[str], k: int) -> float:
    """Binary-gain nDCG. Ideal DCG uses min(k, |relevant|) hits, so a query
    with fewer relevant documents than k is not penalised for the shortfall."""
    if not relevant:
        return float("nan")
    gains = [1.0 if sp.paper.pmid in relevant else 0.0 for sp in ranked[:k]]
    dcg = sum(g / math.log2(i + 2) for i, g in enumerate(gains))
    ideal = sum(1.0 / math.log2(i + 2) for i in range(min(k, len(relevant))))
    return dcg / ideal if ideal else float("nan")


def mrr_at(ranked: list[ScoredPaper], relevant: set[str], k: int) -> float:
    for i, sp in enumerate(ranked[:k]):
        if sp.paper.pmid in relevant:
            return 1.0 / (i + 1)
    return 0.0


def precision_at(ranked: list[ScoredPaper], relevant: set[str], k: int) -> float:
    if k == 0:
        return float("nan")
    return sum(1 for sp in ranked[:k] if sp.paper.pmid in relevant) / k


def _mean(values: list[float]) -> float:
    usable = [v for v in values if not math.isnan(v)]
    return round(statistics.fmean(usable), 4) if usable else float("nan")


# ============================================================================
# Citation support
# ============================================================================

_SUPPORT_THRESHOLD = 0.8


def claim_is_supported(sentence: str, abstract: str) -> bool:
    """Does the cited passage actually contain the claim?

    Content-word containment: what fraction of the claim's tokens occur in the
    abstract it cites. Deliberately not the pipeline's own `citation_check`
    verdict - that is produced by the same LLM stand-in that wrote the
    summary, so using it here would be marking its own homework.
    """
    import re

    claim_tokens = _tokenize(re.sub(r"\[\d+\]", " ", sentence))
    if not claim_tokens:
        return False
    return len(claim_tokens & _tokenize(abstract)) / len(claim_tokens) >= _SUPPORT_THRESHOLD


def measure_citation_support(result, *, shuffle_attribution: bool = False) -> dict:
    """Per-claim support for one `QueryResult`.

    `shuffle_attribution` rotates every [N] marker onto the next paper. It is
    a control, not a measurement: if the metric still reports full support
    after every citation has been pointed at the wrong paper, the metric is
    vacuous and no number it produces means anything.

    `uncited` counts assertions in the summary that carry no [N] at all.
    Iterating `result.citations` alone cannot see them: that list is built by
    enumerating the markers that ARE present, so a summary half made of
    unsourced assertion used to report a support rate of 1.0, which
    `test_grounding_eval.py` reproduces. They are counted
    into the denominator, because a claim from nowhere is a worse failure than
    a claim cited to the wrong paper, not an absent one.
    """
    import re

    from backend.app.verify.grounding import content_words, split_claims

    by_index = {i + 1: sp.paper for i, sp in enumerate(result.papers)}
    if shuffle_attribution and len(by_index) > 1:
        n = len(by_index)
        by_index = {i: by_index[(i % n) + 1] for i in range(1, n + 1)}

    sentences = re.split(r"(?<=[.!?])\s+", result.summary_markdown or "")
    supported = unsupported = unresolvable = 0
    for citation in result.citations:
        paper = by_index.get(citation.index)
        if paper is None:
            unresolvable += 1
            continue
        sentence = next((s for s in sentences if f"[{citation.index}]" in s), "")
        if claim_is_supported(sentence, paper.abstract):
            supported += 1
        else:
            unsupported += 1

    uncited = sum(
        1
        for claim in split_claims(result.summary_markdown or "")
        if not re.search(r"\[\d+\]", claim) and len(content_words(claim)) >= 3
    )

    total = supported + unsupported + unresolvable + uncited
    return {
        "claims": total,
        "supported": supported,
        "unsupported": unsupported,
        "unresolvable": unresolvable,
        "uncited": uncited,
        "support_rate": round(supported / total, 4) if total else float("nan"),
    }


# ============================================================================
# The runs
# ============================================================================


@dataclass
class QuerySpec:
    query: str
    condition: str
    is_rare: bool


def gold_for_fold(fold: Fold) -> list[QuerySpec]:
    """The fixed test set, scoped to what this fold can actually answer.

    A query whose condition has no documents on this side of the split is
    dropped and counted, not scored as a zero: it would measure the split, not
    the retriever.
    """
    specs = []
    for query, condition in GOLD_SET:
        if fold.relevant(condition):
            specs.append(QuerySpec(query, condition, condition in RARE_CONDITIONS))
    return specs


EXPANSIONS = ("none", "prf", "fakellm")
ORDERINGS = ("lexical", "rarity", "rarity+memory")


def _seeded_memory(specs: list[QuerySpec], fold: Fold, index: int):
    """A deterministic returning user: has explored the previous query's
    condition and already seen two of its papers."""
    prior = specs[index - 1] if index else specs[-1]
    seen = set(sorted(fold.relevant(prior.condition))[:2])
    profile = ResearcherProfile(
        user_id="c25-eval", specialty=None, conditions_explored=[prior.condition], query_count=3
    )
    return profile, seen


def evaluate_fold(fold: Fold) -> dict:
    retrieval = FoldRetrieval(fold)
    specs = gold_for_fold(fold)

    retrieval_rows: dict[str, dict] = {}
    ordering_rows: dict[str, dict] = {}
    pools: dict[str, list[list[ScoredPaper]]] = {}

    # -- retrieval stage: one variable, the query formulation ---------------
    for expansion in EXPANSIONS:
        recalls, tight_recalls, zero_hits, latencies = [], [], 0, []
        pool_for_expansion: list[list[ScoredPaper]] = []
        for spec in specs:
            started = time.perf_counter()
            if expansion == "none":
                query = spec.query
            elif expansion == "prf":
                query = expand_prf(retrieval, spec.query)
            else:
                query = f"{spec.query} {expand_fakellm(spec.query)}"
            candidates = retrieval.candidates(query, depth=CANDIDATE_DEPTH)
            latencies.append((time.perf_counter() - started) * 1000)
            pool_for_expansion.append(candidates)

            relevant = fold.relevant(spec.condition)
            r = recall(candidates, relevant)
            recalls.append(r)
            # Recall at the candidate depth saturates: 30 slots over a ~165
            # document fold is 18% of the fold, so almost anything scores well.
            # The tighter depth is where a query reformulation has room to
            # show an effect, so both are reported.
            tight_recalls.append(recall(candidates[:FINAL_DEPTH], relevant))
            if r == 0.0:
                zero_hits += 1
        pools[expansion] = pool_for_expansion
        rare_recalls = [r for r, s in zip(recalls, specs) if s.is_rare]
        retrieval_rows[expansion] = {
            "queries": len(specs),
            f"recall_at_{CANDIDATE_DEPTH}": _mean(recalls),
            f"recall_at_{FINAL_DEPTH}": _mean(tight_recalls),
            f"rare_recall_at_{CANDIDATE_DEPTH}": _mean(rare_recalls),
            "zero_hit_queries": zero_hits,
            "median_latency_ms": round(statistics.median(latencies), 3) if latencies else 0.0,
            "p95_latency_ms": round(_p95(latencies), 3),
        }

    # -- ordering stage: SAME pool, one variable, the ordering --------------
    fixed_pool = pools["none"]
    for ordering in ORDERINGS:
        ndcgs, mrrs, precisions, latencies = [], [], [], []
        for index, (spec, candidates) in enumerate(zip(specs, fixed_pool)):
            profile, seen = _seeded_memory(specs, fold, index)
            started = time.perf_counter()
            if ordering == "lexical":
                ranked = order_lexical(candidates)
            elif ordering == "rarity":
                ranked = order_rarity(candidates)
            else:
                ranked = order_rarity_memory(candidates, profile=profile, seen=seen)
            latencies.append((time.perf_counter() - started) * 1000)

            relevant = fold.relevant(spec.condition)
            ndcgs.append(ndcg_at(ranked, relevant, FINAL_DEPTH))
            mrrs.append(mrr_at(ranked, relevant, FINAL_DEPTH))
            precisions.append(precision_at(ranked, relevant, 5))
        # The rarity boost exists to surface rare-condition papers. A pooled
        # nDCG cannot reward that: on a COMMON-condition query the boost lifts
        # rare papers that are by definition irrelevant, so pooling the two
        # marks the boost down for doing its job. Split out, always.
        rare_ndcgs = [v for v, s in zip(ndcgs, specs) if s.is_rare]
        common_ndcgs = [v for v, s in zip(ndcgs, specs) if not s.is_rare]
        ordering_rows[ordering] = {
            "candidate_pool": "expansion=none, identical across orderings",
            f"ndcg_at_{FINAL_DEPTH}": _mean(ndcgs),
            f"ndcg_at_{FINAL_DEPTH}_rare_queries": _mean(rare_ndcgs),
            f"ndcg_at_{FINAL_DEPTH}_common_queries": _mean(common_ndcgs),
            f"mrr_at_{FINAL_DEPTH}": _mean(mrrs),
            "precision_at_5": _mean(precisions),
            "median_latency_ms": round(statistics.median(latencies), 3) if latencies else 0.0,
            "p95_latency_ms": round(_p95(latencies), 3),
        }

    return {
        "fold": fold.name,
        "documents": len(fold.pmids),
        "records": len(fold.papers),
        "queries_scored": len(specs),
        "queries_dropped_no_relevant_document_in_fold": len(GOLD_SET) - len(specs),
        "retrieval_stage": retrieval_rows,
        "ordering_stage": ordering_rows,
    }


def _p95(values: list[float]) -> float:
    if not values:
        return 0.0
    ordered = sorted(values)
    index = min(len(ordered) - 1, math.ceil(0.95 * len(ordered)) - 1)
    return ordered[max(index, 0)]


def evaluate_end_to_end(fold: Fold, limit: int | None = None) -> dict:
    """Cost, p95 and citation support from the real pipeline.

    Runs the whole `run_query` path (hyde -> retrieve -> relevance gate ->
    summarise -> citation check) so the cost and latency are the pipeline's
    own, not the harness's. Retrieval here is the FULL corpus, not the fold:
    `run_query` resolves its own services and there is no supported seam to
    scope it, so this section is labelled as un-split and is used only for
    cost / latency / citation-plumbing, never for a recall claim.
    """
    from backend.app.pipeline import run_query

    specs = gold_for_fold(fold)[: limit or len(gold_for_fold(fold))]
    latencies, tokens, costs = [], [], []
    support = {"claims": 0, "supported": 0, "unsupported": 0, "unresolvable": 0}
    shuffled = {"claims": 0, "supported": 0, "unsupported": 0, "unresolvable": 0}

    for i, spec in enumerate(specs):
        started = time.perf_counter()
        result = run_query(spec.query, f"c25-user-{i}", f"c25-session-{i}", personalize=False)
        latencies.append((time.perf_counter() - started) * 1000)
        tokens.append(result.cost.total_tokens)
        costs.append(result.cost.cost_usd)

        for target, kwargs in ((support, {}), (shuffled, {"shuffle_attribution": True})):
            measured = measure_citation_support(result, **kwargs)
            for key in ("claims", "supported", "unsupported", "unresolvable"):
                target[key] += measured[key]

    def rate(block: dict) -> float:
        return round(block["supported"] / block["claims"], 4) if block["claims"] else float("nan")

    return {
        "note": (
            "un-split: run_query resolves its own services and retrieves over the "
            "full corpus. Used for cost, latency and citation plumbing only."
        ),
        "queries": len(specs),
        "median_latency_ms": round(statistics.median(latencies), 2) if latencies else 0.0,
        "p95_latency_ms": round(_p95(latencies), 2),
        "median_total_tokens": round(statistics.median(tokens), 1) if tokens else 0,
        "total_cost_usd_fake_prices": round(sum(costs), 8),
        "citation_support": {**support, "support_rate": rate(support)},
        "citation_support_shuffled_control": {**shuffled, "support_rate": rate(shuffled)},
    }


def compare(dev: dict, test: dict) -> dict:
    """Baseline versus the fancy path, on the held-out side, stated plainly."""
    r = test["retrieval_stage"]
    o = test["ordering_stage"]
    recall_key = f"recall_at_{CANDIDATE_DEPTH}"
    ndcg_key = f"ndcg_at_{FINAL_DEPTH}"

    retrieval_delta = r["prf"][recall_key] - r["none"][recall_key]
    ordering_delta = o["rarity"][ndcg_key] - o["lexical"][ndcg_key]
    rare_key = f"{ndcg_key}_rare_queries"
    rare_ordering_delta = o["rarity"][rare_key] - o["lexical"][rare_key]
    memory_delta = o["rarity+memory"][ndcg_key] - o["rarity"][ndcg_key]
    fakellm_delta = r["fakellm"][recall_key] - r["none"][recall_key]

    verdicts = {
        "expansion_beats_raw_query": retrieval_delta > 0,
        "rarity_rerank_beats_lexical_order": ordering_delta > 0,
        "rarity_rerank_beats_lexical_order_on_rare_queries": rare_ordering_delta > 0,
        "memory_rerank_beats_rarity_alone": memory_delta > 0,
        "fakellm_hyde_changes_anything": abs(fakellm_delta) > 1e-9,
    }
    return {
        "held_out_fold": "test",
        "baseline": "expansion=none, ordering=lexical, zero LLM calls",
        f"retrieval_delta_{recall_key}_prf_minus_none": round(retrieval_delta, 4),
        f"ordering_delta_{ndcg_key}_rarity_minus_lexical": round(ordering_delta, 4),
        f"ordering_delta_{ndcg_key}_rarity_minus_lexical_rare_queries_only": round(rare_ordering_delta, 4),
        f"ordering_delta_{ndcg_key}_memory_minus_rarity": round(memory_delta, 4),
        f"retrieval_delta_{recall_key}_fakellm_minus_none": round(fakellm_delta, 4),
        "verdicts": verdicts,
        "dev_agrees_with_test_on_expansion": (
            (dev["retrieval_stage"]["prf"][recall_key] - dev["retrieval_stage"]["none"][recall_key] > 0)
            == verdicts["expansion_beats_raw_query"]
        ),
    }


def run() -> dict:
    folds = make_folds()
    report = {
        "split": assert_disjoint(folds),
        "method": {
            "test_set": f"{len(GOLD_SET)} fixed gold queries from run_gate.GOLD_SET",
            "relevance": "a paper is relevant to a query if its condition is the query's gold condition",
            "split_unit": "PMID, not record - 17 PMIDs appear in more than one record",
            "retrieval_stage": "varies query formulation only; recall is order-free",
            "ordering_stage": "same candidate pool for every ordering; nDCG/MRR/P@5",
            "hyde": "BLOCKED live. 'prf' is a local stand-in; 'fakellm' is inert by construction",
            "cost": "token counts are real; USD uses FakeLLM's $0.000002/token and is not money",
            "latency": "in-process only, no network. A floor, not a production p95",
        },
        "dev": evaluate_fold(folds["dev"]),
        "test": evaluate_fold(folds["test"]),
    }
    report["end_to_end"] = evaluate_end_to_end(folds["test"])
    report["comparison"] = compare(report["dev"], report["test"])
    return report


def main() -> None:
    report = run()
    RESULTS_DIR.mkdir(parents=True, exist_ok=True)
    RESULTS_PATH.write_text(json.dumps(report, indent=2))

    split = report["split"]
    print(f"split ({split['split_label']}): dev {split['dev_documents']} documents / "
          f"{split['dev_records']} records, test {split['test_documents']} / "
          f"{split['test_records']}, shared {split['shared_documents']}")
    for fold_name in ("dev", "test"):
        fold = report[fold_name]
        print(f"\n--- {fold_name} ({fold['queries_scored']} queries scored, "
              f"{fold['queries_dropped_no_relevant_document_in_fold']} dropped) ---")
        print("  RETRIEVAL STAGE (query formulation only)")
        for name, row in fold["retrieval_stage"].items():
            print(f"    {name:9s} recall@{CANDIDATE_DEPTH}={row[f'recall_at_{CANDIDATE_DEPTH}']:.4f} "
                  f"recall@{FINAL_DEPTH}={row[f'recall_at_{FINAL_DEPTH}']:.4f} "
                  f"rare={row[f'rare_recall_at_{CANDIDATE_DEPTH}']:.4f} "
                  f"zero-hit={row['zero_hit_queries']} p95={row['p95_latency_ms']:.2f}ms")
        print("  ORDERING STAGE (same candidate pool)")
        for name, row in fold["ordering_stage"].items():
            print(f"    {name:14s} nDCG@{FINAL_DEPTH}={row[f'ndcg_at_{FINAL_DEPTH}']:.4f} "
                  f"(rare {row[f'ndcg_at_{FINAL_DEPTH}_rare_queries']:.4f} / "
                  f"common {row[f'ndcg_at_{FINAL_DEPTH}_common_queries']:.4f}) "
                  f"MRR={row[f'mrr_at_{FINAL_DEPTH}']:.4f} P@5={row['precision_at_5']:.4f} "
                  f"p95={row['p95_latency_ms']:.3f}ms")

    e2e = report["end_to_end"]
    print(f"\n--- end to end ({e2e['queries']} queries, un-split) ---")
    print(f"  median {e2e['median_latency_ms']:.1f}ms  p95 {e2e['p95_latency_ms']:.1f}ms  "
          f"median {e2e['median_total_tokens']:.0f} tokens")
    print(f"  citation support {e2e['citation_support']['support_rate']} "
          f"over {e2e['citation_support']['claims']} claims")
    print(f"  shuffled-attribution control {e2e['citation_support_shuffled_control']['support_rate']} "
          f"(must be lower, or the metric is vacuous)")

    print("\n--- comparison, held out fold ---")
    for key, value in report["comparison"].items():
        if key == "verdicts":
            for verdict, outcome in value.items():
                print(f"  {verdict}: {outcome}")
        else:
            print(f"  {key}: {value}")
    print(f"\nwrote {RESULTS_PATH}")


if __name__ == "__main__":
    main()
