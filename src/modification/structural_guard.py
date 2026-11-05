from __future__ import annotations

from openpyxl.utils import range_boundaries

from .parsed_intent import COPY_SHEET, STRUCTURED_OPERATIONS, UPDATE_CELL


def validate_change_plan(
    change_plan: dict,
    snapshot: dict,
    allow_formula_update: bool = False,
    allow_merged_update: bool = False,
) -> dict:
    operation = change_plan.get("operation")
    if operation != UPDATE_CELL:
        return _validate_structured_plan(change_plan, snapshot)

    errors = []
    warnings = []
    checks = {
        "has_supported_operation": True,
        "has_target_sheet": bool(change_plan.get("target_sheet")),
        "has_target_cells": bool(change_plan.get("target_cells")),
        "target_cells_exist": True,
        "range_within_sheet_bounds": True,
        "does_not_modify_formula": True,
        "does_not_modify_merged_cell": True,
        "preserves_original_file": True,
    }

    sheet = _find_sheet(snapshot, change_plan.get("target_sheet"))
    if not sheet:
        checks["has_target_sheet"] = False
        checks["target_cells_exist"] = False
        errors.append("Aba alvo nao existe no snapshot.")
        return _report(checks, errors, warnings)

    cell_lookup = {cell.get("coordinate"): cell for cell in sheet.get("cells", [])}
    merged_ranges = sheet.get("merged_cells", [])

    for coordinate in change_plan.get("target_cells", []):
        cell = cell_lookup.get(coordinate)
        if not cell:
            checks["target_cells_exist"] = False
            errors.append(f"Celula alvo ausente no snapshot: {coordinate}")
            continue

        if not _coordinate_within_sheet(coordinate, sheet):
            checks["range_within_sheet_bounds"] = False
            errors.append(f"Celula fora dos limites da aba: {coordinate}")

        if cell.get("is_formula") and not allow_formula_update:
            checks["does_not_modify_formula"] = False
            errors.append(f"Alteracao de formula nao permitida: {coordinate}")

        containing_merged_ranges = _merged_ranges_for_coordinate(coordinate, merged_ranges)
        if containing_merged_ranges and not allow_merged_update:
            checks["does_not_modify_merged_cell"] = False
            errors.append(
                f"Alteracao de celula em regiao mesclada nao permitida: "
                f"{coordinate} em {containing_merged_ranges}"
            )

    if not change_plan.get("new_value"):
        errors.append("Novo valor vazio ou ausente.")

    return _report(checks, errors, warnings)


def _validate_structured_plan(change_plan: dict, snapshot: dict) -> dict:
    operation = change_plan.get("operation")
    errors = []
    warnings = []
    checks = {
        "has_supported_operation": operation in STRUCTURED_OPERATIONS,
        "has_target_sheet": bool(change_plan.get("target_sheet")),
        "has_target_cells": True,
        "target_cells_exist": True,
        "range_within_sheet_bounds": True,
        "does_not_modify_formula": True,
        "does_not_modify_merged_cell": True,
        "preserves_original_file": True,
    }
    if not checks["has_supported_operation"]:
        errors.append("Operacao nao suportada.")
        return _report(checks, errors, warnings)

    if operation == COPY_SHEET:
        if not _find_sheet(snapshot, change_plan.get("target_sheet")):
            checks["has_target_sheet"] = False
            errors.append("Aba alvo nao existe no snapshot.")
        if not change_plan.get("new_sheet_name"):
            errors.append("Nome da nova aba ausente.")
        return _report(checks, errors, warnings)

    sheet = _find_sheet(snapshot, change_plan.get("target_sheet"))
    if not sheet:
        checks["has_target_sheet"] = False
        errors.append("Aba alvo nao existe no snapshot.")
        return _report(checks, errors, warnings)

    if operation in {"write_value", "write_formula", "replace_values"}:
        dest = change_plan.get("destination") or change_plan.get("target_coordinate")
        targets = list(change_plan.get("target_cells") or [])
        if dest:
            targets.append(dest)
        for coordinate in targets:
            if coordinate and not _valid_a1(coordinate):
                checks["range_within_sheet_bounds"] = False
                errors.append(f"Celula invalida: {coordinate}")
        if operation == "write_formula" and not str(change_plan.get("formula") or "").startswith("="):
            errors.append("Formula ausente ou invalida.")
        if operation == "replace_values" and not change_plan.get("source_range"):
            errors.append("Intervalo de substituicao ausente.")
    elif operation in {"sort_range", "unmerge_range"}:
        rng = change_plan.get("sort_range") or change_plan.get("source_range")
        if not rng or not _valid_range_text(rng):
            checks["range_within_sheet_bounds"] = False
            errors.append("Intervalo invalido.")
    elif operation == "insert_row" and change_plan.get("insertion_position") in (None, ""):
        errors.append("Indice de linha ausente.")
    elif operation == "insert_column" and change_plan.get("insertion_position") in (None, ""):
        errors.append("Indice de coluna ausente.")

    return _report(checks, errors, warnings)


def _valid_a1(coordinate: str) -> bool:
    try:
        min_col, min_row, max_col, max_row = range_boundaries(str(coordinate).upper())
    except ValueError:
        return False
    return (min_col, min_row) == (max_col, max_row) and min_row >= 1 and min_col >= 1


def _valid_range_text(cell_range: str) -> bool:
    try:
        range_boundaries(str(cell_range).upper())
    except ValueError:
        return False
    return True


def _report(checks: dict, errors: list[str], warnings: list[str]) -> dict:
    return {
        "status": "passed" if not errors else "failed",
        "checks": checks,
        "errors": errors,
        "warnings": warnings,
    }


def _find_sheet(snapshot: dict, sheet_name: str | None) -> dict | None:
    for sheet in snapshot.get("sheets", []):
        if sheet.get("sheet_name") == sheet_name:
            return sheet
    return None


def _coordinate_within_sheet(coordinate: str, sheet: dict) -> bool:
    try:
        min_column, min_row, max_column, max_row = range_boundaries(coordinate)
    except ValueError:
        return False

    return (
        min_row >= 1
        and min_column >= 1
        and max_row <= int(sheet.get("max_row", 0))
        and max_column <= int(sheet.get("max_column", 0))
    )


def _merged_ranges_for_coordinate(coordinate: str, merged_ranges: list[str]) -> list[str]:
    if not merged_ranges:
        return []

    min_column, min_row, max_column, max_row = range_boundaries(coordinate)
    row = min_row
    column = min_column
    containing = []

    for merged_range in merged_ranges:
        range_min_col, range_min_row, range_max_col, range_max_row = range_boundaries(merged_range)
        if range_min_row <= row <= range_max_row and range_min_col <= column <= range_max_col:
            containing.append(merged_range)

    return containing
