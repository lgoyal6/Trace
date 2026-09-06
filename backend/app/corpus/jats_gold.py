"""Ground truth for the C31 parser comparison, taken from publisher JATS XML.

Every document in the eval set ships from PMC as both a PDF and the publisher's
own JATS XML. The XML states, without inference, what each table contains and
what each figure caption says. That makes it the reference answer key for
scoring a PDF parser, and it removes the need to hand-label anything.

The XML is the answer key, never a candidate in the head-to-head: scoring a
parser against itself would be circular. Its own cost and its own limits are
reported separately in the C31 record.
"""
from __future__ import annotations

import re
import xml.etree.ElementTree as ET
from dataclasses import dataclass
from pathlib import Path

_WS = re.compile(r"\s+")


def norm(text: str) -> str:
    """Whitespace-normalized, soft-hyphen-free comparison form."""
    return _WS.sub(" ", (text or "").replace("­", "").replace("‐", "-")).strip()


def _text(el) -> str:
    return norm("".join(el.itertext())) if el is not None else ""


@dataclass(frozen=True)
class GoldItem:
    """One checkable fact from the publisher's own markup.

    `kind` is "table_cell", "caption" or "body_sentence". `text` is the string a
    parser must reproduce for the fact to count as extracted.
    """

    doc_id: str
    kind: str
    label: str
    text: str


def _strip_ns(root):
    for el in root.iter():
        if isinstance(el.tag, str) and "}" in el.tag:
            el.tag = el.tag.split("}", 1)[1]
    return root


def load_gold(xml_path: Path, doc_id: str) -> list[GoldItem]:
    root = _strip_ns(ET.parse(xml_path).getroot())
    items: list[GoldItem] = []

    for tw in root.iter("table-wrap"):
        label = _text(tw.find("label")) or "table"
        cap = _text(tw.find("caption"))
        if len(cap) >= 25:
            items.append(GoldItem(doc_id, "caption", label, cap))
        for row in tw.iter("tr"):
            cells = [_text(c) for c in list(row) if c.tag in ("td", "th")]
            # A row is checkable only if it pairs a label with a value; a lone
            # number is too weak a string to attribute to one table.
            informative = [c for c in cells if len(c) >= 2]
            if len(informative) >= 2:
                items.append(GoldItem(doc_id, "table_cell", label, " ".join(informative[:4])))

    for fig in root.iter("fig"):
        label = _text(fig.find("label")) or "figure"
        cap = _text(fig.find("caption"))
        if len(cap) >= 25:
            items.append(GoldItem(doc_id, "caption", label, cap))

    body = root.find("body")
    if body is not None:
        for p in body.iter("p"):
            txt = _text(p)
            for sent in re.split(r"(?<=[.!?])\s+", txt):
                sent = sent.strip()
                if 80 <= len(sent) <= 400:
                    items.append(GoldItem(doc_id, "body_sentence", "body", sent))

    return items
