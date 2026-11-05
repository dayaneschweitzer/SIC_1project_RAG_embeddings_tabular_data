from __future__ import annotations

from dataclasses import dataclass, asdict

from .parsed_intent import (
    COPY_SHEET,
    INSERT_COLUMN,
    INSERT_ROW,
    PARSE_AMBIGUOUS,
    PLAN_FAILED,
    PLAN_PLANNED,
    REPLACE_VALUES,
    SORT_RANGE,
    STRUCTURED_OPERATIONS,
    UNMERGE_RANGE,
    WRITE_FORMULA,
    WRITE_VALUE,
)


@dataclass
class ChangePlan:
    operation: str
    target_sheet: str
    target_range: str
    target_cells: list[str]
    new_value: str
    original_values: dict[str, str | None]
    evidence_block_ids: list[str]
    evidence_summary: list[dict]
    preserve_style: bool = True
    preserve_formula: bool = True
    status: str = "planned"
    warnings: list[str] | None = None

    def to_dict(self) -> dict:
        data = asdict(self)
        data["warnings"] = data["warnings"] or []
        return data


def build_change_plan(intent: dict, evidences: list[dict], snapshot: dict) -> dict:
    evidences = evidences or []
    snapshot = snapshot or {}

    if intent.get("parse_status") == PARSE_AMBIGUOUS:
        return {
            "status": PLAN_FAILED,
            "errors": ["Intencao ambigua; plano nao gerado."],
            "intent": intent,
        }

    operation = intent.get("operation")
    if operation in STRUCTURED_OPERATIONS:
        return _build_structured_plan(intent)

    if operation != "update_cell":
        return {
            "status": PLAN_FAILED,
            "errors": ["Operacao nao suportada para plano de modificacao."],
            "intent": intent,
        }

    new_value = intent.get("new_value")
    if not new_value:
        return {
            "status": "failed",
            "errors": ["Novo valor ausente."],
            "intent": intent,
        }

    target = find_target_cell(intent=intent, evidences=evidences, snapshot=snapshot)
    if not target:
        return {
            "status": "failed",
            "errors": ["Celula alvo nao encontrada nas evidencias nem no snapshot."],
            "intent": intent,
        }

    coordinate = target["coordinate"]
    evidence_block_ids = _evidence_block_ids(evidences, coordinate)
    plan = ChangePlan(
        operation="update_cell",
        target_sheet=target["sheet_name"],
        target_range=coordinate,
        target_cells=[coordinate],
        new_value=str(new_value),
        original_values={coordinate: target.get("value")},
        evidence_block_ids=evidence_block_ids,
        evidence_summary=_summarize_evidences(evidences, coordinate),
        warnings=[],
    )
    payload = plan.to_dict()
    payload["status"] = PLAN_PLANNED
    return payload


def _build_structured_plan(intent: dict) -> dict:
    errors = _structured_plan_errors(intent)
    operation = intent.get("operation")
    destination = intent.get("destination") or intent.get("target_coordinate")
    target_cells = [destination] if destination else []
    target_range = (
        intent.get("source_range")
        or intent.get("sort_range")
        or destination
        or ""
    )
    plan = {
        "status": PLAN_PLANNED if not errors else PLAN_FAILED,
        "operation": operation,
        "target_sheet": intent.get("target_sheet"),
        "target_range": target_range,
        "target_cells": target_cells,
        "destination": destination,
        "source_cells": list(intent.get("source_cells") or []),
        "source_range": intent.get("source_range"),
        "literal_value": intent.get("literal_value"),
        "new_value": intent.get("new_value") or intent.get("literal_value"),
        "formula": intent.get("formula"),
        "expression": intent.get("expression"),
        "replacement_old": intent.get("replacement_old"),
        "replacement_new": intent.get("replacement_new"),
        "sort_range": intent.get("sort_range"),
        "sort_key": intent.get("sort_key"),
        "sort_order": intent.get("sort_order"),
        "insertion_position": intent.get("insertion_position"),
        "insertion_axis": intent.get("insertion_axis"),
        "new_sheet_name": intent.get("new_sheet_name"),
        "extract_length": intent.get("extract_length"),
        "separator": intent.get("separator"),
        "original_values": {},
        "evidence_block_ids": [],
        "evidence_summary": [],
        "preserve_style": True,
        "preserve_formula": True,
        "warnings": list(intent.get("warnings") or []),
        "errors": errors,
        "apply_xlsx": False,
    }
    if errors:
        plan["intent"] = intent
    return plan


