"""Provenance for the corpus: which fetched document version produced which
stored record.

Measured before this module existed: every one of the 329 records in
`backend/data/corpus.json` carried exactly eight keys (abstract, atlas_label,
condition, overlaps_with, pmid, rarity, region_literature, title) and not one
of them named a fetch time, a source URL, a content hash, or a parser version.
0 of 329 records could be traced to the input that produced them. There was no
sidecar manifest either - `backend/data/` held one file.

--- WHAT PROVENANCE MEANS HERE ---------------------------------------------

Two hashes, kept apart on purpose:

    content_sha256   sha256 of the RAW BYTES E-utils returned for one efetch
                     request. This identifies the input *document version*.
    record_sha256    sha256 of the canonicalised derived fields of one stored
                     record. This identifies the OUTPUT.

A `RecordProvenance` row binds one output to one input, plus the
`parser_version` that turned one into the other. Given a stored record you can
answer "which fetched document, fetched when, parsed by which parser, produced
this?" - and given a fetched document you can answer "is what I stored still
what this document says?" (`Manifest.drifted`).

--- WHY PARSER_VERSION IS NOT COSMETIC -------------------------------------

The content hash alone is not enough to decide whether an unchanged document
can skip reprocessing. If `fetch_pubmed._parse_articles` changes which element
it reads, the same bytes produce a different record, so the skip must also be
conditional on the parser being the same one. `needs_reprocess` checks both,
and says which of the two forced the work.

Bump `PARSER_VERSION` whenever `_parse_articles` changes what it extracts.
`test_provenance.py` pins the current value so a silent edit to the parser
without a bump is at least a visible test diff.

--- WHAT THIS IS NOT -------------------------------------------------------

Not a content-addressed store: the raw efetch bytes are hashed and then
discarded, not archived. So the manifest can tell you the document you stored
has changed since; it cannot show you the old document. Archiving raw
responses is a storage decision this repo has not made, and inventing one
here would be a bigger change than the provenance question needed.
"""
from __future__ import annotations

import datetime as _dt
import hashlib
import json
from dataclasses import asdict, dataclass
from pathlib import Path
from typing import Any, Iterable, Mapping

#: Bump when `backend/app/corpus/fetch_pubmed._parse_articles` changes what it
#: extracts from an efetch response. Pinned by test_provenance.py.
PARSER_VERSION = "pubmed-efetch/1"

MANIFEST_VERSION = 1

DEFAULT_MANIFEST_PATH = (
    Path(__file__).resolve().parent.parent.parent / "data" / "corpus_manifest.json"
)

#: The stored fields that are derived from the fetched document, and so are
#: what `record_sha256` covers. `condition`/`rarity`/`atlas_label` are local
#: curation attached by build_corpus, not document content, so a change to
#: them is not document drift and must not read as one.
DERIVED_FIELDS: tuple[str, ...] = ("pmid", "title", "abstract")


def content_hash(raw: bytes) -> str:
    """sha256 of the raw fetched bytes. Identifies the input document version."""
    return hashlib.sha256(raw).hexdigest()


def record_hash(
    record: Mapping[str, Any], fields: Iterable[str] = DERIVED_FIELDS
) -> str:
    """sha256 over the canonicalised derived fields of one stored record.

    Canonical form is sorted-key, separator-tight JSON with non-ASCII left
    intact, so two records that differ only in key order or in the whitespace
    of the file they were written to hash the same.
    """
    canonical = json.dumps(
        {f: record.get(f, "") for f in fields},
        sort_keys=True,
        ensure_ascii=False,
        separators=(",", ":"),
    )
    return hashlib.sha256(canonical.encode("utf-8")).hexdigest()


def _now_iso() -> str:
    return _dt.datetime.now(_dt.timezone.utc).isoformat()


@dataclass(frozen=True)
class SourceDocument:
    """One fetched document version."""

    url: str
    fetched_at_iso: str
    content_sha256: str
    byte_length: int
    parser_version: str = PARSER_VERSION


@dataclass(frozen=True)
class RecordProvenance:
    """One stored record, bound to the document version that produced it."""

    pmid: str
    source_url: str
    fetched_at_iso: str
    source_sha256: str
    parser_version: str
    record_sha256: str


def describe_source(
    url: str,
    raw: bytes,
    *,
    fetched_at_iso: str | None = None,
    parser_version: str = PARSER_VERSION,
) -> SourceDocument:
    return SourceDocument(
        url=url,
        fetched_at_iso=fetched_at_iso or _now_iso(),
        content_sha256=content_hash(raw),
        byte_length=len(raw),
        parser_version=parser_version,
    )


