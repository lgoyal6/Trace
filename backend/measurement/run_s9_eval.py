"""Paired local retrieval comparison for S9.

This is an exploratory replay over Trace's existing 28 condition queries, not
an independently adjudicated or unseen-source study. It uses the existing
document-level C25 split, compares BM25, the shipped deterministic local dense
features, and reciprocal-rank fusion on the same held-out queries, and keeps
per-query raw metrics so the aggregate can be reconstructed.

    python -m backend.measurement.run_s9_eval --output /path/to/raw.json

No network, model provider, or external store is used. Synthetic no-answer
controls are evaluated separately from the existing query set.
"""
from __future__ import annotations

import argparse
import hashlib
import json
import math
import statistics
import time
from collections import Counter, defaultdict
from pathlib import Path
from typing import Iterable

from backend.contracts.models import Paper
from backend.measurement.run_gate import GOLD_SET
from backend.measurement.run_retrieval_eval import make_folds
from backend.vectorstore.retrieval import _cosine, _embed, _tokens

CORPUS_PATH = Path(__file__).resolve().parent.parent / "data" / "corpus.json"
PROTOCOL = {
    "protocol_id": "s9-local-paired-v2",
    "split": "existing PMID-level c25-v1 test fold; dev/test disjointness checked at runtime",
    "queries": "all existing GOLD_SET queries whose condition appears in test fold; condition is relevance label",
    "methods": {
        "bm25": "local BM25 over title+abstract, k1=1.5, b=0.75",
        "dense": "the existing LocalVectorRetriever deterministic 256-d word+bigram feature hash, no rarity boost",
        "hybrid_rrf": "reciprocal-rank fusion of each method's top 30; k=60",
    },
    "candidate_depth": 30,
    "final_depth": 10,
    "metrics": ["recall_at_10", "mrr_at_10", "ndcg_at_10", "precision_at_5"],
    "answerability": "study-only lexical-evidence gate: BM25 score > 0; raw method candidate-positive status is also retained. This is not an app/API gate.",
    "synthetic_controls": "3 no-answer strings, each 3 random 12-character lowercase tokens, generated from seed 20261015 after protocol lock",
    "repetitions": 5,
    "latency": "local in-process method latency; hybrid includes BM25+dense+fusion; descriptive, warmup excluded",
    "duplicate_pmids": "one canonical row per PMID, first row wins, matching LocalVectorRetriever index initialization",
    "uncertainty": "paired bootstrap of condition clusters, 2,000 replicates, seed 20261015; percentile 95% intervals",
    "limitations": [
        "28 existing condition queries are not documented as independently adjudicated",
        "query wording was drawn from corpus titles/themes, so this is not unseen-source generalization",
        "condition membership is a coarse relevance label, not claim-level citation adjudication",
        "hashed local dense features are not a pretrained embedding model or ANN index",
        "synthetic no-answer controls do not represent researcher sessions",
    ],
}


def _tokens_for(paper: Paper) -> list[str]:
    return _tokens(f"{paper.title} {paper.abstract}")


class BM25Index:
    def __init__(self, papers: list[Paper], *, k1: float = 1.5, b: float = 0.75):
        self.papers = papers
        self.k1, self.b = k1, b
        self.docs = [_tokens_for(p) for p in papers]
        self.tf = [Counter(doc) for doc in self.docs]
        self.lengths = [len(doc) for doc in self.docs]
        self.avglen = sum(self.lengths) / len(self.lengths) if self.lengths else 0.0
        df: Counter[str] = Counter()
        for doc in self.docs:
            df.update(set(doc))
        n = len(self.docs)
        self.idf = {term: math.log(1 + (n - count + 0.5) / (count + 0.5))
                    for term, count in df.items()}

    def search(self, query: str, limit: int) -> list[tuple[Paper, float]]:
        scores: dict[int, float] = defaultdict(float)
        for term in _tokens(query):
            idf = self.idf.get(term)
            if idf is None:
                continue
            for i, tf in enumerate(self.tf):
                freq = tf[term]
                if not freq:
                    continue
                denom = freq + self.k1 * (1 - self.b + self.b * self.lengths[i] / (self.avglen or 1))
                scores[i] += idf * freq * (self.k1 + 1) / denom
        ranked = sorted(scores.items(), key=lambda item: (-item[1], self.papers[item[0]].pmid))
        return [(self.papers[i], score) for i, score in ranked[:limit]]


class DenseIndex:
    def __init__(self, papers: list[Paper]):
        self.papers = papers
        self.vectors = [_embed(f"{p.title} {p.abstract}") for p in papers]

    def search(self, query: str, limit: int) -> list[tuple[Paper, float]]:
        vector = _embed(query)
        scored = [(max(0.0, _cosine(vector, v)), p) for p, v in zip(self.papers, self.vectors)]
        scored.sort(key=lambda item: (-item[0], item[1].pmid))
        return [(p, score) for score, p in scored[:limit]]


