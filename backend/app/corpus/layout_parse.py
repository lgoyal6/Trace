"""Layout-aware PDF parsing with per-page provenance (C31).

Trace's shipped extraction (`backend/app/corpus/fetch_pubmed.fetch_abstracts`)
reads one PubMed XML abstract per paper. It has no page concept because an
abstract record has no pages. Retrieval over full documents needs one, so
every chunk produced here carries `doc_id` + `page`, and that pair is the
provenance that must survive into a retrieval result and into a citation.

Four ingestion approaches are implemented so they can be measured against each
other on the same documents:

    abstract_only  -- the shape Trace ships today: title + abstract, one chunk,
                      page provenance unavailable (recorded as page 0).
    naive_pdf      -- PyMuPDF raw `get_text("text")` per page, fixed-size
                      windows. Layout-blind, and it still knows what page it
                      read, so it is a real baseline rather than a straw man.
                      On this corpus its reading order is good: publisher PDFs
                      carry a sanely ordered text layer, and it recovers body
                      prose at least as well as the layout-aware paths. Where
                      it loses is that it cannot tell a caption from the
                      paragraph next to it.
    layout_pymupdf -- PyMuPDF text blocks re-ordered by detected column, plus
                      `find_tables()` for table structure and an explicit
                      caption pass.
    layout_pdfplumber -- pdfplumber word boxes assigned to a column, merged
                      into paragraph blocks on vertical gap, plus
                      `extract_tables()`.

`kind` distinguishes narrative body text from table and caption material,
because that is the split the C31 comparison turns on.
"""
from __future__ import annotations

import hashlib
import re
from dataclasses import dataclass, field
from pathlib import Path

#: A chunk kind. "table" and "caption" only ever come from a layout-aware
#: parser; the naive parsers cannot tell them apart from body prose.
ChunkKind = str

#: Words that, immediately after a "Table 3"/"Figure 2" marker, mean the block is
#: a sentence about the exhibit rather than its caption. Measured against this
#: corpus: body prose reads "Table 1 summarizes...", "Table 2 gives an
#: overview...", "Table 3 shows the relationship...", while real captions read
#: "Table 1 Clinical phenotypes and baseline characteristics". Punctuation after
#: the number is NOT a usable discriminator; several journals in the sample set
#: print captions with no separator at all.
_CAPTION_STOPWORDS = frozenset("""
summarizes summarised summarizes summarised summarize summarise shows show
gives give lists list presents present displays display illustrates illustrate
describes describe contains contain reports report provides provide indicates
indicate compares compare details detail depicts depict outlines outline
demonstrates demonstrate highlights highlight includes include shown listed
also and or in for of to which that was were is are as we our this these
""".split())

_CAPTION_MARKER_RE = re.compile(
    r"^\s*(fig(?:ure)?|table|scheme|chart)\s*\.?\s*(\d+|[ivxlcIVXLC]+)\s*([.:)\u2013\u2014|-]?)\s*(\S*)",
    re.IGNORECASE,
)


def is_caption(text: str) -> bool:
    """True when a block reads as a figure/table caption rather than prose."""
    m = _CAPTION_MARKER_RE.match(text or "")
    if not m:
        return False
    separator, next_word = m.group(3), m.group(4)
    if separator:
        return True
    return next_word.strip(".,;:").lower() not in _CAPTION_STOPWORDS


@dataclass(frozen=True)
class DocChunk:
    """One retrievable unit of a document, carrying its own provenance.

    `doc_id` + `page` is the provenance pair. `page` is 1-based as printed by
    a PDF reader; 0 means "this approach cannot attribute a page", which is
    true of abstract-only ingestion and is recorded rather than faked.
    """

    doc_id: str
    page: int
    block_index: int
    kind: ChunkKind
    text: str
    parser: str
    bbox: tuple[float, float, float, float] | None = None
    meta: dict = field(default_factory=dict)

    @property
    def citation(self) -> str:
        """Human-facing source label. This is the string a summary cites."""
        if self.page <= 0:
            return f"{self.doc_id} (no page attribution)"
        return f"{self.doc_id} p.{self.page}"


# ---------------------------------------------------------------------------
# chunking helper
# ---------------------------------------------------------------------------

def window(text: str, size: int = 1200, overlap: int = 150) -> list[str]:
    """Fixed-size character windows. Deliberately dumb and identical across
    parsers so the comparison varies the parser, not the chunker.
    """
    text = text.strip()
    if not text:
        return []
    if len(text) <= size:
        return [text]
    out: list[str] = []
    step = size - overlap
    for start in range(0, len(text), step):
        piece = text[start : start + size].strip()
        if piece:
            out.append(piece)
        if start + size >= len(text):
            break
    return out


