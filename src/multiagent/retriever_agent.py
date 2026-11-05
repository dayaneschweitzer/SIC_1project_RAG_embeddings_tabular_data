from __future__ import annotations

from pathlib import Path

try:
    from rag.evidence import evidences_from_search_results
except ImportError:
    import sys

    SRC_DIR = Path(__file__).resolve().parents[1]
    sys.path.insert(0, str(SRC_DIR))
    from rag.evidence import evidences_from_search_results

try:
    from retrieval.search_index import search_index
except ImportError:
    import sys

    SRC_DIR = Path(__file__).resolve().parents[1]
    sys.path.insert(0, str(SRC_DIR))
    from retrieval.search_index import search_index


AGENT_NAME = "retriever"


def run_retriever_agent(
    retrieval_plan: dict,
    index_dir: str,
    basename: str,
    search_results: list[dict] | None = None,
) -> dict:
    query = retrieval_plan.get("query", "")
    top_k = int(retrieval_plan.get("top_k", 5))

    if search_results is None:
        search_results = search_index(
            query=query,
            top_k=top_k,
            index_dir=index_dir,
            basename=basename,
        )

    evidences = evidences_from_search_results(search_results)

    return {
        "agent": AGENT_NAME,
        "query": query,
        "top_k": top_k,
        "preferred_block_types": retrieval_plan.get("preferred_block_types", []),
        "retrieved_evidences": evidences,
    }
