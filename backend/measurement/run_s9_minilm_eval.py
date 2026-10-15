"""Paired S9 comparison with pinned cached MiniLM plus the v2 baselines.

Offline only. Requires the cached all-MiniLM-L6-v2 checkpoint and an explicit
path to the pure-NumPy inference module from noisy-rag-bench.
"""
from __future__ import annotations

import hashlib
import importlib.util
import json
import math
import os
import time
from pathlib import Path

import numpy as np

from backend.contracts.models import Paper
from backend.measurement.run_gate import GOLD_SET
from backend.measurement.run_retrieval_eval import make_folds
from backend.measurement.run_s9_eval import (
    BM25Index, DenseIndex, _metrics, _paired_bootstrap_deltas,
    _synthetic_no_answer_queries, rrf,
)

MODEL_ID = "sentence-transformers/all-MiniLM-L6-v2"
MODEL_REVISION = "1110a243fdf4706b3f48f1d95db1a4f5529b4d41"
MODEL_FILE_SHA256 = "53aa51172d142c89d9012cce15ae4d6cc0ca6895895114379cacb4fab128d9db"
PROTOCOL = {
    "protocol_id": "s9-local-paired-v3.2-minilm",
    "split": "existing PMID-level c25-v1 test fold; same as v2",
    "queries": "exact existing GOLD_SET query-condition pairs with relevance in test fold; same as v2",
    "arms": {
        "bm25": "v2 local BM25, title+abstract, k1=1.5, b=0.75",
        "feature_hash_dense": "v2 deterministic 256-dimensional word+bigram feature hashing, no rarity boost",
        "minilm_dense": f"{MODEL_ID} revision {MODEL_REVISION}; mean-pooled normalized 384-dimensional vectors",
        "bm25_minilm_rrf": "RRF of BM25 and MiniLM top-30, k=60",
    },
    "depths": {"candidate": 30, "final": 10},
    "metrics": ["recall_at_10", "mrr_at_10", "ndcg_at_10", "precision_at_5"],
    "no_answer_controls": "same three seeded synthetic OOV strings as v2; descriptive only",
    "latency": "local CPU inference/search, descriptive; model encoding separate from query latency",
    "limitations": ["same non-independent existing condition labels and corpus-theme query wording as v2",
                    "MiniLM inference implementation is pure NumPy; no production ANN or deployment claim",
                    "synthetic OOV controls do not establish user-facing answerability"]
}


class MiniLMIndex:
    def __init__(self, papers: list[Paper], model):
        self.papers = papers
        self.vectors = model.encode([f"{p.title} {p.abstract}" for p in papers])

    def search(self, query: str, limit: int):
        q = model_encode(query)
        scores = self.vectors @ q
        order = sorted(range(len(self.papers)), key=lambda i: (-float(scores[i]), self.papers[i].pmid))[:limit]
        return [(self.papers[i], float(scores[i])) for i in order]


_MODEL = None
def model_encode(query: str):
    return _MODEL.encode([query])[0]