def _column_sort(boxes: list[tuple[float, float, float, float, str]], page_width: float):
    """Order blocks by column, then down the column.

    A page is treated as two-column when at least a quarter of the blocks sit
    entirely left of the mid-line and at least a quarter sit entirely right of
    it. Single-column pages fall through to plain top-to-bottom order, so the
    heuristic cannot damage a page it does not apply to.
    """
    if not boxes:
        return []
    mid = page_width / 2.0
    left = [b for b in boxes if b[2] <= mid * 1.05]
    right = [b for b in boxes if b[0] >= mid * 0.95]
    n = len(boxes)
    two_col = len(left) >= n * 0.25 and len(right) >= n * 0.25
    if not two_col:
        return sorted(boxes, key=lambda b: (round(b[1], 1), b[0]))
    spanning = [b for b in boxes if b not in left and b not in right]
    return (
        sorted(spanning, key=lambda b: (round(b[1], 1), b[0]))
        + sorted(left, key=lambda b: (round(b[1], 1), b[0]))
        + sorted(right, key=lambda b: (round(b[1], 1), b[0]))
    )


#: A grid must be at least this wide and this tall to count as a table.
_MIN_TABLE_ROWS = 2
_MIN_TABLE_COLS = 2
_MIN_TABLE_CELLS = 4


def _table_to_text(rows) -> str:
    """Render an extracted grid, or return "" when the grid is not table-shaped.

    Both table finders report ruled prose boxes as tables: the boxed abstract on
    page 1 of PMC10016410 came back from PyMuPDF as a 30-row, 3-column grid in
    which only the middle column ever held text. Row and cell counts alone let
    that through, so a grid must also have at least two columns that are
    populated on at least two rows.
    """
    lines = []
    widths = []
    filled = 0
    col_counts: dict[int, int] = {}
    for row in rows or []:
        cells = [("" if c is None else str(c)).replace("\n", " ").strip() for c in row]
        if not any(cells):
            continue
        widths.append(len(cells))
        filled += sum(1 for c in cells if c)
        for i, c in enumerate(cells):
            if c:
                col_counts[i] = col_counts.get(i, 0) + 1
        lines.append(" | ".join(cells))
    populated_columns = sum(1 for n in col_counts.values() if n >= _MIN_TABLE_ROWS)
    if len(lines) < _MIN_TABLE_ROWS:
        return ""
    if max(widths, default=0) < _MIN_TABLE_COLS:
        return ""
    if filled < _MIN_TABLE_CELLS:
        return ""
    if populated_columns < _MIN_TABLE_COLS:
        return ""
    return "\n".join(lines)


# ---------------------------------------------------------------------------
# approach 1: what Trace ships today
# ---------------------------------------------------------------------------

def parse_abstract_only(doc_id: str, title: str, abstract: str) -> list[DocChunk]:
    """Trace's current ingestion shape: one title+abstract chunk, no page.

    Page provenance is genuinely unavailable here, so it is recorded as 0
    rather than invented.
    """
    text = f"{title}\n\n{abstract}".strip()
    if not text:
        return []
    return [
        DocChunk(doc_id=doc_id, page=0, block_index=i, kind="abstract",
                 text=piece, parser="abstract_only")
        for i, piece in enumerate(window(text))
    ]


