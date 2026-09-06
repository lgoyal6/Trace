from unittest.mock import patch
from backend.app.corpus.fetch_pubmed import search_pmids, fetch_abstracts

ESEARCH_JSON = b'{"esearchresult": {"idlist": ["111", "222"]}}'
EFETCH_XML = b"""<?xml version="1.0"?>
<PubmedArticleSet>
  <PubmedArticle>
    <MedlineCitation>
      <PMID>111</PMID>
      <Article>
        <ArticleTitle>A rare case report</ArticleTitle>
        <Abstract><AbstractText>Patient presented with focal uptake.</AbstractText></Abstract>
      </Article>
    </MedlineCitation>
  </PubmedArticle>
</PubmedArticleSet>"""


def test_search_pmids_parses_idlist():
    with patch("backend.app.corpus.fetch_pubmed.fetch", return_value=ESEARCH_JSON):
        ids = search_pmids("scalp angiosarcoma AND PET", retmax=2)
    assert ids == ["111", "222"]


def test_fetch_abstracts_parses_xml():
    with patch("backend.app.corpus.fetch_pubmed.fetch", return_value=EFETCH_XML):
        papers = fetch_abstracts(["111"])
    assert papers == [{
        "pmid": "111",
        "title": "A rare case report",
        "abstract": "Patient presented with focal uptake.",
    }]


# -- ingest boundary ----------------------------------------------------------
# These drive the real socket path rather than patching `fetch`, because what
# is under test is whether the fetcher can be walked off PubMed and onto an
# internal address. Everything binds 127.0.0.1.

import socket  # noqa: E402
import threading  # noqa: E402
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer  # noqa: E402

import pytest  # noqa: E402

from backend.app.corpus import fetch_pubmed  # noqa: E402


def _serve(fn):
    class _H(BaseHTTPRequestHandler):
        def do_GET(self):  # noqa: N802
            fn(self)

        def log_message(self, *args):
            pass

    s = socket.socket()
    s.bind(("127.0.0.1", 0))
    port = s.getsockname()[1]
    s.close()
    server = ThreadingHTTPServer(("127.0.0.1", port), _H)
    threading.Thread(target=server.serve_forever, daemon=True).start()
    return port, server


def test_fetch_abstracts_refuses_a_redirect_into_private_space(monkeypatch):
    """A 302 from the efetch host to an instance-metadata address must not be
    followed. The assertion is on the destination's hit log, so a request that
    lands fails the test even if the call also raised."""
    hits: list[str] = []

    def metadata(h):
        hits.append(h.path)
        h.send_response(200)
        h.send_header("Content-Length", "2")
        h.end_headers()
        h.wfile.write(b"{}")

    meta_port, meta_server = _serve(metadata)
    target = f"http://127.0.0.1:{meta_port}/latest/meta-data/iam/security-credentials/"

    def redirect(h):
        h.send_response(302)
        h.send_header("Location", target)
        h.send_header("Content-Length", "0")
        h.end_headers()

    port, server = _serve(redirect)
    try:
        monkeypatch.setattr(
            fetch_pubmed, "EFETCH_URL",
            f"http://127.0.0.1:{port}/entrez/eutils/efetch.fcgi",
        )
        with pytest.raises(Exception):
            fetch_pubmed.fetch_abstracts(["12345"])
        assert hits == [], f"request reached the forbidden destination: {hits}"
    finally:
        server.shutdown()
        meta_server.shutdown()


def test_fetch_abstracts_rejects_entity_declarations():
    bomb = (
        b'<?xml version="1.0"?><!DOCTYPE d [<!ENTITY a "AAAAAAAAAA">'
        b'<!ENTITY b "&a;&a;&a;&a;&a;&a;&a;&a;&a;&a;">]>'
        b'<PubmedArticleSet><PubmedArticle><PMID>1</PMID>'
        b'<ArticleTitle>&b;</ArticleTitle><AbstractText>x</AbstractText>'
        b'</PubmedArticle></PubmedArticleSet>'
    )
    with patch("backend.app.corpus.fetch_pubmed.fetch", return_value=bomb):
        with pytest.raises(ValueError):
            fetch_abstracts(["1"])


def test_pmids_from_the_network_cannot_rewrite_the_efetch_query():
    """The id list is an efetch query-string value that arrives over the wire.
    A value that is not a PMID is dropped rather than pasted in."""
    captured: dict[str, str] = {}

    def capture(url, **kwargs):
        captured["url"] = url
        return b"<PubmedArticleSet></PubmedArticleSet>"

    with patch("backend.app.corpus.fetch_pubmed.fetch", capture):
        fetch_abstracts(["111", "1&db=nuccore&rettype=fasta", "../../etc/passwd"])
    assert "nuccore" not in captured["url"]
    assert "id=111&" in captured["url"] or captured["url"].endswith("id=111")


def test_search_pmids_filters_the_upstream_idlist():
    payload = b'{"esearchresult": {"idlist": ["111", "1&db=nuccore", null, "222"]}}'
    with patch("backend.app.corpus.fetch_pubmed.fetch", return_value=payload):
        assert search_pmids("q", retmax=4) == ["111", "222"]
