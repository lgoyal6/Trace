from backend.vectorstore.retrieval import LocalVectorRetriever


def test_local_vector_store_persists_and_retrieves(tmp_path):
    db_path = tmp_path / "vectors.sqlite3"
    first = LocalVectorRetriever(db_path=db_path)
    results = first.search("mitochondrial disease", top_k=3)
    assert results
    assert results[0].semantic_score > 0
    assert first.health()["ok"] is True

    reopened = LocalVectorRetriever(db_path=db_path)
    assert reopened.health()["detail"] == first.health()["detail"]
    assert [item.paper.pmid for item in reopened.search("mitochondrial disease", top_k=3)] == [item.paper.pmid for item in results]


def test_local_vector_store_supports_port_operations(tmp_path):
    retriever = LocalVectorRetriever(db_path=tmp_path / "vectors.sqlite3")
    result = retriever.search("rare neurological syndrome", top_k=5, exclude_pmids=["1001"])
    assert all(item.paper.pmid != "1001" for item in result)
    assert retriever.get_by_pmids([result[0].paper.pmid])[0].pmid == result[0].paper.pmid
    assert retriever.closest_conditions("mitochondrial", top_n=1)[0].condition
