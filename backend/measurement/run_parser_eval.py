"""C31: document parsing and grounded retrieval, measured.

Compares Trace's shipped extraction shape against layout-aware PDF parsing on
permitted PMC Open Access documents, then measures what the difference does to
retrieval and to citation provenance.

    python -m backend.measurement.run_parser_eval --docs <dir> --manifest <json>

What is held constant so the parser is the only variable:
  * the same 12 documents for every approach;
  * the same fixed-size chunker (`layout_parse.window`);
  * the same BM25 retriever, implemented here, over one pooled index per
    approach containing every chunk from all 12 documents, so a query has to
    beat 11 other papers and not just rank within its own.

Ground truth is the publisher's JATS XML (`jats_gold`). A gold fact is only
scored if an independent oracle - PyMuPDF's raw per-page text layer, which no
parser under test is allowed to influence - can locate it on some page of the
PDF. Facts the oracle cannot find are dropped and counted, so the numbers
measure parsing rather than encoding drift between the XML and the PDF.
"""
from __future__ import annotations

import argparse
import json
import math
import os
import re
import time
from collections import Counter, defaultdict
from pathlib import Path

from backend.app.corpus.jats_gold import GoldItem, load_gold, norm
from backend.app.corpus.layout_parse import (
    DocChunk, parse_abstract_only, parse_layout_pdfplumber, parse_layout_pymupdf,
    parse_naive_pdf,
)

RESULTS_DIR = Path(__file__).resolve().parent / "results"
_TOKEN = re.compile(r"[a-z0-9]+")
_ALNUM = re.compile(r"[^a-z0-9]+")


def loose(text: str) -> str:
    """Letters and digits only, lowercased.

    Whether a fact survived parsing must not turn on whether a parser emitted a
    soft hyphen, a ligature or a line break where another emitted a space.
    Matching on the whitespace-preserving form scored pdfplumber 1/40 and
    PyMuPDF 14/40 on the same page of the same document purely on punctuation
    fidelity, which measures the oracle's own text layer rather than the
    parser. Strings are only matched at 12 characters or more, where an
    accidental alphanumeric collision is not a practical risk.
    """
    return _ALNUM.sub("", (text or "").lower())


def tokenize(text: str) -> list[str]:
    return _TOKEN.findall(text.lower())


class BM25:
    """Standard BM25 over chunk text. Local, deterministic, no API cost.

    Trace retrieves through Snowflake Cortex Search, which is hybrid lexical +
    vector. Building a Cortex Search service over PDF chunks would mutate a
    shared Snowflake account and bill it, so the comparison runs on a local
    lexical retriever instead. Every approach is scored on the same retriever,
    so the ranking between parsers is a like-for-like comparison; the absolute
    numbers are not a Cortex Search measurement and are not reported as one.
    """

    def __init__(self, chunks: list[DocChunk], k1: float = 1.5, b: float = 0.75):
        self.chunks = chunks
        self.k1, self.b = k1, b
        self.docs = [tokenize(c.text) for c in chunks]
        self.lens = [len(d) for d in self.docs]
        self.avglen = (sum(self.lens) / len(self.lens)) if self.lens else 0.0
        self.tf: list[Counter] = [Counter(d) for d in self.docs]
        df: Counter = Counter()
        for d in self.docs:
            df.update(set(d))
        n = len(self.docs)
        self.idf = {t: math.log(1 + (n - c + 0.5) / (c + 0.5)) for t, c in df.items()}
        self.postings: dict[str, list[int]] = defaultdict(list)
        for i, d in enumerate(self.docs):
            for t in set(d):
                self.postings[t].append(i)

    def search(self, query: str, top_k: int = 10) -> list[tuple[int, float]]:
        q = tokenize(query)
        scores: dict[int, float] = defaultdict(float)
        for t in q:
            idf = self.idf.get(t)
            if idf is None:
                continue
            for i in self.postings[t]:
                f = self.tf[i][t]
                denom = f + self.k1 * (1 - self.b + self.b * self.lens[i] / (self.avglen or 1))
                scores[i] += idf * (f * (self.k1 + 1)) / (denom or 1)
        ranked = sorted(scores.items(), key=lambda kv: (-kv[1], kv[0]))
        return ranked[:top_k]


def page_oracle(pdf_path: Path) -> dict[int, str]:
    """Normalized text of each page, straight from the PDF text layer.

    Independent of every parser under test: it is used only to decide which
    page a gold fact really sits on, and to drop facts the PDF does not contain.
    """
    import pymupdf

    out: dict[int, str] = {}
    with pymupdf.open(pdf_path) as doc:
        for pno, page in enumerate(doc, start=1):
            out[pno] = loose(page.get_text("text"))
    return out


