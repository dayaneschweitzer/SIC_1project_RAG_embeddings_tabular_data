from __future__ import annotations

from hashlib import sha256
import json

import numpy as np


CONDITIONS = {
    "C1-E1": ("C1", "E1_MINILM"),
    "C1-E2": ("C1", "E2_TABERTA"),
    "C2-E1": ("C2", "E1_MINILM"),
    "C2-E2": ("C2", "E2_TABERTA"),
}
TOP_K = 5
CONTEXT_BUDGET = 5


def canonical_hash(value):
    payload = json.dumps(value, ensure_ascii=False, sort_keys=True, separators=(",", ":"), allow_nan=False).encode("utf8")
    return sha256(payload).hexdigest()


def _document_id(hit):
    metadata = hit.get("metadata") or {}
    return metadata.get("block_id") or metadata.get("document_id") or metadata.get("id")


def _score(hit):
    for key in ("score", "similarity", "cosine_similarity"):
        if key in hit:
            return float(hit[key])
    raise ValueError("RETRIEVAL_HIT_SCORE_MISSING")


def _build_plan(instruction, architecture):
    from scientific.workbook.native_interpreter import normalize_request, interpret_native_task, build_native_queries

    if architecture == "C1":
        return ({
            "queries": [normalize_request(instruction)],
            "schema_version": "frozen_structured_C1_global",
            "architecture_id": "C1",
            "subobjectives": [],
        }, None)
    task = interpret_native_task(instruction)
    plan = build_native_queries(task, "C2").to_dict()
    if plan.get("architecture_id") != "C2":
        raise ValueError("C2_PLANNER_DEGRADED_TO_C1")
    return plan, task


def prepare_retrieval(
    instruction,
    documents,
    vectors,
    encode_queries,
    condition,
    threshold,
    *,
    query_plan=None,
    index_factory=None,
    policy_function=None,
    aggregate_function=None,
    evidence_function=None,
    objective_selector=None,
):
    if condition not in CONDITIONS:
        raise ValueError("UNKNOWN_CONDITION")
    architecture, encoder_id = CONDITIONS[condition]
    task = None
    if query_plan is None:
        query_plan, task = _build_plan(instruction, architecture)
    if query_plan.get("architecture_id") != architecture:
        raise ValueError("ARCHITECTURE_QUERY_PLAN_MISMATCH")
    queries = list(query_plan.get("queries") or [])
    if architecture == "C1" and len(queries) != 1:
        raise ValueError("C1_MUST_HAVE_ONE_DIRECT_QUERY")
    if not queries:
        raise ValueError("EMPTY_QUERY_PLAN")
    if index_factory is None:
        from scientific.paired_index import ExperimentalIndex
        index_factory = ExperimentalIndex
    if policy_function is None:
        from scientific.retrieval_policy import apply_retrieval_policy
        policy_function = apply_retrieval_policy
    if aggregate_function is None:
        from scientific.retrieval_helpers import _aggregate
        aggregate_function = _aggregate
    if evidence_function is None:
        from scientific.evidence import evidences_from_search_results
        evidence_function = evidences_from_search_results

    encoded = encode_queries(queries)
    index = index_factory(encoder_id=encoder_id, embeddings=np.asarray(vectors).tolist(), metadata=documents)
    candidates = []
    traces = []
    for query_index, (query, vector) in enumerate(zip(queries, encoded)):
        hits = list(index.search(np.asarray(vector).tolist(), TOP_K))
        policy = policy_function(hits, top_k=TOP_K, threshold=threshold)
        accepted_ids = {_document_id(hit) for hit in policy.accepted}
        query_id = f"{architecture.lower()}_q{query_index + 1}"
        rows = []
        for rank, hit in enumerate(hits, 1):
            document_id = _document_id(hit)
            rows.append({
                "document_id": document_id,
                "score": _score(hit),
                "rank": rank,
                "accepted_by_threshold": document_id in accepted_ids,
                "query_id": query_id,
            })
        traces.append({
            "query_id": query_id,
            "query": query,
            "query_sha256": sha256(query.encode("utf8")).hexdigest(),
            "provenance": "direct_instruction" if architecture == "C1" else "functional_subobjective",
            "subobjective": (query_plan.get("subobjectives") or [None] * len(queries))[query_index] if query_index < len(query_plan.get("subobjectives") or []) else None,
            "candidates_before_threshold": rows,
            "accepted_document_ids": [row["document_id"] for row in rows if row["accepted_by_threshold"]],
        })
        candidates.extend((query_index, hit) for hit in policy.accepted)

    selected = aggregate_function(candidates) if architecture == "C2" else [hit for _, hit in candidates]
    selection = None
    if objective_selector is not None:
        selected, selection = objective_selector(selected, CONTEXT_BUDGET)
    elif task is not None:
        from scientific.evidence_objectives import derive_evidence_requirements, select_objective_evidence
        requirements = derive_evidence_requirements(task, [hit["metadata"] for hit in selected])
        selected, selection = select_objective_evidence(selected, requirements, CONTEXT_BUDGET)
    else:
        selected = selected[:CONTEXT_BUDGET]
    evidence = evidence_function(selected)
    for item, hit in zip(evidence, selected):
        if not item.get("block_id"):
            item["block_id"] = _document_id(hit)
        item["score"] = _score(hit)
        item["rank"] = next(
            row["rank"]
            for trace in traces
            for row in trace["candidates_before_threshold"]
            if row["document_id"] == item["block_id"]
        )
    vector_by_id = {document["block_id"]: vector for document, vector in zip(documents, np.asarray(vectors))}
    if selected:
        context = np.mean([vector_by_id[_document_id(hit)] for hit in selected], axis=0).astype(np.float32)
    else:
        context = np.zeros(np.asarray(vectors).shape[1], np.float32)
    final_ids = [item["block_id"] for item in evidence]
    final_text = [item.get("text", item.get("retrieval_text", "")) for item in evidence]
    final_context = {
        "document_ids": final_ids,
        "document_count": len(final_ids),
        "token_count_whitespace_proxy": sum(len(text.split()) for text in final_text),
        "context_sha256": canonical_hash({"document_ids": final_ids, "text": final_text}),
        "budget": CONTEXT_BUDGET,
    }
    return {
        "schema_version": "sic_retrieval_trace_v2",
        "architecture": architecture,
        "encoder": encoder_id,
        "query_count": len(queries),
        "query_plan": query_plan,
        "decomposition_applied": architecture == "C2" and len(queries) > 1,
        "retrieval_traces": traces,
        "threshold": float(threshold),
        "top_k": TOP_K,
        "context_budget": CONTEXT_BUDGET,
        "evidence": evidence,
        "context": context,
        "context_selection": selection,
        "final_context": final_context,
    }


def assert_comparable_traces(left, right):
    shared = ("encoder", "top_k", "context_budget")
    if any(left.get(key) != right.get(key) for key in shared):
        raise ValueError("C1_C2_SHARED_INVARIANT_MISMATCH")
    if left.get("architecture") != "C1" or right.get("architecture") != "C2":
        raise ValueError("C1_C2_ARCHITECTURE_MISMATCH")
    if left.get("query_count") != 1:
        raise ValueError("C1_NOT_DIRECT")
    return True
