"""Fronteira pública de plano completo para o executor XLSX determinístico."""
from __future__ import annotations

from pathlib import Path
from shutil import copyfile
from tempfile import TemporaryDirectory

from openpyxl import load_workbook
from openpyxl.formula import Tokenizer
from openpyxl.utils.cell import range_boundaries

from ingestion.structural_snapshot import extract_structural_snapshot
from modification.structural_diff import build_structural_diff

from .contracts import file_hash, write_json
from .workbook.action_plan import ActionPlan
from .workbook.change_mask import column_position_to_index
from .workbook.insert_column_dependencies import persist_append_extent
from .workbook.ooxml_serialization import preserve_source_serialization
from .workbook.plan_effect_verifier import verify_plan_effect
from .workbook.spreadsheet_editor import _apply_structured_operation
from .workbook.structural_guard import validate_change_plan


def _reference(reference, sheet_name, sheets, *, existing_extent):
    sheet_name = sheet_name.strip("'").replace("''", "'")
    if sheet_name not in sheets:
        raise ValueError(f"WORKBOOK_SHEET_MISSING:{sheet_name}")
    if "[" in reference or "[" in sheet_name:
        raise ValueError("EXTERNAL_WORKBOOK_REFERENCE_NOT_SUPPORTED")
    bounds = range_boundaries(reference.replace("$", ""))
    if any(value is None for value in bounds):
        raise ValueError("REFERENCE_REQUIRES_FINITE_A1_EXTENT")
    left, top, right, bottom = bounds
    if not (1 <= left <= right <= 16384 and 1 <= top <= bottom <= 1048576):
        raise ValueError("REFERENCE_OUTSIDE_XLSX_LIMITS")
    sheet = sheets[sheet_name]
    if existing_extent and (right > sheet["max_column"] or bottom > sheet["max_row"]):
        raise ValueError(f"REFERENCE_OUTSIDE_WORKBOOK_EXTENT:{sheet_name}!{reference}")


def validate_workbook_references(action, snapshot):
    """Confere referências declaradas contra o workbook corrente, antes da escrita."""
    sheets = {sheet["sheet_name"]: sheet for sheet in snapshot["sheets"]}
    if action.sheet not in sheets:
        raise ValueError(f"WORKBOOK_SHEET_MISSING:{action.sheet}")
    for cell in action.source.cells:
        _reference(cell, action.sheet, sheets, existing_extent=True)
    if action.source.range:
        _reference(action.source.range, action.sheet, sheets, existing_extent=True)
    for value in action.source.ranges:
        _reference(value, action.source.ranges_sheet or action.sheet, sheets, existing_extent=True)
    for cell in action.target.cells:
        _reference(cell, action.sheet, sheets, existing_extent=action.operation == "update_cell")
    if action.target.range:
        _reference(
            action.target.range, action.sheet, sheets,
            existing_extent=action.operation not in {"write_value", "write_formula"},
        )
    if action.operation == "copy_sheet" and action.target.new_sheet in sheets:
        raise ValueError("COPY_SHEET_DESTINATION_ALREADY_EXISTS")
    if action.operation in {"insert_row", "delete_row", "insert_column"}:
        position = action.arguments["position"]
        column = action.operation == "insert_column"
        position = column_position_to_index(position) if column else int(position)
        maximum = sheets[action.sheet]["max_column" if column else "max_row"]
        allowance = 0 if action.operation == "delete_row" else 1
        if not 1 <= position <= maximum + allowance:
            raise ValueError("STRUCTURAL_POSITION_OUTSIDE_WORKBOOK_EXTENT")
    if action.operation == "write_formula":
        for token in Tokenizer(action.arguments["formula"]).items:
            if token.type == "OPERAND" and token.subtype == "RANGE":
                if "!" in token.value:
                    sheet, reference = token.value.rsplit("!", 1)
                else:
                    sheet, reference = action.sheet, token.value
                _reference(reference, sheet, sheets, existing_extent=True)
    return {"status": "passed", "scope": "declared_references_and_direct_formula_references"}