class Manifest:
    """The sidecar that makes stored records traceable.

    Written next to `corpus.json` rather than into it, because `corpus.json`
    is a frozen fixture 336 tests read: adding eight provenance keys to every
    record would change the fixture every test in the suite loads. The
    manifest is keyed by pmid, so a record and its provenance are joined on
    read instead.
    """

    def __init__(self) -> None:
        self.documents: dict[str, SourceDocument] = {}
        self.records: dict[str, RecordProvenance] = {}

    # -- persistence ----------------------------------------------------
    def to_dict(self) -> dict:
        return {
            "manifest_version": MANIFEST_VERSION,
            "parser_version": PARSER_VERSION,
            "documents": {k: asdict(v) for k, v in self.documents.items()},
            "records": {k: asdict(v) for k, v in self.records.items()},
        }

    @classmethod
    def from_dict(cls, data: Mapping[str, Any]) -> "Manifest":
        manifest = cls()
        for url, doc in (data.get("documents") or {}).items():
            manifest.documents[url] = SourceDocument(**doc)
        for pmid, prov in (data.get("records") or {}).items():
            manifest.records[pmid] = RecordProvenance(**prov)
        return manifest

    @classmethod
    def load(cls, path: Path = DEFAULT_MANIFEST_PATH) -> "Manifest":
        """An absent manifest is an empty one, not an error: the corpus
        predates provenance and must stay readable."""
        path = Path(path)
        if not path.exists():
            return cls()
        return cls.from_dict(json.loads(path.read_text()))

    def save(self, path: Path = DEFAULT_MANIFEST_PATH) -> Path:
        path = Path(path)
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(json.dumps(self.to_dict(), indent=2, sort_keys=True))
        return path

    # -- recording ------------------------------------------------------
    def record_fetch(self, doc: SourceDocument) -> None:
        self.documents[doc.url] = doc

    def record_derived(self, record: Mapping[str, Any], doc: SourceDocument) -> RecordProvenance:
        """Bind one stored record to the document version it came from."""
        prov = RecordProvenance(
            pmid=str(record["pmid"]),
            source_url=doc.url,
            fetched_at_iso=doc.fetched_at_iso,
            source_sha256=doc.content_sha256,
            parser_version=doc.parser_version,
            record_sha256=record_hash(record),
        )
        self.records[prov.pmid] = prov
        return prov

    def forget(self, pmid: str) -> bool:
        """Drop one record's provenance. Returns whether there was one.

        Deletion has to reach the manifest too: a manifest row names a pmid, a
        source URL and a fetch time, which is exactly the metadata a deletion
        request is usually about.
        """
        return self.records.pop(str(pmid), None) is not None

    # -- questions ------------------------------------------------------
    def provenance_for(self, pmid: str) -> RecordProvenance | None:
        return self.records.get(str(pmid))

    def needs_reprocess(self, doc: SourceDocument) -> tuple[bool, str]:
        """(should_reprocess, reason) for a freshly fetched document.

        The whole point of the content hash: an unchanged document parsed by
        the same parser has nothing new to say, so the parse, the derived
        record, the re-embed and the index update can all be skipped. It does
        NOT skip the fetch - the hash is of the response, so the bytes have
        already crossed the wire by the time this can answer. Saying otherwise
        would be a lie about what a content hash can do; conditional GET is
        the mechanism that would skip the fetch, and E-utils does not offer a
        usable validator for it.
        """
        prior = self.documents.get(doc.url)
        if prior is None:
            return True, "no prior fetch recorded for this url"
        if prior.content_sha256 != doc.content_sha256:
            return True, "document content changed"
        if prior.parser_version != doc.parser_version:
            return True, (
                f"parser version changed {prior.parser_version} -> {doc.parser_version}"
            )
        return False, "unchanged document, same parser"

    def audit(self, records: Iterable[Mapping[str, Any]]) -> dict:
        """How much of a stored corpus can be traced back to an input.

        `drifted` is the interesting column: a record whose manifest row
        exists but whose current derived fields no longer hash to what was
        recorded. That means something edited the stored record after it was
        derived, which is the case provenance exists to catch.
        """
        traced: list[str] = []
        untraced: list[str] = []
        drifted: list[str] = []
        for record in records:
            pmid = str(record.get("pmid", ""))
            prov = self.records.get(pmid)
            if prov is None:
                untraced.append(pmid)
                continue
            traced.append(pmid)
            if prov.record_sha256 != record_hash(record):
                drifted.append(pmid)
        return {
            "records": len(traced) + len(untraced),
            "traced": len(traced),
            "untraced": len(untraced),
            "drifted": len(drifted),
            "untraced_pmids": untraced,
            "drifted_pmids": drifted,
        }
