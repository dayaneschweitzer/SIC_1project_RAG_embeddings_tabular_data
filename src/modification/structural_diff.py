from __future__ import annotations

import hashlib
import math

from ingestion.semantic_style import semantic_style_signature


def build_structural_diff(
    original_snapshot: dict,
    modified_snapshot: dict,
    change_plan: dict,
    original_file: str | None = None,
    modified_file: str | None = None,
) -> dict:
    original_cells = _cell_lookup(original_snapshot)
    modified_cells = _cell_lookup(modified_snapshot)
    changed_cells = []
    added_cells = []
    removed_cells = []
    formulas_changed = []
    styles_changed = []

    for key, original_cell in original_cells.items():
        modified_cell = modified_cells.get(key)
        if not modified_cell:
            if _is_semantically_empty_cell(original_cell):
                continue
            removed_cells.append(_cell_key_to_dict(key, original_cell))
            continue

        if not _values_equivalent(original_cell, modified_cell):
            changed_cells.append(
                {
                    "sheet_name": key[0],
                    "coordinate": key[1],
                    "before_value": original_cell.get("value"),
                    "after_value": modified_cell.get("value"),
                }
            )

        if bool(original_cell.get("is_formula")) != bool(modified_cell.get("is_formula")):
            formulas_changed.append(
                {
                    "sheet_name": key[0],
                    "coordinate": key[1],
                    "before_is_formula": bool(original_cell.get("is_formula")),
                    "after_is_formula": bool(modified_cell.get("is_formula")),
                }
            )

        if semantic_style_signature(original_cell) != semantic_style_signature(modified_cell):
            styles_changed.append(
                {
                    "sheet_name": key[0],
                    "coordinate": key[1],
                    "before_style": semantic_style_signature(original_cell),
                    "after_style": semantic_style_signature(modified_cell),
                    "before_style_id": original_cell.get("style_id"),
                    "after_style_id": modified_cell.get("style_id"),
                }
            )

    for key, modified_cell in modified_cells.items():
        if key not in original_cells:
            if _is_semantically_empty_cell(modified_cell):
                continue
            added_cells.append(_cell_key_to_dict(key, modified_cell))

    target_cells = {
        (change_plan.get("target_sheet"), coordinate)
        for coordinate in change_plan.get("target_cells", [])
    }
    unexpected_value_changes = [
        change
        for change in changed_cells
        if (change["sheet_name"], change["coordinate"]) not in target_cells
    ]

    structural_preservation = {
        "sheet_names_preserved": _sheet_names(original_snapshot) == _sheet_names(modified_snapshot),
        "merged_cells_preserved": _merged_cells(original_snapshot) == _merged_cells(modified_snapshot),
        "tables_preserved": _tables(original_snapshot) == _tables(modified_snapshot),
        "validations_count_preserved": _validation_counts(original_snapshot) == _validation_counts(modified_snapshot),
        "conditional_formatting_count_preserved": (
            _conditional_counts(original_snapshot) == _conditional_counts(modified_snapshot)
        ),
        "unexpected_value_changes": len(unexpected_value_changes),
        "formula_changes": len(formulas_changed),
        "style_changes": len(styles_changed),
    }

    return {
        "original_file": original_file,
        "modified_file": modified_file,
        "original_sha256": calculate_file_hash(original_file) if original_file else None,
        "modified_sha256": calculate_file_hash(modified_file) if modified_file else None,
        "changed_cells": changed_cells,
        "added_cells": added_cells,
        "removed_cells": removed_cells,
        "formulas_changed": formulas_changed,
        "styles_changed": styles_changed,
        "unexpected_value_changes": unexpected_value_changes,
        "structural_preservation": structural_preservation,
        "fidelity_status": "passed" if _fidelity_passed(structural_preservation, added_cells, removed_cells) else "warning",
    }


def calculate_file_hash(file_path: str) -> str:
    sha256 = hashlib.sha256()
    with open(file_path, "rb") as file:
        for block in iter(lambda: file.read(4096), b""):
            sha256.update(block)
    return sha256.hexdigest()


def _cell_lookup(snapshot: dict) -> dict[tuple[str, str], dict]:
    lookup = {}
    for sheet in snapshot.get("sheets", []):
        sheet_name = sheet.get("sheet_name")
        for cell in sheet.get("cells", []):
            lookup[(sheet_name, cell.get("coordinate"))] = cell
    return lookup


def _cell_key_to_dict(key: tuple[str, str], cell: dict) -> dict:
    return {
        "sheet_name": key[0],
        "coordinate": key[1],
        "value": cell.get("value"),
    }


def _style_signature(cell: dict) -> dict:
    """Semantic style only. style_id is diagnostic and is not part of identity."""
    return semantic_style_signature(cell)


def _is_semantically_empty_cell(cell: dict) -> bool:
    value = cell.get("value")
    return (
        value in (None, "")
        and not cell.get("is_formula")
    )


def _values_equivalent(original_cell: dict, modified_cell: dict) -> bool:
    original_value = original_cell.get("value")
    modified_value = modified_cell.get("value")
    if original_value == modified_value:
        return True

    if str(original_value) == str(modified_value):
        return True

    if _looks_numeric_cell(original_cell) and _looks_numeric_cell(modified_cell):
        try:
            return math.isclose(
                float(original_value),
                float(modified_value),
                rel_tol=1e-12,
                abs_tol=1e-12,
            )
        except (TypeError, ValueError):
            return False

    return False


def _looks_numeric_cell(cell: dict) -> bool:
    value = cell.get("value")
    data_type = str(cell.get("data_type") or "").lower()
    if data_type == "n":
        return value not in (None, "")
    if isinstance(value, bool):
        return False
    if isinstance(value, (int, float)):
        return True
    try:
        float(str(value))
        return True
    except (TypeError, ValueError):
        return False


def _sheet_names(snapshot: dict) -> list[str]:
    return [sheet.get("sheet_name") for sheet in snapshot.get("sheets", [])]


def _merged_cells(snapshot: dict) -> dict[str, list[str]]:
    return {sheet.get("sheet_name"): sheet.get("merged_cells", []) for sheet in snapshot.get("sheets", [])}


def _tables(snapshot: dict) -> dict[str, list[dict]]:
    return {sheet.get("sheet_name"): sheet.get("tables", []) for sheet in snapshot.get("sheets", [])}


def _validation_counts(snapshot: dict) -> dict[str, int]:
    return {
        sheet.get("sheet_name"): int(sheet.get("data_validations_count", 0))
        for sheet in snapshot.get("sheets", [])
    }


def _conditional_counts(snapshot: dict) -> dict[str, int]:
    return {
        sheet.get("sheet_name"): int(sheet.get("conditional_formatting_count", 0))
        for sheet in snapshot.get("sheets", [])
    }


def _fidelity_passed(structural_preservation: dict, added_cells: list, removed_cells: list) -> bool:
    return (
        structural_preservation["sheet_names_preserved"]
        and structural_preservation["merged_cells_preserved"]
        and structural_preservation["tables_preserved"]
        and structural_preservation["validations_count_preserved"]
        and structural_preservation["conditional_formatting_count_preserved"]
        and structural_preservation["unexpected_value_changes"] == 0
        and structural_preservation["formula_changes"] == 0
        and not added_cells
        and not removed_cells
    )
