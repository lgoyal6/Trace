"""C31 parsing and provenance tests.

The PDFs are built here rather than committed: a synthetic two-column page with
known content is enough to pin reading order, table and caption handling, and
the provenance guarantee, and it keeps binaries out of the repository.
"""
from __future__ import annotations

import pytest

pymupdf = pytest.importorskip("pymupdf", reason="pip install -r backend/requirements-parsing.txt")

from backend.app.corpus.layout_parse import (  # noqa: E402
    DocChunk, is_caption, parse_abstract_only, parse_layout_pymupdf, parse_naive_pdf,
)

# Three paragraphs per column, vertically interleaved, so that ordering blocks
# by vertical position alone yields L1 R1 L2 R2 L3 R3 and breaks both columns.
# A single block per column would stay contiguous under either rule and the
# test would not be measuring column detection at all.
LEFT_PARTS = [
    "The patient presented with progressive supranuclear palsy and a vertical gaze palsy.",
    "Symptoms had worsened steadily over eighteen months of outpatient follow up.",
    "Levodopa produced no sustained benefit at any dose that was tolerated.",
]
RIGHT_PARTS = [
    "Midbrain atrophy was quantified on structural imaging by a blinded rater.",
    "Comparison used an age matched control cohort drawn from the same scanner.",
    "The hummingbird sign was present on the midsagittal reconstruction.",
]
LEFT = " ".join(LEFT_PARTS)
RIGHT = " ".join(RIGHT_PARTS)
PAGE2 = ("Hypometabolism was most pronounced in the frontal medial cortex on the "
         "interictal fluorodeoxyglucose positron emission tomography study.")
CAPTION = "Table 1 Baseline characteristics of the enrolled cohort"


@pytest.fixture()
def two_column_pdf(tmp_path):
    """Two pages. Page 1 is two-column with a caption; page 2 is single-column."""
    path = tmp_path / "synthetic.pdf"
    doc = pymupdf.open()
    page = doc.new_page(width=612, height=792)
    for i, (l, r) in enumerate(zip(LEFT_PARTS, RIGHT_PARTS)):
        top = 60 + i * 110
        page.insert_textbox(pymupdf.Rect(40, top, 290, top + 90), l, fontsize=10)
        page.insert_textbox(pymupdf.Rect(320, top, 570, top + 90), r, fontsize=10)
    page.insert_textbox(pymupdf.Rect(40, 430, 570, 470), CAPTION, fontsize=10)
    page2 = doc.new_page(width=612, height=792)
    page2.insert_textbox(pymupdf.Rect(40, 60, 570, 400), PAGE2, fontsize=10)
    doc.save(path)
    doc.close()
    return path


def _flat(chunks: list[DocChunk]) -> str:
    return " ".join(c.text for c in chunks)


def _alnum(text: str) -> str:
    return "".join(ch for ch in text.lower() if ch.isalnum())


# -- provenance --------------------------------------------------------------

def test_every_chunk_carries_document_and_page(two_column_pdf):
    chunks = parse_layout_pymupdf(two_column_pdf, "DOC1")
    assert chunks
    for c in chunks:
        assert c.doc_id == "DOC1"
        assert c.page >= 1


def test_page_number_matches_the_page_the_text_is_on(two_column_pdf):
    chunks = parse_layout_pymupdf(two_column_pdf, "DOC1")
    page1 = {c.page for c in chunks if _alnum(LEFT)[:40] in _alnum(c.text)}
    page2 = {c.page for c in chunks if _alnum(PAGE2)[:40] in _alnum(c.text)}
    assert page1 == {1}, f"page-1 text attributed to {page1}"
    assert page2 == {2}, f"page-2 text attributed to {page2}"


def test_provenance_survives_into_a_retrieval_result(two_column_pdf):
    """The claim C31 makes: the page reaches the answer, not just the parser."""
    from backend.measurement.run_parser_eval import BM25

    chunks = parse_layout_pymupdf(two_column_pdf, "DOC1")
    index = BM25(chunks)
    ranked = index.search("interictal fluorodeoxyglucose positron emission tomography", top_k=3)
    assert ranked, "retriever returned nothing"
    top = chunks[ranked[0][0]]
    assert _alnum(PAGE2)[:40] in _alnum(top.text)
    assert top.page == 2
    assert top.doc_id == "DOC1"
    assert top.citation == "DOC1 p.2"


