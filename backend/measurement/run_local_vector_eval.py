"""Run a small, deterministic evaluation of the credential-free vector store.

This is a reproducibility check for the local adapter, not a comparison with
Snowflake Cortex Search and not a claim about embedding or ANN quality.
"""
from __future__ import annotations

import argparse
import json
from pathlib import Path

from backend.vectorstore import LocalVectorRetriever


CASES = (
    {"query": "scalp angiosarcoma", "condition": "Scalp angiosarcoma"},
    {"query": "semantic variant aphasia", "condition": "Primary progressive aphasia semantic variant"},
    {"query": "mitochondrial disease", "condition": "Mitochondrial disease"},
)


def evaluate(retriever: LocalVectorRetriever, top_k: int = 5) -> dict[str, object]:
    rows = []
    for case in CASES:
        results = retriever.search(case["query"], top_k=top_k)
        hits = sum(result.paper.condition == case["condition"] for result in results)
        rows.append({
            "query": case["query"],
            "expected_condition": case["condition"],
            "top_k": top_k,
            "hits": hits,
            "hit_at_k": hits > 0,
            "returned_pmids": [result.paper.pmid for result in results],
        })
    return {
        "backend": "LocalVectorRetriever",
        "embedding": "deterministic hashed word and bigram features",
        "top_k": top_k,
        "cases": rows,
        "hit_rate_at_k": sum(row["hit_at_k"] for row in rows) / len(rows),
        "limitation": "fixed public fixture; this does not establish production retrieval quality",
    }


def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument("--db", type=Path, default=None)
    parser.add_argument("--output", type=Path, default=None)
    parser.add_argument("--top-k", type=int, default=5)
    args = parser.parse_args()
    result = evaluate(LocalVectorRetriever(db_path=args.db), top_k=args.top_k)
    rendered = json.dumps(result, indent=2, sort_keys=True) + "\n"
    if args.output:
        args.output.parent.mkdir(parents=True, exist_ok=True)
        args.output.write_text(rendered)
    else:
        print(rendered, end="")


if __name__ == "__main__":
    main()