def front_matter(xml_path: Path) -> tuple[str, str]:
    """Title and abstract, the two fields Trace's PubMed ingestion keeps."""
    import xml.etree.ElementTree as ET

    root = ET.parse(xml_path).getroot()
    for el in root.iter():
        if isinstance(el.tag, str) and "}" in el.tag:
            el.tag = el.tag.split("}", 1)[1]
    title_el = root.find(".//article-title")
    title = norm("".join(title_el.itertext())) if title_el is not None else ""
    abs_el = root.find(".//abstract")
    abstract = norm("".join(abs_el.itertext())) if abs_el is not None else ""
    return title, abstract


def locatable(gold: list[GoldItem], pages: dict[int, str]) -> list[tuple[GoldItem, set[int]]]:
    """Keep gold facts the PDF actually contains; report where they live."""
    kept: list[tuple[GoldItem, set[int]]] = []
    for item in gold:
        needle = loose(item.text)
        if len(needle) < 12:
            continue
        hits = {p for p, text in pages.items() if needle in text}
        if hits:
            kept.append((item, hits))
    return kept


def build_query(item: GoldItem) -> tuple[str, str] | None:
    """Split a gold fact into a query half and a held-out answer half.

    The answer never appears in the query, so a hit means the retriever found
    the passage rather than matching the answer string back to itself.
    """
    words = norm(item.text).split()
    if len(words) < 8:
        return None
    cut = max(4, len(words) // 2)
    query, answer = " ".join(words[:cut]), " ".join(words[cut:])
    if len(answer) < 12:
        return None
    return query, answer


def evaluate(docs_dir: Path, manifest_path: Path, out_path: Path) -> dict:
    manifest = json.loads(manifest_path.read_text())
    approaches = {
        "abstract_only": None,
        "naive_pdf": parse_naive_pdf,
        "layout_pymupdf": parse_layout_pymupdf,
        "layout_pdfplumber": parse_layout_pdfplumber,
    }

    chunks_by_approach: dict[str, list[DocChunk]] = {a: [] for a in approaches}
    parse_seconds: dict[str, float] = {a: 0.0 for a in approaches}
    parse_cpu_seconds: dict[str, float] = {a: 0.0 for a in approaches}
    per_doc_seconds: dict[str, list[float]] = {a: [] for a in approaches}
    gold_by_doc: dict[str, list[tuple[GoldItem, set[int]]]] = {}
    dropped = Counter()
    gold_raw = Counter()

    for row in manifest:
        doc_id = row["pmcid"]
        pdf = docs_dir / f"{doc_id}.pdf"
        xml = docs_dir / f"{doc_id}.xml"
        pages = page_oracle(pdf)
        gold = load_gold(xml, doc_id)
        gold_raw.update(i.kind for i in gold)
        kept = locatable(gold, pages)
        gold_by_doc[doc_id] = kept
        for kind in ("table_cell", "caption", "body_sentence"):
            n_raw = sum(1 for i in gold if i.kind == kind)
            n_kept = sum(1 for i, _ in kept if i.kind == kind)
            dropped[f"{kind}_dropped"] += n_raw - n_kept

        title, abstract = front_matter(xml)
        for name, fn in approaches.items():
            t0, c0 = time.perf_counter(), time.process_time()
            if name == "abstract_only":
                produced = parse_abstract_only(doc_id, title, abstract)
            else:
                produced = fn(pdf, doc_id)
            dt = time.perf_counter() - t0
            parse_cpu_seconds[name] += time.process_time() - c0
            parse_seconds[name] += dt
            per_doc_seconds[name].append(dt)
            chunks_by_approach[name].extend(produced)

    # -- extraction coverage + provenance correctness ------------------------
    extraction: dict[str, dict] = {}
    for name, chunks in chunks_by_approach.items():
        by_doc: dict[str, list[DocChunk]] = defaultdict(list)
        for c in chunks:
            by_doc[c.doc_id].append(c)
        found = Counter()
        total = Counter()
        page_right = Counter()
        page_scored = Counter()
        for doc_id, kept in gold_by_doc.items():
            haystack = [(c, loose(c.text)) for c in by_doc.get(doc_id, [])]
            for item, true_pages in kept:
                total[item.kind] += 1
                needle = loose(item.text)
                hit = next((c for c, text in haystack if needle in text), None)
                if hit is None:
                    continue
                found[item.kind] += 1
                if hit.page > 0:
                    page_scored[item.kind] += 1
                    if hit.page in true_pages:
                        page_right[item.kind] += 1
        extraction[name] = {
            "chunks": len(chunks),
            "chars": sum(len(c.text) for c in chunks),
            "kinds": dict(Counter(c.kind for c in chunks)),
            "recall": {
                k: {"found": found[k], "of": total[k],
                    "pct": round(100 * found[k] / total[k], 1) if total[k] else None}
                for k in ("table_cell", "caption", "body_sentence")
            },
            "page_provenance": {
                k: {"correct": page_right[k], "attributed": page_scored[k],
                    "pct": round(100 * page_right[k] / page_scored[k], 1) if page_scored[k] else None}
                for k in ("table_cell", "caption", "body_sentence")
            },
        }

    # -- retrieval + citation quality ---------------------------------------
    queries: list[tuple[GoldItem, str, str, set[int]]] = []
    for doc_id, kept in gold_by_doc.items():
        for item, true_pages in kept:
            built = build_query(item)
            if built:
                queries.append((item, built[0], built[1], true_pages))

    retrieval: dict[str, dict] = {}
    for name, chunks in chunks_by_approach.items():
        t0 = time.perf_counter()
        index = BM25(chunks)
        index_seconds = time.perf_counter() - t0
        norm_chunks = [loose(c.text) for c in chunks]

        hits1 = Counter(); hits5 = Counter(); rr = defaultdict(float); n = Counter()
        cite_ok = Counter(); cite_n = Counter(); doc_ok = Counter()
        t0 = time.perf_counter()
        for item, query, answer, true_pages in queries:
            n[item.kind] += 1
            ranked = index.search(query, top_k=10)
            needle = loose(answer)
            rank = None
            for pos, (idx, _score) in enumerate(ranked, start=1):
                if needle in norm_chunks[idx]:
                    rank = pos
                    break
            if rank is None:
                continue
            if rank == 1:
                hits1[item.kind] += 1
            if rank <= 5:
                hits5[item.kind] += 1
            rr[item.kind] += 1.0 / rank
            top = chunks[ranked[rank - 1][0]]
            if top.doc_id == item.doc_id:
                doc_ok[item.kind] += 1
            if top.page > 0:
                cite_n[item.kind] += 1
                if top.page in true_pages:
                    cite_ok[item.kind] += 1
        query_seconds = time.perf_counter() - t0

        retrieval[name] = {
            "index_build_seconds": round(index_seconds, 3),
            "queries": sum(n.values()),
            "total_query_seconds": round(query_seconds, 3),
            "ms_per_query": round(1000 * query_seconds / max(sum(n.values()), 1), 3),
            "by_kind": {
                k: {
                    "n": n[k],
                    "hit@1_pct": round(100 * hits1[k] / n[k], 1) if n[k] else None,
                    "recall@5_pct": round(100 * hits5[k] / n[k], 1) if n[k] else None,
                    "mrr@10": round(rr[k] / n[k], 3) if n[k] else None,
                    "correct_document_pct": round(100 * doc_ok[k] / n[k], 1) if n[k] else None,
                    "citation_page_correct_pct": round(100 * cite_ok[k] / cite_n[k], 1) if cite_n[k] else None,
                    "citation_page_attributed": cite_n[k],
                }
                for k in ("table_cell", "caption", "body_sentence")
            },
        }

    result = {
        "documents": len(manifest),
        "loadavg_at_run": os.getloadavg(),
        "gold_raw": dict(gold_raw),
        "gold_dropped_not_in_pdf": {k: v for k, v in dropped.items() if k.endswith("_dropped")},
        "gold_scored": {
            k: sum(1 for kept in gold_by_doc.values() for i, _ in kept if i.kind == k)
            for k in ("table_cell", "caption", "body_sentence")
        },
        "held_out_queries": len(queries),
        "cost": {
            name: {
                "api_spend_usd": 0.0,
                "total_parse_wall_seconds": round(parse_seconds[name], 3),
                "wall_seconds_per_document": round(parse_seconds[name] / max(len(manifest), 1), 3),
                "total_parse_cpu_seconds": round(parse_cpu_seconds[name], 3),
                "cpu_seconds_per_document": round(parse_cpu_seconds[name] / max(len(manifest), 1), 3),
                "max_document_wall_seconds": round(max(per_doc_seconds[name]), 3),
            }
            for name in approaches
        },
        "extraction": extraction,
        "retrieval": retrieval,
    }
    out_path.parent.mkdir(parents=True, exist_ok=True)
    out_path.write_text(json.dumps(result, indent=2))
    return result


def main() -> None:
    ap = argparse.ArgumentParser()
    ap.add_argument("--docs", required=True, type=Path)
    ap.add_argument("--manifest", required=True, type=Path)
    ap.add_argument("--out", type=Path, default=RESULTS_DIR / "parser_eval.json")
    args = ap.parse_args()
    result = evaluate(args.docs, args.manifest, args.out)
    print(json.dumps(result, indent=2))


if __name__ == "__main__":
    main()
