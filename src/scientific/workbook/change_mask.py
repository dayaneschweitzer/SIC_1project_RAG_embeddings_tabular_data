"""allowed_change_mask as a write guardrail. Never used to discover the operation."""

from __future__ import annotations

from typing import Any

from openpyxl.utils import column_index_from_string, get_column_letter, range_boundaries


def parse_change_mask(mask: Any) -> dict[str, Any]:
    if not mask:
        return {
            "unrestricted": True,
            "scope_type": None,
            "sheets": set(),
            "cells": set(),
            "ranges": [],
            "insert_row_at": None,
            "insert_column_at": None,
            "unmerge": [],
            "allow_new_sheet": None,
            "source_sheet": None,
        }
    if isinstance(mask, str):
        cells = {item.strip().upper() for item in mask.replace(";", ",").split(",") if item.strip()}
        return {
            "unrestricted": False,
            "scope_type": "cells",
            "sheets": set(),
            "cells": cells,
            "ranges": [],
            "insert_row_at": None,
            "insert_column_at": None,
            "unmerge": [],
            "allow_new_sheet": None,
            "source_sheet": None,
        }
    sheets = {str(item) for item in mask.get("sheets") or [] if item}
    cells = {str(item).upper() for item in mask.get("cells") or [] if item}
    ranges = [str(item).upper() for item in mask.get("ranges") or [] if item]
    unmerge = [str(item).upper() for item in mask.get("unmerge") or [] if item]
    return {
        "unrestricted": False,
        "scope_type": mask.get("scope_type"),
        "sheets": sheets,
        "cells": cells,
        "ranges": ranges,
        "insert_row_at": mask.get("insert_row_at"),
        "insert_column_at": mask.get("insert_column_at"),
        "unmerge": unmerge,
        "allow_new_sheet": mask.get("allow_new_sheet"),
        "source_sheet": mask.get("source_sheet"),
    }


def coordinate_in_range(coordinate: str, cell_range: str) -> bool:
    try:
        col, row, _, _ = range_boundaries(str(coordinate).upper())
        min_col, min_row, max_col, max_row = range_boundaries(str(cell_range).upper())
    except ValueError:
        return False
    return min_row <= row <= max_row and min_col <= col <= max_col


def cell_allowed(sheet_name: str, coordinate: str, mask: Any) -> bool:
    parsed = parse_change_mask(mask)
    if parsed["unrestricted"]:
        return True
    if parsed["sheets"] and sheet_name not in parsed["sheets"]:
        return False
    wanted = str(coordinate).upper()
    if parsed["cells"] and wanted in parsed["cells"]:
        return True
    if any(coordinate_in_range(wanted, item) for item in parsed["ranges"]):
        return True
    if parsed["cells"] or parsed["ranges"]:
        return False
    return True


def plan_mask_errors(change_plan: dict[str, Any], mask: Any) -> list[str]:
    parsed = parse_change_mask(mask)
    if parsed["unrestricted"]:
        return []
    operation = change_plan.get("operation")
    sheet = change_plan.get("target_sheet")
    errors: list[str] = []

    if operation == "copy_sheet":
        new_name = change_plan.get("new_sheet_name")
        if parsed["allow_new_sheet"] and new_name != parsed["allow_new_sheet"]:
            errors.append("copy_sheet fora de allowed_change_mask.")
        if parsed["source_sheet"] and sheet != parsed["source_sheet"]:
            errors.append("aba origem fora de allowed_change_mask.")
        return errors

    if parsed["sheets"] and sheet not in parsed["sheets"]:
        if operation == "copy_sheet":
            return errors
        errors.append(f"aba {sheet} fora de allowed_change_mask.")
        return errors

    if operation == "delete_row":
        errors.append("delete_row with allowed_change_mask is not qualified")
    if operation == "insert_row":
        position = change_plan.get("insertion_position")
        if parsed["insert_row_at"] is not None and int(parsed["insert_row_at"]) != int(position):
            errors.append("insert_row fora de allowed_change_mask.")
        return errors

    if operation == "insert_column":
        position = change_plan.get("insertion_position")
        allowed = parsed["insert_column_at"]
        if allowed is not None and not _same_column(position, allowed):
            errors.append("insert_column fora de allowed_change_mask.")
        return errors

    if operation == "unmerge_range":
        rng = str(change_plan.get("source_range") or "").upper()
        allowed_unmerge = {str(item).upper() for item in parsed["unmerge"]} | set(parsed["ranges"])
        if allowed_unmerge and rng not in allowed_unmerge:
            errors.append("unmerge fora de allowed_change_mask.")
        return errors

    if operation == "sort_range":
        rng = str(change_plan.get("sort_range") or change_plan.get("source_range") or "").upper()
        if parsed["ranges"] and rng not in parsed["ranges"]:
            errors.append("sort_range fora de allowed_change_mask.")
        return errors

    if operation == "replace_values":
        rng = str(change_plan.get("source_range") or "").upper()
        if parsed["ranges"] and rng not in parsed["ranges"] and not any(
            coordinate_in_range(cell, rng) for cell in parsed["cells"]
        ):
            errors.append("replace_values fora de allowed_change_mask.")
        return errors

    dest = change_plan.get("destination") or change_plan.get("target_coordinate")
    targets = list(change_plan.get("target_cells") or [])
    if dest:
        targets.append(dest)
    for coordinate in targets:
        if coordinate and not cell_allowed(sheet, str(coordinate), mask):
            errors.append(f"celula {coordinate} fora de allowed_change_mask.")
    return errors


def _same_column(position: Any, allowed: Any) -> bool:
    if position is None or allowed is None:
        return False
    try:
        left = int(position) if str(position).isdigit() else column_index_from_string(str(position))
        right = int(allowed) if str(allowed).isdigit() else column_index_from_string(str(allowed))
    except ValueError:
        return str(position).upper() == str(allowed).upper()
    return left == right


def column_position_to_index(position: Any) -> int:
    if isinstance(position, int):
        return position
    text = str(position).strip()
    if text.isdigit():
        return int(text)
    return column_index_from_string(text.upper())


def column_index_to_letter(index: int) -> str:
    return get_column_letter(index)
