from __future__ import annotations

from pathlib import Path

try:
    from .agent_context import AgentContext
    from .agent_trace import append_trace
    from .modification_executor_agent import run_modification_executor_agent
    from .retriever_agent import run_retriever_agent
    from .router_planner_agent import run_router_planner
    from .verifier_executor_agent import run_verifier_executor_agent
except ImportError:
    from agent_context import AgentContext
    from agent_trace import append_trace
    from modification_executor_agent import run_modification_executor_agent
    from retriever_agent import run_retriever_agent
    from router_planner_agent import run_router_planner
    from verifier_executor_agent import run_verifier_executor_agent

try:
    from retrieval.vector_index import infer_single_basename, load_index_artifacts
except ImportError:
    import sys

    SRC_DIR = Path(__file__).resolve().parents[1]
    sys.path.insert(0, str(SRC_DIR))
    from retrieval.vector_index import infer_single_basename, load_index_artifacts


ARCHITECTURE_NAME = "multiagent-rag-controlled-modification"


def run_multiagent_modification(
    request: str,
    input_file: str,
    snapshot_path: str,
    index_dir: str = "data/index",
    basename: str | None = None,
    output_dir: str = "data/modified",
    top_k: int = 5,
    search_results: list[dict] | None = None,
    index_config: dict | None = None,
) -> dict:
    basename = basename or infer_single_basename(index_dir)
    index_config = index_config or _load_index_config(index_dir=index_dir, basename=basename)
    context = AgentContext(
        question=request,
        basename=basename,
        index_dir=index_dir,
        top_k=top_k,
    )

    context.planner_output = run_router_planner(question=request, top_k=top_k)
    append_trace(
        context.agent_trace,
        agent="router_planner",
        action="create_modification_retrieval_plan",
        input_summary=f"request={request}",
        output_summary=(
            f"intent={context.planner_output.get('intent')}; "
            f"top_k={context.planner_output.get('retrieval_plan', {}).get('top_k')}"
        ),
    )

    context.retriever_output = run_retriever_agent(
        retrieval_plan=context.planner_output["retrieval_plan"],
        index_dir=index_dir,
        basename=basename,
        search_results=search_results,
    )
    append_trace(
        context.agent_trace,
        agent="retriever",
        action="semantic_vector_search_for_modification",
        input_summary=f"query={context.retriever_output.get('query')}",
        output_summary=f"retrieved={len(context.retriever_output.get('retrieved_evidences', []))}",
    )

    context.verifier_output = run_verifier_executor_agent(
        context.retriever_output.get("retrieved_evidences", [])
    )
    append_trace(
        context.agent_trace,
        agent="verifier_executor",
        action="verify_evidences_before_modification",
        input_summary=f"candidate_evidences={len(context.retriever_output.get('retrieved_evidences', []))}",
        output_summary=(
            f"status={context.verifier_output.get('verification_status')}; "
            f"verified={len(context.verifier_output.get('verified_evidences', []))}"
        ),
    )

    executor_output = run_modification_executor_agent(
        request=request,
        input_file=input_file,
        snapshot_path=snapshot_path,
        index_dir=index_dir,
        basename=basename,
        output_dir=output_dir,
        top_k=top_k,
        verified_evidences=context.verifier_output.get("verified_evidences", []),
        index_config=index_config,
    )
    append_trace(
        context.agent_trace,
        agent="modification_executor",
        action="apply_controlled_xlsx_modification",
        input_summary=f"verified_evidences={len(context.verifier_output.get('verified_evidences', []))}",
        output_summary=(
            f"status={executor_output.get('modification_status')}; "
            f"modified_file={executor_output.get('modified_file')}"
        ),
    )

    final_response = build_multiagent_modification_response(
        request=request,
        planner_output=context.planner_output,
        retriever_output=context.retriever_output,
        verifier_output=context.verifier_output,
        executor_output=executor_output,
        agent_trace=context.agent_trace,
        index_config=index_config,
        index_dir=index_dir,
        basename=basename,
    )
    context.final_response = final_response
    return final_response


def build_multiagent_modification_response(
    request: str,
    planner_output: dict,
    retriever_output: dict,
    verifier_output: dict,
    executor_output: dict,
    agent_trace: list[dict],
    index_config: dict,
    index_dir: str,
    basename: str,
) -> dict:
    report = executor_output.get("modification_report", {})
    return {
        "request": request,
        "architecture": ARCHITECTURE_NAME,
        "status": executor_output.get("modification_status"),
        "intent": planner_output.get("intent"),
        "retrieval_plan": planner_output.get("retrieval_plan", {}),
        "evidence_count": len(retriever_output.get("retrieved_evidences", [])),
        "verified_evidence_count": len(verifier_output.get("verified_evidences", [])),
        "modified_file": executor_output.get("modified_file"),
        "change_plan": executor_output.get("change_plan", {}),
        "guard_report": executor_output.get("guard_report", {}),
        "structural_diff": executor_output.get("structural_diff", {}),
        "effect_verification": executor_output.get("effect_verification")
        or report.get("effect_verification", {}),
        "verification_report": verifier_output,
        "agent_trace": agent_trace,
        "modification_report": report,
        "execution_metadata": {
            "architecture": ARCHITECTURE_NAME,
            "coordination": "hierarchical_orchestrated_modification_pipeline",
            "agents": [
                "router_planner",
                "retriever",
                "verifier_executor",
                "modification_executor",
            ],
            "retrieval_strategy": "semantic_vector_search",
            "modification_strategy": "deterministic_controlled_xlsx_edit",
            "embedding_model": index_config.get("embedding_model"),
            "embedding_provider": index_config.get("embedding_provider"),
            "similarity_metric": index_config.get("similarity_metric", "cosine"),
            "index_strategy": index_config.get("index_strategy"),
            "index_dir": index_dir,
            "basename": basename,
        },
    }


def _load_index_config(index_dir: str, basename: str) -> dict:
    _, _, config = load_index_artifacts(index_dir=index_dir, basename=basename)
    return config