def rrf(*ranked_lists: Iterable[tuple[Paper, float]], k: int = 60,
        limit: int = 10) -> list[tuple[Paper, float]]:
    by_pmid: dict[str, Paper] = {}
    scores: dict[str, float] = defaultdict(float)
    for rows in ranked_lists:
        for rank, (paper, _) in enumerate(rows, 1):
            by_pmid[paper.pmid] = paper
            scores[paper.pmid] += 1.0 / (k + rank)
    ranked = sorted(scores, key=lambda pmid: (-scores[pmid], pmid))[:limit]
    return [(by_pmid[pmid], scores[pmid]) for pmid in ranked]


def _metrics(rows: list[tuple[Paper, float]], relevant: set[str]) -> dict[str, float]:
    pmids = [p.pmid for p, _ in rows]
    found = set(pmids) & relevant
    recall = len(found) / len(relevant) if relevant else math.nan
    reciprocal_rank = next((1 / i for i, pmid in enumerate(pmids, 1) if pmid in relevant), 0.0)
    dcg = sum(1 / math.log2(i + 1) for i, pmid in enumerate(pmids[:10], 1) if pmid in relevant)
    ideal = sum(1 / math.log2(i + 1) for i in range(1, min(len(relevant), 10) + 1))
    return {
        "recall_at_10": recall,
        "mrr_at_10": reciprocal_rank,
        "ndcg_at_10": dcg / ideal if ideal else math.nan,
        "precision_at_5": sum(pmid in relevant for pmid in pmids[:5]) / 5,
    }


def _mean(values: list[float]) -> float | None:
    finite = [x for x in values if math.isfinite(x)]
    return round(statistics.mean(finite), 6) if finite else None


def _paired_bootstrap_deltas(rows: list[dict], *, replicates: int = 2000,
                             seed: int = 20261015) -> dict:
    """Paired percentile intervals resampling conditions, not query rows."""
    import random

    grouped: dict[str, list[dict]] = defaultdict(list)
    for row in rows:
        grouped[row["condition_label"]].append(row)
    clusters = sorted(grouped)
    rng = random.Random(seed)
    metrics = ("recall_at_10", "mrr_at_10", "ndcg_at_10", "precision_at_5")
    output = {}
    for method in ("dense", "hybrid_rrf"):
        output[method] = {}
        for metric in metrics:
            deltas = []
            for _ in range(replicates):
                sampled = [clusters[rng.randrange(len(clusters))] for _ in clusters]
                pairs = [(r["methods"][method]["metrics"][metric],
                          r["methods"]["bm25"]["metrics"][metric])
                         for cluster in sampled for r in grouped[cluster]]
                deltas.append(statistics.mean(a - b for a, b in pairs))
            ordered = sorted(deltas)
            output[method][metric] = {
                "mean_delta_vs_bm25": round(statistics.mean(
                    r["methods"][method]["metrics"][metric]
                    - r["methods"]["bm25"]["metrics"][metric] for r in rows
                ), 6),
                "ci95_percentile": [round(ordered[int(0.025 * (replicates - 1))], 6),
                                     round(ordered[int(0.975 * (replicates - 1))], 6)],
                "clusters": len(clusters),
                "replicates": replicates,
                "seed": seed,
            }
    return output


def _synthetic_no_answer_queries(seed: int = 20261015, count: int = 3) -> list[str]:
    import random
    import string

    rng = random.Random(seed)
    return [" ".join("".join(rng.choice(string.ascii_lowercase) for _ in range(12))
                     for _ in range(3)) for _ in range(count)]


