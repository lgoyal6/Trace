"""NCBI E-utils client for the one-time corpus fetch (Step 2.5). No API key
required but rate-limited to ~3 req/sec without one, hence the sleep between
calls in build_corpus.py's caller loop, not in these low-level functions.

Everything E-utils returns is untrusted input, including the part that decides
where the next request goes. `backend.app.net.safe_http.fetch` is what keeps a
redirect or a DNS answer from turning "fetch an abstract" into a request at an
internal address, and caps the body so an oversized response cannot be read
into memory whole. See that module for why the check is at the socket rather
than on the URL string.
"""
from __future__ import annotations

import json
import xml.etree.ElementTree as ET
from urllib.parse import urlencode

from backend.app.net.safe_http import fetch
from backend.app.corpus.provenance import SourceDocument, describe_source

ESEARCH_URL = "https://eutils.ncbi.nlm.nih.gov/entrez/eutils/esearch.fcgi"
EFETCH_URL = "https://eutils.ncbi.nlm.nih.gov/entrez/eutils/efetch.fcgi"
USER_AGENT = "Mozilla/5.0 (Windows NT 10.0; Win64; x64) NeuLitTrace/1.0"


def _valid_pmids(candidates) -> list[str]:
    """PMIDs are decimal integers. The list comes out of the esearch response,
    i.e. off the network, and is pasted into the efetch query string - so it is
    filtered to the shape a PMID actually has rather than trusted and encoded.
    """
    if not isinstance(candidates, list):
        return []
    return [str(c) for c in candidates if str(c).isdigit()]


def _parse_articles(xml_bytes: bytes):
    """Parse an efetch response.

    Rejects a document that declares its own entities. The NLM DTD efetch
    names is external and declares none inline, so nothing legitimate is lost,
    and it removes the entity-expansion surface rather than leaving it to
    expat's amplification heuristic to notice.
    """
    if b"<!ENTITY" in xml_bytes:
        raise ValueError("efetch response declares XML entities; refusing to parse it")
    return ET.fromstring(xml_bytes)


def search_pmids(query: str, retmax: int) -> list[str]:
    params = urlencode({
        "db": "pubmed", "term": query, "retmax": retmax, "retmode": "json",
    })
    body = fetch(f"{ESEARCH_URL}?{params}", headers={"User-Agent": USER_AGENT})
    data = json.loads(body)
    return _valid_pmids(data["esearchresult"]["idlist"])


def fetch_abstracts(pmids: list[str]) -> list[dict]:
    """Papers only. `fetch_abstracts_with_provenance` is the same fetch with
    the source-document descriptor kept; this wrapper exists so the callers
    that do not record provenance keep their original signature."""
    papers, _ = fetch_abstracts_with_provenance(pmids)
    return papers


def fetch_abstracts_with_provenance(
    pmids: list[str],
) -> tuple[list[dict], SourceDocument | None]:
    """Returns (papers, the fetched document version they were derived from).

    The descriptor carries the sha256 of the raw efetch bytes, the fetch time,
    the request URL and the parser version, which together are what lets a
    stored record name the input that produced it and lets an unchanged
    document skip reprocessing. `None` when nothing was fetched.
    """
    ids = _valid_pmids(pmids)
    if not ids:
        return [], None
    params = urlencode({
        "db": "pubmed", "id": ",".join(ids), "rettype": "abstract", "retmode": "xml",
    })
    url = f"{EFETCH_URL}?{params}"
    xml_bytes = fetch(url, headers={"User-Agent": USER_AGENT})
    source = describe_source(url, xml_bytes)

    root = _parse_articles(xml_bytes)
    papers = []
    for article in root.findall(".//PubmedArticle"):
        pmid_el = article.find(".//PMID")
        title_el = article.find(".//ArticleTitle")
        abstract_el = article.find(".//AbstractText")
        if pmid_el is None or title_el is None or abstract_el is None:
            continue
        papers.append({
            "pmid": pmid_el.text or "",
            "title": title_el.text or "",
            "abstract": abstract_el.text or "",
        })
    return papers, source
