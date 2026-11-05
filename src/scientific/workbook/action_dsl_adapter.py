
from __future__ import annotations

from openpyxl.utils import get_column_letter, range_boundaries

from .action_dsl import ActionDSL
from .parsed_intent import (
    COPY_SHEET,
    INSERT_COLUMN,
    INSERT_ROW,
    PLAN_PLANNED,
    REPLACE_VALUES,
    SORT_RANGE,
    UNMERGE_RANGE,
    UPDATE_CELL,
    WRITE_FORMULA,
    WRITE_VALUE,
)


def dsl_to_change_plan(action: ActionDSL) -> dict:
    arguments = action.arguments
    plan = {
        "status": PLAN_PLANNED,
        "operation": action.operation,
        "target_sheet": action.sheet,
        "target_range": "",
        "target_cells": [],
        "new_value": None,
        "original_values": {},
        "evidence_block_ids": [],
        "evidence_summary": [],
        "preserve_style": True,
        "preserve_formula": True,
        "warnings": [],
        "errors": [],
        "apply_xlsx": False,
    }

    if action.operation == UPDATE_CELL:
        cells = [_canonical_a1(cell) for cell in action.target.cells]
        target_range = _canonical_a1(action.target.range)
        if not cells:
            cells = _expand_range(target_range)
        plan.update(
            target_cells=cells,
            target_range=target_range or _enclosing_range(cells),
            new_value=arguments["new_value"],
        )
        return plan

    plan.update(
        destination=None,
        source_cells=[],
        source_range=None,
        literal_value=None,
        formula=None,
        expression=None,
        replacement_old=None,
        replacement_new=None,
        sort_range=None,
        sort_key=None,
        sort_order=None,
        insertion_position=None,
        insertion_axis=None,
        new_sheet_name=None,
        extract_length=None,
        separator=None,
    )

    if action.operation in {WRITE_VALUE, WRITE_FORMULA}:
        destination = _canonical_a1(
            action.target.cells[0]
            if action.target.cells
            else action.target.range.split(":")[0]
        )
        plan.update(
            target_range=destination,
            target_cells=[destination],
            destination=destination,
            source_cells=[_canonical_a1(cell) for cell in action.source.cells],
            source_range=_canonical_a1(action.source.range),
            expression=arguments.get("expression"),
        )
        if action.operation == WRITE_VALUE:
            plan.update(
                literal_value=arguments.get("literal_value"),
                new_value=arguments.get("literal_value"),
                separator=arguments.get("separator"),
                extract_length=arguments.get("extract_length"),
            )
        else:
            plan["formula"] = arguments["formula"]
            if arguments.get("array_mode"):
                plan["array_mode"] = arguments["array_mode"]
            if arguments.get("fill_mode"):
                plan.update(fill_mode=arguments["fill_mode"], target_range=_canonical_a1(action.target.range),
                            target_cells=_expand_range(_canonical_a1(action.target.range)))
        if action.source.ranges:
            plan["source_ranges"] = [_canonical_a1(value) for value in action.source.ranges]
        if action.source.ranges_sheet is not None:
            plan["source_ranges_sheet"] = action.source.ranges_sheet
    elif action.operation in {REPLACE_VALUES, SORT_RANGE, UNMERGE_RANGE}:
        plan.update(
            target_range=_canonical_a1(action.target.range),
            source_range=_canonical_a1(action.source.range),
        )
        if action.operation == REPLACE_VALUES:
            plan.update(
                replacement_old=arguments["replacement_old"],
                replacement_new=arguments["replacement_new"],
            )
        elif action.operation == SORT_RANGE:
            plan.update(
                sort_range=_canonical_a1(action.source.range),
                sort_key=arguments["sort_key"],
                sort_order=arguments["sort_order"],
            )
    elif action.operation == "delete_row":
        plan.update(deletion_position=int(arguments["position"]), source_cells=list(action.source.cells))
    elif action.operation in {INSERT_ROW, INSERT_COLUMN}:
        plan.update(
            insertion_position=arguments["position"],
            insertion_axis="row" if action.operation == INSERT_ROW else "column",
        )
    elif action.operation == COPY_SHEET:
        plan["new_sheet_name"] = action.target.new_sheet

    return plan


def _canonical_a1(reference: str | None) -> str | None:
    return reference.replace("$", "").upper() if reference is not None else None


def _expand_range(cell_range: str) -> list[str]:
    min_col, min_row, max_col, max_row = range_boundaries(cell_range)
    return [
        f"{get_column_letter(column)}{row}"
        for row in range(min_row, max_row + 1)
        for column in range(min_col, max_col + 1)
    ]


def _enclosing_range(cells: list[str]) -> str:
    bounds = [range_boundaries(cell) for cell in cells]
    start = f"{get_column_letter(min(item[0] for item in bounds))}{min(item[1] for item in bounds)}"
    end = f"{get_column_letter(max(item[2] for item in bounds))}{max(item[3] for item in bounds)}"
    return start if start == end else f"{start}:{end}"