def parse_layout_llamaparse(
    pdf_path: Path, doc_id: str, *, client=None
) -> list[DocChunk]:
    """Parse a PDF with the named LlamaParse service and retain its page metadata.

    The dependency and credential are loaded only on this path. A client can be
    injected for contract tests, but the default is the real `llama_parse.LlamaParse`
    SDK. Current clients return a JobResult from ``parse``; its page documents
    and job id are authoritative. Injected and older clients that expose only
    ``load_data`` remain supported. If the service omits a page number, page
    remains 0 rather than inventing provenance.
    """
    pdf_path = Path(pdf_path)
    if not pdf_path.is_file() or pdf_path.suffix.lower() != ".pdf":
        raise ValueError("LlamaParse input must be an existing PDF file")
    if client is None:
        import os

        from llama_parse import LlamaParse

        api_key = os.environ.get("LLAMA_CLOUD_API_KEY")
        if not api_key:
            raise RuntimeError("LLAMA_CLOUD_API_KEY is required for LlamaParse")
        client = LlamaParse(
            api_key=api_key,
            result_type="markdown",
            split_by_page=True,
            verbose=False,
        )
    source_sha256 = hashlib.sha256(pdf_path.read_bytes()).hexdigest()
    parse = getattr(client, "parse", None)
    if callable(parse):
        parsed = parse(str(pdf_path))
        job_results = parsed if isinstance(parsed, (list, tuple)) else [parsed]
        if not all(
            callable(getattr(result, "get_markdown_documents", None))
            for result in job_results
        ):
            raise RuntimeError("LlamaParse parse() returned no JobResult objects")
        documents_with_jobs = [
            (document, getattr(result, "job_id", None))
            for result in job_results
            for document in result.get_markdown_documents(split_by_page=True)
        ]
    else:
        documents_with_jobs = [
            (document, None) for document in client.load_data(str(pdf_path))
        ]
    chunks: list[DocChunk] = []
    for document_index, (document, job_id) in enumerate(documents_with_jobs):
        metadata = dict(getattr(document, "metadata", {}) or {})
        raw_page = metadata.get("page_number", metadata.get("page_label", 0))
        try:
            page = int(raw_page)
        except (TypeError, ValueError):
            page = 0
        text = getattr(document, "text", None)
        if text is None and hasattr(document, "get_content"):
            text = document.get_content()
        for block_index, piece in enumerate(window(text or "")):
            chunks.append(DocChunk(
                doc_id=doc_id,
                page=page,
                block_index=block_index,
                kind="body",
                text=piece,
                parser="llamaparse",
                meta={
                    "source_sha256": source_sha256,
                    "parser_document_index": document_index,
                    "llamaparse_job_id": job_id or metadata.get("job_id"),
                },
            ))
    return chunks


# ---------------------------------------------------------------------------
# approach 2: naive full-text PDF extraction
# ---------------------------------------------------------------------------

def parse_naive_pdf(pdf_path: Path, doc_id: str) -> list[DocChunk]:
    """Raw text layer per page, windowed. No column handling, no table or
    caption awareness. Pages are still attributable because the extraction is
    done page by page, so this baseline is not handicapped on provenance.
    """
    import pymupdf

    chunks: list[DocChunk] = []
    with pymupdf.open(pdf_path) as doc:
        for pno, page in enumerate(doc, start=1):
            raw = page.get_text("text")
            for i, piece in enumerate(window(raw)):
                chunks.append(DocChunk(doc_id=doc_id, page=pno, block_index=i,
                                       kind="body", text=piece, parser="naive_pdf"))
    return chunks


# ---------------------------------------------------------------------------
# approach 3: layout-aware, PyMuPDF
# ---------------------------------------------------------------------------

def parse_layout_pymupdf(pdf_path: Path, doc_id: str) -> list[DocChunk]:
    import pymupdf

    chunks: list[DocChunk] = []
    with pymupdf.open(pdf_path) as doc:
        for pno, page in enumerate(doc, start=1):
            idx = 0
            table_rects = []
            try:
                found = page.find_tables()
                for t in found.tables:
                    body = _table_to_text(t.extract())
                    if not body.strip():
                        continue
                    table_rects.append(tuple(t.bbox))
                    for piece in window(body, size=1800, overlap=0):
                        chunks.append(DocChunk(doc_id=doc_id, page=pno, block_index=idx,
                                               kind="table", text=piece,
                                               parser="layout_pymupdf", bbox=tuple(t.bbox)))
                        idx += 1
            except Exception:
                # A page whose table finder fails still yields its prose below.
                pass

            raw_blocks = page.get_text("blocks")
            boxes = [(b[0], b[1], b[2], b[3], b[4]) for b in raw_blocks
                     if len(b) > 4 and isinstance(b[4], str) and b[4].strip()]

            def in_table(b):
                for (x0, y0, x1, y1) in table_rects:
                    if b[0] >= x0 - 2 and b[2] <= x1 + 2 and b[1] >= y0 - 2 and b[3] <= y1 + 2:
                        return True
                return False

            boxes = [b for b in boxes if not in_table(b)]
            ordered = _column_sort(boxes, page.rect.width)

            body_parts: list[str] = []
            for b in ordered:
                text = b[4].strip()
                if is_caption(text):
                    for piece in window(text, size=1800, overlap=0):
                        chunks.append(DocChunk(doc_id=doc_id, page=pno, block_index=idx,
                                               kind="caption", text=piece,
                                               parser="layout_pymupdf", bbox=b[:4]))
                        idx += 1
                else:
                    body_parts.append(text)

            for piece in window("\n".join(body_parts)):
                chunks.append(DocChunk(doc_id=doc_id, page=pno, block_index=idx,
                                       kind="body", text=piece, parser="layout_pymupdf"))
                idx += 1
    return chunks


