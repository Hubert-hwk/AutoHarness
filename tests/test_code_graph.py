from pathlib import Path

from autoharness.code_graph import PythonCodeGraph
from autoharness.models import Evidence, FailureDiagnosis, FailureType


def test_graph_indexes_and_localizes_python_symbols(tmp_path: Path) -> None:
    source = tmp_path / "retriever.py"
    source.write_text(
        '"""Policy retrieval."""\n'
        "def retrieve_documents(query: str):\n"
        '    """Retrieve documents from the vector store."""\n'
        "    return vector_search(query)\n",
        encoding="utf-8",
    )
    diagnosis = FailureDiagnosis(
        failure_type=FailureType.RETRIEVAL,
        confidence=0.9,
        summary="Retrieval failed",
        likely_causes=["low recall"],
        evidence=[Evidence(source="log", excerpt="retrieval failed", signal="retrieval")],
        recommended_checks=["inspect retriever"],
        search_terms=["retrieve", "vector"],
    )

    graph = PythonCodeGraph(tmp_path)
    stats = graph.build()
    locations = graph.locate(diagnosis)

    assert stats.files == 1
    assert stats.symbols == 2
    assert locations[0].path == "retriever.py"
    assert any(location.symbol == "retrieve_documents" for location in locations)


def test_graph_skips_invalid_python(tmp_path: Path) -> None:
    (tmp_path / "broken.py").write_text("def broken(", encoding="utf-8")

    stats = PythonCodeGraph(tmp_path).build()

    assert stats.parse_errors == 1
    assert stats.files == 0
