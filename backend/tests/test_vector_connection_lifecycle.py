"""Check local retriever connection ownership without changing its frozen port.
Uses retrieval.py with a temporary corpus and a captured real SQLite connection.
An operation must close the handle even while another reference retains it.
"""
import json
import sqlite3
import pytest
from backend.vectorstore import retrieval


def test_health_closes_its_database_connection(tmp_path, monkeypatch):
    corpus = tmp_path / "corpus.json"
    corpus.write_text(json.dumps([]))
    store = retrieval.LocalVectorRetriever(tmp_path / "index.sqlite3", corpus)
    connect = sqlite3.connect
    captured = []
    def capture(*args, **kwargs):
        conn = connect(*args, **kwargs)
        captured.append(conn)
        return conn
    monkeypatch.setattr(retrieval.sqlite3, "connect", capture)
    store.health()
    assert len(captured) == 1
    with pytest.raises(sqlite3.ProgrammingError, match="closed"):
        captured[0].execute("SELECT 1")