def execute_complete_plan(input_file, instruction, payload, evidences, output_dir):
    """Recebe o plano inteiro, conserva dependências e publica somente após todos os passos."""
    source = Path(input_file).resolve(strict=True)
    output_dir = Path(output_dir).resolve()
    output_dir.mkdir(parents=True, exist_ok=False)
    original_hash = file_hash(source)
    final = output_dir / "modified_workbook.xlsx"
    report = {
        "status": "PLAN_REJECTED", "published": False, "output": None,
        "planned_steps": len(payload.get("steps") or []), "completed_steps": 0,
        "steps": [], "original_file_preserved": True,
        "executor_contract": "public_complete_action_plan_v1",
    }
    try:
        if payload.get("schema_version") != "action_plan_v1":
            raise ValueError("ACTION_PLAN_V1_REQUIRED")
        plan = ActionPlan.from_dict(payload)
        plan.bind_instruction(instruction)
        available = {item["block_id"] for item in evidences} | {"instruction"}
        for step in plan.steps:
            if not set(step.evidence_block_ids) <= available:
                raise ValueError("PLAN_CITES_UNRETRIEVED_EVIDENCE")
        composite = plan.to_change_plan()
        report["change_plan"] = composite
        report["status"] = "EXECUTION_FAILED"
        with TemporaryDirectory(prefix="sic_plan_", dir=output_dir) as temporary:
            current = source
            for number, step in enumerate(plan.steps):
                candidate = Path(temporary) / f"step_{number:04d}.xlsx"
                change = composite["steps"][number]["change_plan"]
                event = {**step.to_dict(), "status": "started"}
                report["steps"].append(event)
                before = extract_structural_snapshot(str(current))
                event["references"] = validate_workbook_references(step.action, before)
                event["guard"] = validate_change_plan(change, before)
                if event["guard"]["status"] != "passed":
                    raise ValueError("STEP_STRUCTURAL_GUARD_REJECTED")
                workbook = load_workbook(current, data_only=False, keep_links=True)
                try:
                    changes = _apply_structured_operation(workbook, change, None)
                    preserve_source_serialization(workbook, current, change)
                    event["quality_diagnostics"] = getattr(workbook, "_observational_diagnostics", [])
                    workbook.save(candidate)
                finally:
                    workbook.close()
                if step.action.operation == "insert_column":
                    position = column_position_to_index(step.action.arguments["position"])
                    sheet = next(sheet for sheet in before["sheets"] if sheet["sheet_name"] == step.action.sheet)
                    if position == sheet["max_column"] + 1:
                        persist_append_extent(candidate, step.action.sheet, position)
                after = extract_structural_snapshot(str(candidate))
                difference = build_structural_diff(before, after, change, str(current), str(candidate))
                editor = {"status": "applied", "changed_cells": changes}
                event["effect_verification"] = verify_plan_effect(change, before, after, difference, editor)
                event["structural_diff"] = difference
                if event["effect_verification"]["status"] != "passed":
                    raise ValueError("STEP_EFFECT_VERIFICATION_FAILED")
                event["status"] = "verified"
                report["completed_steps"] += 1
                current = candidate
            if file_hash(source) != original_hash:
                raise RuntimeError("ORIGINAL_MODIFIED")
            copyfile(current, final)
            report.update(status="OUTPUT", published=True, output=str(final))
    except Exception as error:
        report["error_type"] = type(error).__name__
        report["error"] = str(error)
        if report["steps"] and report["steps"][-1]["status"] == "started":
            report["steps"][-1]["status"] = "failed"
    report["original_file_preserved"] = file_hash(source) == original_hash
    if not report["original_file_preserved"]:
        report.update(status="ORIGINAL_MODIFIED", published=False, output=None)
    write_json(output_dir / "execution.json", report)
    return report