def _structured_plan_errors(intent: dict) -> list[str]:
    operation = intent.get("operation")
    errors: list[str] = []

    if operation != COPY_SHEET and not intent.get("target_sheet"):
        errors.append("Aba alvo ausente.")

    if operation == REPLACE_VALUES:
        if not intent.get("source_range"):
            errors.append("Intervalo de substituicao ausente.")
        if intent.get("replacement_old") in (None, ""):
            errors.append("Valor antigo ausente.")
        if intent.get("replacement_new") in (None, ""):
            errors.append("Valor novo ausente.")
    elif operation == WRITE_VALUE:
        if not (intent.get("destination") or intent.get("target_coordinate")):
            errors.append("Destino ausente.")
        expression = intent.get("expression")
        sources = list(intent.get("source_cells") or [])
        if expression == "concatenate" and len(sources) < 2:
            errors.append("Concatenacao exige duas celulas fonte.")
        elif expression == "extract_token" and len(sources) < 1:
            errors.append("Extracao exige celula fonte.")
        elif expression == "extract_prefix":
            if len(sources) < 1:
                errors.append("Extracao exige celula fonte.")
            if not intent.get("extract_length"):
                errors.append("Comprimento do prefixo ausente.")
        elif expression == "lookup" and len(sources) < 1:
            errors.append("Lookup exige celula chave.")
        elif expression == "add" and len(sources) < 2:
            errors.append("Soma constante exige duas celulas fonte.")
        elif expression == "layout_caption" and not (intent.get("literal_value") or intent.get("new_value")):
            errors.append("Caption ausente.")
        elif expression is None and not (intent.get("literal_value") or intent.get("new_value")):
            errors.append("Valor literal ausente.")
    elif operation == WRITE_FORMULA:
        if not (intent.get("destination") or intent.get("target_coordinate")):
            errors.append("Destino ausente.")
        if not intent.get("formula"):
            errors.append("Formula ausente.")
    elif operation == INSERT_ROW:
        if intent.get("insertion_position") in (None, ""):
            errors.append("Indice de linha ausente.")
    elif operation == INSERT_COLUMN:
        if intent.get("insertion_position") in (None, ""):
            errors.append("Indice de coluna ausente.")
    elif operation == SORT_RANGE:
        if not intent.get("sort_range"):
            errors.append("Intervalo de ordenacao ausente.")
        if not intent.get("sort_key"):
            errors.append("Chave de ordenacao ausente.")
    elif operation == UNMERGE_RANGE:
        if not intent.get("source_range"):
            errors.append("Intervalo a desmesclar ausente.")
    elif operation == COPY_SHEET:
        if not intent.get("target_sheet"):
            errors.append("Aba origem ausente.")
        if not intent.get("new_sheet_name"):
            errors.append("Nome da nova aba ausente.")
    else:
        errors.append("Operacao estruturada desconhecida.")
    return errors


def find_target_cell(intent: dict, evidences: list[dict], snapshot: dict) -> dict | None:
    target_sheet = intent.get("target_sheet")
    target_coordinate = intent.get("target_coordinate")
    target_value = intent.get("target_value")

    if target_coordinate:
        found = _find_cell_by_coordinate_in_evidences(target_coordinate, evidences, target_sheet)
        if found:
            return found

        return _find_cell_by_coordinate_in_snapshot(target_coordinate, snapshot, target_sheet)

    if target_value:
        found = _find_cell_by_value_in_evidences(target_value, evidences, target_sheet)
        if found:
            return found

        return _find_cell_by_value_in_snapshot(target_value, snapshot, target_sheet)

    return None


