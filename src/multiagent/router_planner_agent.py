from __future__ import annotations

import re


AGENT_NAME = "router_planner"


def run_router_planner(question: str, top_k: int = 5) -> dict:
    intent = detect_intent(question)
    preferred_block_types = preferred_block_types_for_intent(intent)

    return {
        "agent": AGENT_NAME,
        "question": question,
        "intent": intent,
        "retrieval_plan": {
            "query": normalize_query(question),
            "top_k": top_k,
            "preferred_block_types": preferred_block_types,
        },
        "rationale": rationale_for_intent(intent),
    }


def detect_intent(question: str) -> str:
    text = normalize_query(question)

    if _contains_any(text, ["field", "fields", "column", "columns", "header", "headers", "name", "address"]):
        return "field_lookup"

    if _contains_any(text, ["value", "values", "amount", "total", "code", "customer"]):
        return "value_lookup"

    if _contains_any(text, ["structure", "range", "sheet", "row", "rows", "table", "layout"]):
        return "structure_lookup"

    if _contains_any(text, ["metadata", "formula", "formulas", "validation", "merged", "format"]):
        return "metadata_lookup"

    return "general_table_question"


def preferred_block_types_for_intent(intent: str) -> list[str]:
    mapping = {
        "field_lookup": ["header_context", "column_block", "row_block"],
        "value_lookup": ["row_block", "column_block", "used_range"],
        "structure_lookup": ["sheet_overview", "used_range", "header_context"],
        "metadata_lookup": ["sheet_overview", "cell_reference_block", "used_range"],
        "general_table_question": ["row_block", "column_block", "used_range"],
    }
    return mapping.get(intent, mapping["general_table_question"])


def rationale_for_intent(intent: str) -> str:
    rationales = {
        "field_lookup": "A pergunta contem termos associados a campos, cabecalhos ou colunas.",
        "value_lookup": "A pergunta contem termos associados a valores ou registros da tabela.",
        "structure_lookup": "A pergunta solicita informacao sobre organizacao estrutural da planilha.",
        "metadata_lookup": "A pergunta aponta para metadados, formulas, validacoes ou formatacao.",
        "general_table_question": "A pergunta nao acionou heuristicas especificas; sera tratada como consulta geral.",
    }
    return rationales.get(intent, rationales["general_table_question"])


def normalize_query(question: str) -> str:
    return " ".join(str(question).strip().split())


def _contains_any(text: str, words: list[str]) -> bool:
    lowered = text.lower()
    return any(re.search(rf"\b{re.escape(word)}\b", lowered) for word in words)