# ---------------------------------------------------------------------------
# approach 4: layout-aware, pdfplumber
# ---------------------------------------------------------------------------

def parse_layout_pdfplumber(pdf_path: Path, doc_id: str) -> list[DocChunk]:
    import pdfplumber

    chunks: list[DocChunk] = []
    with pdfplumber.open(pdf_path) as pdf:
        for pno, page in enumerate(pdf.pages, start=1):
            idx = 0
            table_bboxes = []
            try:
                for t in page.find_tables():
                    body = _table_to_text(t.extract())
                    if not body.strip():
                        continue
                    table_bboxes.append(tuple(t.bbox))
                    for piece in window(body, size=1800, overlap=0):
                        chunks.append(DocChunk(doc_id=doc_id, page=pno, block_index=idx,
                                               kind="table", text=piece,
                                               parser="layout_pdfplumber", bbox=tuple(t.bbox)))
                        idx += 1
            except Exception:
                pass

            try:
                filtered = page
                for bbox in table_bboxes:
                    filtered = filtered.outside_bbox(bbox)
                words = filtered.extract_words(use_text_flow=False)
            except Exception:
                words = page.extract_words(use_text_flow=False)

            # Words must be assigned to a column BEFORE they are grouped into
            # lines. Grouping by vertical position first merges a left-column
            # word with the right-column word printed beside it, producing one
            # page-wide pseudo-line whose bbox then defeats column ordering -
            # measured on PMC10016410, that path recovered 4 of 40 gold body
            # sentences where the layout-blind baseline recovered 14.
            mid = float(page.width) / 2.0
            fully_left = sum(1 for w in words if w["x1"] <= mid)
            fully_right = sum(1 for w in words if w["x0"] >= mid)
            total_words = max(len(words), 1)
            two_col = fully_left >= total_words * 0.25 and fully_right >= total_words * 0.25

            columns: list[list] = [[], []]
            for w in words:
                if not two_col:
                    columns[0].append(w)
                else:
                    columns[0 if (w["x0"] + w["x1"]) / 2.0 < mid else 1].append(w)

            # Lines are then merged into paragraph blocks on vertical gap.
            # PyMuPDF hands back whole blocks, so classifying pdfplumber output
            # line by line would judge the two layout parsers on different-sized
            # units: a three-line caption would arrive as three short chunks and
            # lose any held-out phrase that fell on another line. Merging first
            # makes the comparison like for like.
            ordered = []
            for col in columns:
                lines: dict[int, list] = {}
                for w in col:
                    lines.setdefault(round(w["top"] / 3.0), []).append(w)
                rows = []
                for key in sorted(lines):
                    ws = sorted(lines[key], key=lambda w: w["x0"])
                    rows.append((min(w["x0"] for w in ws), min(w["top"] for w in ws),
                                 max(w["x1"] for w in ws), max(w["bottom"] for w in ws),
                                 " ".join(w["text"] for w in ws)))
                if not rows:
                    continue
                heights = sorted(r[3] - r[1] for r in rows)
                line_height = heights[len(heights) // 2] or 10.0
                block = list(rows[0])
                for row in rows[1:]:
                    if row[1] - block[3] <= line_height * 0.8:
                        block = [min(block[0], row[0]), min(block[1], row[1]),
                                 max(block[2], row[2]), max(block[3], row[3]),
                                 block[4] + " " + row[4]]
                    else:
                        ordered.append(tuple(block))
                        block = list(row)
                ordered.append(tuple(block))

            body_parts: list[str] = []
            for b in ordered:
                text = b[4].strip()
                if is_caption(text):
                    for piece in window(text, size=1800, overlap=0):
                        chunks.append(DocChunk(doc_id=doc_id, page=pno, block_index=idx,
                                               kind="caption", text=piece,
                                               parser="layout_pdfplumber", bbox=b[:4]))
                        idx += 1
                else:
                    body_parts.append(text)

            for piece in window("\n".join(body_parts)):
                chunks.append(DocChunk(doc_id=doc_id, page=pno, block_index=idx,
                                       kind="body", text=piece,
                                       parser="layout_pdfplumber"))
                idx += 1
    return chunks