def _find_cell_by_coordinate_in_evidences(
    coordinate: str, evidences: list[dict], target_sheet: str | None = None
) -> dict | None:
    wanted = str(coordinate).upper()
    for evidence in evidences:
        sheet_name = evidence.get("sheet_name")
        if target_sheet and sheet_name != target_sheet:
            continue
        for cell in evidence.get("cells", []):
            if not isinstance(cell, dict):
                continue
            if str(cell.get("coordinate", "")).upper() == wanted:
                return _cell_with_sheet(cell, sheet_name)
    return None


def _find_cell_by_value_in_evidences(
    value: str, evidences: list[dict], target_sheet: str | None = None
) -> dict | None:
    wanted = _normalize_value(value)
    for evidence in evidences:
        sheet_name = evidence.get("sheet_name")
        if target_sheet and sheet_name != target_sheet:
            continue
        for cell in evidence.get("cells", []):
            if not isinstance(cell, dict):
                continue
            if _normalize_value(cell.get("value")) == wanted:
                return _cell_with_sheet(cell, sheet_name)
    return None


def _find_cell_by_coordinate_in_snapshot(
    coordinate: str, snapshot: dict, target_sheet: str | None = None
) -> dict | None:
    wanted = str(coordinate).upper()
    for sheet in snapshot.get("sheets", []):
        if target_sheet and sheet.get("sheet_name") != target_sheet:
            continue
        for cell in sheet.get("cells", []):
            if str(cell.get("coordinate", "")).upper() == wanted:
                return _cell_with_sheet(cell, sheet.get("sheet_name"))
    return None


def _find_cell_by_value_in_snapshot(
    value: str, snapshot: dict, target_sheet: str | None = None
) -> dict | None:
    wanted = _normalize_value(value)
    for sheet in snapshot.get("sheets", []):
        if target_sheet and sheet.get("sheet_name") != target_sheet:
            continue
        for cell in sheet.get("cells", []):
            if _normalize_value(cell.get("value")) == wanted:
                return _cell_with_sheet(cell, sheet.get("sheet_name"))
    return None


def _cell_with_sheet(cell: dict, sheet_name: str | None) -> dict:
    copied = dict(cell)
    copied["sheet_name"] = sheet_name
    return copied


def _evidence_block_ids(evidences: list[dict], coordinate: str) -> list[str]:
    block_ids = []
    wanted = str(coordinate).upper()
    for evidence in evidences:
        for cell in evidence.get("cells", []):
            if not isinstance(cell, dict):
                continue
            if str(cell.get("coordinate", "")).upper() == wanted and evidence.get("block_id"):
                block_ids.append(evidence["block_id"])
                break

    return _deduplicate(block_ids)


def _summarize_evidences(evidences: list[dict], coordinate: str) -> list[dict]:
    summaries = []
    wanted = str(coordinate).upper()
    for evidence in evidences:
        evidence_cells = [
            cell.get("coordinate")
            for cell in evidence.get("cells", [])
            if isinstance(cell, dict)
        ]
        if wanted not in {str(cell).upper() for cell in evidence_cells}:
            continue
        summaries.append(
            {
                "rank": evidence.get("rank"),
                "score": evidence.get("score"),
                "block_id": evidence.get("block_id"),
                "block_type": evidence.get("block_type"),
                "sheet_name": evidence.get("sheet_name"),
                "range": evidence.get("range"),
                "cells": evidence_cells,
            }
        )

    return summaries


def _normalize_value(value) -> str:
    return " ".join(str(value or "").strip().lower().split())


def _deduplicate(values: list[str]) -> list[str]:
    seen = set()
    result = []
    for value in values:
        if value in seen:
            continue
        seen.add(value)
        result.append(value)
    return result
