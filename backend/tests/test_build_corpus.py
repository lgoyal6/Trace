import json
from unittest.mock import patch
from backend.app.corpus.conditions import Condition
from backend.app.corpus.build_corpus import build_corpus
from backend.app.corpus.provenance import describe_source

FAKE_CONDITION = Condition(
    name="Test condition", rarity="rare", pubmed_query="test query",
    region_literature="Test region", atlas_label="Test Atlas Label",
    target_count=2,
)


# build_corpus now calls fetch_abstracts_with_provenance, which returns
# (papers, SourceDocument) so each stored record can name the fetched document
# version it came from. The patch target moved with it; the assertions below
# are unchanged.
FAKE_PAPERS = [{"pmid": "1", "title": "T1", "abstract": "A1"},
               {"pmid": "2", "title": "T2", "abstract": "A2"}]
FAKE_SOURCE = describe_source("https://example.invalid/efetch?id=1,2", b"<xml>fixture</xml>")


def test_build_corpus_tags_each_paper_with_condition_metadata(tmp_path):
    with patch("backend.app.corpus.build_corpus.search_pmids", return_value=["1", "2"]), \
         patch("backend.app.corpus.build_corpus.fetch_abstracts_with_provenance",
               return_value=(FAKE_PAPERS, FAKE_SOURCE)), \
         patch("backend.app.corpus.build_corpus.time.sleep"):
        out_path = tmp_path / "corpus.json"
        result = build_corpus([FAKE_CONDITION], out_path)

    assert len(result) == 2
    assert result[0]["condition"] == "Test condition"
    assert result[0]["rarity"] == "rare"
    assert result[0]["atlas_label"] == "Test Atlas Label"
    saved = json.loads(out_path.read_text())
    assert saved == result


# -- ingest into Snowflake ----------------------------------------------------
# load_to_snowflake writes text that came off the network (titles, abstracts,
# journals, URLs). No Snowflake connection is made here: a recording session
# captures the statement and its bound parameters.

import backend.snowflake.session as sf_session  # noqa: E402
from backend.app.corpus import build_corpus as build_corpus_mod  # noqa: E402

HOSTILE_ABSTRACT = (
    "Benign looking abstract text.\\', 'x', 'x', NULL, 'x', TRUE, 'x', 'x'); "
    "DROP TABLE NEULIT.CORE.TOKEN_LEDGER; --"
)


class _RecordingSession:
    def __init__(self):
        self.calls: list[tuple[str, list]] = []

    def sql(self, text, params=None):
        self.calls.append((text, list(params or [])))
        return self

    def collect(self):
        return []


def _executable_sql(statement: str) -> str:
    """Everything in `statement` that is NOT inside a string literal, under
    Snowflake's literal rules: inside a literal both `''` and a backslash
    escape yield one character, so `\\'` closes nothing that `''` would.
    """
    out, cur, in_literal, i = [], [], False, 0
    while i < len(statement):
        c = statement[i]
        if not in_literal:
            if c == "'":
                out.append("".join(cur)); cur = []; in_literal = True
            else:
                cur.append(c)
            i += 1
            continue
        if c == "\\" and i + 1 < len(statement):
            i += 2
            continue
        if c == "'" and i + 1 < len(statement) and statement[i + 1] == "'":
            i += 2
            continue
        if c == "'":
            cur = []; in_literal = False; i += 1
            continue
        i += 1
    out.append("".join(cur))
    return "".join(out)


def _load_hostile_corpus(tmp_path):
    corpus = [{
        "pmid": "999", "title": "T", "abstract": HOSTILE_ABSTRACT,
        "condition": "C", "rarity": "rare", "atlas_label": "A",
        "region_literature": "R", "journal": "J", "year": 2020,
    }]
    path = tmp_path / "hostile.json"
    path.write_text(json.dumps(corpus))
    session = _RecordingSession()
    with patch.object(sf_session, "get_session", lambda: session), \
         patch.object(sf_session, "snowflake_available", lambda: True):
        build_corpus_mod.load_to_snowflake(path)
    return session


def test_abstract_text_never_reaches_the_sql_statement(tmp_path):
    """A fetched abstract must not be able to add SQL. It is the loader's only
    real trust boundary: PubMed decides what is in that string."""
    session = _load_hostile_corpus(tmp_path)
    insert = next(t for t, _ in session.calls if t.startswith("INSERT INTO NEULIT.CORE.PAPERS"))
    assert "DROP TABLE" not in _executable_sql(insert).upper()
    assert HOSTILE_ABSTRACT not in insert


def test_abstract_text_is_passed_as_a_bound_parameter(tmp_path):
    session = _load_hostile_corpus(tmp_path)
    _, params = next(
        (t, p) for t, p in session.calls if t.startswith("INSERT INTO NEULIT.CORE.PAPERS")
    )
    assert HOSTILE_ABSTRACT in params