def evaluate(repetitions: int = 5, *, minilm_code: Path):
    global _MODEL
    if os.environ.get("HF_HUB_OFFLINE") != "1":
        raise RuntimeError("set HF_HUB_OFFLINE=1 to enforce offline evaluation")
    minilm_code = minilm_code.resolve(strict=True)
    spec = importlib.util.spec_from_file_location("s9_minilm", minilm_code)
    if spec is None or spec.loader is None:
        raise ValueError("minilm-code must name a Python module")
    minilm = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(minilm)
    model_file = Path(os.environ["HF_HOME"]) / "hub/models--sentence-transformers--all-MiniLM-L6-v2/snapshots" / MODEL_REVISION / "model.safetensors"
    tokenizer_file = model_file.with_name("tokenizer.json")
    for path in (model_file, tokenizer_file):
        if not path.is_file():
            raise FileNotFoundError(path)
    actual_model_sha = hashlib.sha256(model_file.read_bytes()).hexdigest()
    if actual_model_sha != MODEL_FILE_SHA256:
        raise AssertionError("cached model checksum differs from pinned checkpoint")
    # The inference module selects the first cached snapshot; require it to be pinned.
    snapshots = list(model_file.parent.parent.glob("*/model.safetensors"))
    if snapshots != [model_file]:
        raise RuntimeError("HF_HOME must contain only the pinned MiniLM snapshot")
    _MODEL = minilm.MiniLM()
    corpus = json.loads((Path(__file__).resolve().parents[1] / "data/corpus.json").read_text())
    from backend.measurement.run_retrieval_eval import _load_corpus
    folds = make_folds(_load_corpus())
    test = folds["test"]
    seen, papers = set(), []
    relevant_by_condition = {}
    for p in test.papers:
        relevant_by_condition.setdefault(p.condition, set()).add(p.pmid)
        if p.pmid not in seen:
            papers.append(p); seen.add(p.pmid)
    queries = [(q, c) for q, c in GOLD_SET if c in relevant_by_condition]
    bm25, hashed, mini = BM25Index(papers), DenseIndex(papers), MiniLMIndex(papers, _MODEL)
    query_vectors = {q: model_encode(q) for q, _ in queries}
    def minilm_search(q, limit):
        scores = mini.vectors @ query_vectors[q]
        order = sorted(range(len(papers)), key=lambda i: (-float(scores[i]), papers[i].pmid))[:limit]
        return [(papers[i], float(scores[i])) for i in order]

    names = ("bm25", "feature_hash_dense", "minilm_dense", "bm25_minilm_rrf")
    raw, lat = [], {n: [] for n in names}
    for q, c in queries:
        qv = query_vectors[q]
        for rep in range(repetitions):
            t = time.perf_counter(); lex = bm25.search(q, 30); lat["bm25"].append((time.perf_counter()-t)*1000)
            t = time.perf_counter(); fh = hashed.search(q, 30); lat["feature_hash_dense"].append((time.perf_counter()-t)*1000)
            t = time.perf_counter(); sem = minilm_search(q, 30); lat["minilm_dense"].append((time.perf_counter()-t)*1000)
            t = time.perf_counter(); fused = rrf(lex, sem, k=60, limit=10); lat["bm25_minilm_rrf"].append((time.perf_counter()-t)*1000)
            if rep == repetitions - 1:
                rel = relevant_by_condition[c]
                arms = {"bm25": lex[:10], "feature_hash_dense": fh[:10], "minilm_dense": sem[:10], "bm25_minilm_rrf": fused}
                raw.append({"query": q, "condition_label": c, "relevant_documents": sorted(rel), "methods": {
                    n: {"ranked_pmids": [p.pmid for p, _ in rows], "top_score": rows[0][1] if rows else 0.0,
                        "metrics": _metrics(rows, rel)} for n, rows in arms.items()}})
    summary = {}
    for n in names:
        summary[n] = {m: round(sum(r["methods"][n]["metrics"][m] for r in raw)/len(raw), 6)
                      for m in PROTOCOL["metrics"]}
        vals = sorted(lat[n]); summary[n]["p95_latency_ms"] = round(vals[math.ceil(.95*len(vals))-1], 4)
    controls = []
    for q in _synthetic_no_answer_queries():
        query_vectors[q] = model_encode(q)
        lex, fh = bm25.search(q, 30), hashed.search(q, 30)
        sem = minilm_search(q, 30)
        fused = rrf(lex, sem, k=60, limit=10)
        controls.append({"query": q, "kind": "synthetic_no_answer", "expected": "abstain",
                         "methods": {n: {"top_score": rows[0][1] if rows else 0.0,
                                         "raw_candidate_positive": bool(rows and rows[0][1] > 0)}
                                     for n, rows in (("bm25", lex), ("feature_hash_dense", fh),
                                                     ("minilm_dense", sem), ("bm25_minilm_rrf", fused))}})
    corpus_path = Path(__file__).resolve().parents[1] / "data/corpus.json"
    gold_bytes = json.dumps(GOLD_SET, ensure_ascii=False, separators=(",", ":")).encode()
    bootstrap_rows = []
    for row in raw:
        bootstrap_rows.append({"condition_label": row["condition_label"], "methods": {
            "bm25": row["methods"]["bm25"],
            "dense": row["methods"]["minilm_dense"],
            "hybrid_rrf": row["methods"]["bm25_minilm_rrf"],
        }})
    return {"study_type": "exploratory_paired_local_comparison", "protocol": PROTOCOL,
            "model": {"id": MODEL_ID, "revision": MODEL_REVISION, "model_file_sha256": actual_model_sha,
                      "tokenizer_sha256": hashlib.sha256(tokenizer_file.read_bytes()).hexdigest(),
                      "inference_code_sha256": hashlib.sha256(minilm_code.read_bytes()).hexdigest()},
            "inputs": {"corpus_sha256": hashlib.sha256(corpus_path.read_bytes()).hexdigest(),
                       "gold_queries_sha256": hashlib.sha256(gold_bytes).hexdigest()},
            "fold": {"label": "c25-v1", "dev_documents": len(folds["dev"].pmids),
                     "test_documents": len(test.pmids), "indexed_unique_documents": len(papers),
                     "shared_documents": len(folds["dev"].pmids & test.pmids), "queries": len(raw)},
            "summary": summary, "paired_bootstrap_deltas_vs_bm25": _paired_bootstrap_deltas(bootstrap_rows),
            "no_answer_controls": controls, "per_query": raw,
            "interpretation": "Exploratory only; same limitations as v2. No abstention behavior was inferred from similarity scores."}


def main():
    import argparse
    parser = argparse.ArgumentParser(); parser.add_argument("--output", type=Path, required=True)
    parser.add_argument("--minilm-code", type=Path, required=True,
                        help="Path to noisy-rag-bench's pure-NumPy minilm.py")
    parser.add_argument("--repetitions", type=int, default=5); args = parser.parse_args()
    result = evaluate(args.repetitions, minilm_code=args.minilm_code)
    args.output.parent.mkdir(parents=True, exist_ok=True)
    args.output.write_text(json.dumps(result, indent=2, sort_keys=True, allow_nan=False) + "\n")
    print(json.dumps({"output": str(args.output), "summary": result["summary"], "queries": result["fold"]["queries"]}, indent=2))

if __name__ == "__main__": main()