def evaluate(*, repetitions: int = 5) -> dict:
    if repetitions < 1:
        raise ValueError("repetitions must be positive")
    folds = make_folds()
    test = folds["test"]
    if folds["dev"].pmids & test.pmids:
        raise AssertionError("document leak across dev/test split")
    if not test.papers:
        raise AssertionError("empty held-out fold")

    canonical: dict[str, Paper] = {}
    for paper in test.papers:
        canonical.setdefault(paper.pmid, paper)
    papers = list(canonical.values())
    relevant_by_condition: dict[str, set[str]] = defaultdict(set)
    for paper in papers:
        relevant_by_condition[paper.condition].add(paper.pmid)
    bm25, dense = BM25Index(papers), DenseIndex(papers)
    query_specs = [(q, condition) for q, condition in GOLD_SET if relevant_by_condition[condition]]
    if not query_specs:
        raise AssertionError("no labeled queries resolve to the held-out fold")

    raw_rows = []
    latency_samples: dict[str, list[float]] = {name: [] for name in ("bm25", "dense", "hybrid_rrf")}
    # Warm both indexes once; then time each paired query/repetition in a fixed
    # order. Metrics use the final repetition, which is deterministic.
    for query, condition in query_specs:
        bm25.search(query, 30)
        dense.search(query, 30)
    for repetition in range(repetitions):
        for query, condition in query_specs:
            relevant = relevant_by_condition[condition]
            started = time.perf_counter()
            hybrid_started = started
            lex = bm25.search(query, 30)
            latency_samples["bm25"].append((time.perf_counter() - started) * 1000)
            started = time.perf_counter()
            sem = dense.search(query, 30)
            latency_samples["dense"].append((time.perf_counter() - started) * 1000)
            started = time.perf_counter()
            fused = rrf(lex, sem, k=60, limit=10)
            latency_samples["hybrid_rrf"].append((time.perf_counter() - hybrid_started) * 1000)
            if repetition == repetitions - 1:
                methods = {"bm25": lex[:10], "dense": sem[:10], "hybrid_rrf": fused}
                signals = {
                    "bm25": bool(lex and lex[0][1] > 0),
                    "dense": bool(lex and lex[0][1] > 0),
                    "hybrid_rrf": bool(lex and lex[0][1] > 0),
                }
                raw_rows.append({
                    "query": query,
                    "condition_label": condition,
                    "relevant_documents": sorted(relevant),
                    "methods": {name: {
                        "ranked_pmids": [p.pmid for p, _ in ranked],
                        "top_score": ranked[0][1] if ranked else 0.0,
                        "raw_candidate_positive": bool(ranked and ranked[0][1] > 0),
                        "resolver_would_answer": signals[name],
                        "metrics": _metrics(ranked, relevant),
                    } for name, ranked in methods.items()},
                })

    summary = {}
    for name in latency_samples:
        values = [row["methods"][name]["metrics"] for row in raw_rows]
        summary[name] = {
            metric: _mean([v[metric] for v in values])
            for metric in ("recall_at_10", "mrr_at_10", "ndcg_at_10", "precision_at_5")
        }
        summary[name]["answerable_queries"] = sum(
            row["methods"][name]["resolver_would_answer"] for row in raw_rows
        )
        summary[name]["queries"] = len(raw_rows)
        summary[name]["p95_latency_ms"] = round(sorted(latency_samples[name])[
            min(len(latency_samples[name]) - 1, math.ceil(0.95 * len(latency_samples[name])) - 1)
        ], 4)

    no_answer_queries = _synthetic_no_answer_queries()
    controls = []
    for query in no_answer_queries:
        lex = bm25.search(query, 30)
        sem = dense.search(query, 30)
        fused = rrf(lex, sem, k=60, limit=10)
        controls.append({
            "query": query,
            "kind": "synthetic_no_answer",
            "expected": "abstain",
            "methods": {
                name: {"top_score": rows[0][1] if rows else 0.0,
                       "raw_candidate_positive": bool(rows and rows[0][1] > 0),
                       "resolver_would_answer": gate,
                       "abstained": not gate}
                for name, rows, gate in (
                    ("bm25", lex, bool(lex and lex[0][1] > 0)),
                    ("dense", sem, bool(lex and lex[0][1] > 0)),
                    ("hybrid_rrf", fused, bool(lex and lex[0][1] > 0)),
                )
            },
        })

    false_accepts = [
        {"query": row["query"], "method": name}
        for row in controls for name, result in row["methods"].items()
        if result["resolver_would_answer"]
    ]

    raw_corpus = CORPUS_PATH.read_bytes()
    query_bytes = json.dumps(GOLD_SET, ensure_ascii=False, separators=(",", ":")).encode()
    return {
        "study_type": "exploratory_paired_local_comparison",
        "protocol": PROTOCOL,
        "input_sha256": {
            "corpus": hashlib.sha256(raw_corpus).hexdigest(),
            "gold_queries": hashlib.sha256(query_bytes).hexdigest(),
        },
        "environment": {"python": __import__("platform").python_version(),
                        "platform": __import__("platform").platform()},
        "fold": {
            "label": "c25-v1",
            "dev_records": len(folds["dev"].papers),
            "test_records": len(test.papers),
            "dev_documents": len(folds["dev"].pmids),
            "test_documents": len(test.pmids),
            "indexed_unique_documents": len(papers),
            "shared_documents": 0,
            "queries": len(raw_rows),
            "dropped_queries_without_relevant_test_document": len(GOLD_SET) - len(query_specs),
        },
        "summary": summary,
        "paired_bootstrap_deltas_vs_bm25": _paired_bootstrap_deltas(raw_rows),
        "no_answer_controls": controls,
        "fail_closed_control_gate": {
            "status": "PASS" if not false_accepts else "FAIL",
            "false_accepts": false_accepts,
            "count": len(false_accepts),
            "required": "all retrieval methods must abstain on every synthetic no-answer control",
        },
        "per_query": raw_rows,
        "interpretation": "Exploratory only. Existing query labels and phrasing lack independent adjudication and unseen-source proof; synthetic controls are not user evidence.",
    }


def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument("--output", type=Path, required=True)
    parser.add_argument("--repetitions", type=int, default=5)
    args = parser.parse_args()
    result = evaluate(repetitions=args.repetitions)
    rendered = json.dumps(result, indent=2, sort_keys=True, allow_nan=False) + "\n"
    args.output.parent.mkdir(parents=True, exist_ok=True)
    args.output.write_text(rendered)
    print(json.dumps({"output": str(args.output), "queries": result["fold"]["queries"],
                      "summary": result["summary"],
                      "no_answer_controls": result["no_answer_controls"]}, indent=2, sort_keys=True))


if __name__ == "__main__":
    main()