def test_abstract_only_declares_that_it_has_no_page(two_column_pdf):
    """Trace's shipped shape must report missing provenance, not invent one."""
    chunks = parse_abstract_only("DOC1", "A title", "An abstract body of text.")
    assert chunks
    assert all(c.page == 0 for c in chunks)
    assert "no page attribution" in chunks[0].citation


# -- reading order -----------------------------------------------------------

def test_layout_parser_keeps_each_column_contiguous(two_column_pdf):
    chunks = parse_layout_pymupdf(two_column_pdf, "DOC1")
    flat = _alnum(_flat(chunks))
    assert _alnum(LEFT) in flat, "left column was interleaved with the right"
    assert _alnum(RIGHT) in flat, "right column was interleaved with the left"


def test_column_sort_emits_whole_columns_not_interleaved_rows():
    """Ordering rule in isolation, independent of any PDF text layer."""
    from backend.app.corpus.layout_parse import _column_sort

    boxes = []
    for i in range(3):
        top = 60.0 + i * 110
        boxes.append((40.0, top, 290.0, top + 90, f"L{i}"))
        boxes.append((320.0, top, 570.0, top + 90, f"R{i}"))
    order = [b[4] for b in _column_sort(boxes, 612.0)]
    assert order == ["L0", "L1", "L2", "R0", "R1", "R2"], order


# -- classification ----------------------------------------------------------

@pytest.mark.parametrize("text", [
    "Table 1 Clinical phenotypes and baseline characteristics",
    "Figure 1 Subtype progression patterns of PSP atrophy",
    "Table 2. The total intracranial volume was calculated using",
])
def test_caption_lines_are_captions(text):
    assert is_caption(text) is True


@pytest.mark.parametrize("text", [
    "Table 1 summarizes the key baseline clinical features for individuals",
    "Table 2 gives an overview of demographics, clinical diagnosis",
    "Table 3 shows the relationship between clinical test scores",
    "Midbrain atrophy was quantified on structural imaging",
])
def test_prose_about_a_table_is_not_a_caption(text):
    assert is_caption(text) is False


def test_single_column_prose_box_is_not_a_table():
    """The observed failure: a ruled abstract box came back as a 30-row grid in
    which only one column ever held text."""
    from backend.app.corpus.layout_parse import _table_to_text

    prose_box = [[None, "A paragraph of running prose.", None] for _ in range(30)]
    assert _table_to_text(prose_box) == ""

    real_table = [["Subtype", "n", "Age"], ["cortical", "34", "68.1"], ["subcortical", "51", "70.4"]]
    assert "Subtype | n | Age" in _table_to_text(real_table)


def test_naive_and_layout_parsers_agree_on_page_count(two_column_pdf):
    naive = parse_naive_pdf(two_column_pdf, "DOC1")
    layout = parse_layout_pymupdf(two_column_pdf, "DOC1")
    assert {c.page for c in naive} == {c.page for c in layout} == {1, 2}


# -- provenance through LlamaIndex ------------------------------------------

def test_provenance_survives_a_llamaindex_round_trip(two_column_pdf):
    """C31 names LlamaIndex, so the page must survive that framework's own node
    and retriever types, not only the in-repo retriever. Metadata is excluded
    from the embed/LLM views so the page number cannot leak into the retrieval
    signal itself and flatter the result.
    """
    pytest.importorskip("llama_index.core", reason="pip install -r backend/requirements-parsing.txt")
    bm25_mod = pytest.importorskip("llama_index.retrievers.bm25")
    from llama_index.core.schema import TextNode

    chunks = parse_layout_pymupdf(two_column_pdf, "DOC1")
    nodes = [
        TextNode(
            text=c.text,
            metadata={"doc_id": c.doc_id, "page": c.page, "kind": c.kind},
            excluded_embed_metadata_keys=["doc_id", "page", "kind"],
            excluded_llm_metadata_keys=["doc_id", "page", "kind"],
        )
        for c in chunks
    ]
    retriever = bm25_mod.BM25Retriever.from_defaults(nodes=nodes, similarity_top_k=3)
    results = retriever.retrieve("interictal fluorodeoxyglucose positron emission tomography")

    assert results, "LlamaIndex retriever returned nothing"
    top = results[0]
    assert _alnum(PAGE2)[:40] in _alnum(top.node.get_content())
    assert top.node.metadata["page"] == 2
    assert top.node.metadata["doc_id"] == "DOC1"
