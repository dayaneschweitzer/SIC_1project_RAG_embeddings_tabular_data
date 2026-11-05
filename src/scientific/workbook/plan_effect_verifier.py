"""Verify observable effects and scope of an executed ChangePlan.

Structural coherence does not certify correctness against a reference output.
Verification never reads operation_family, OracleSpec, golden or expected_* fields.
"""

from __future__ import annotations

import math
from typing import Any

from openpyxl.utils import get_column_letter, range_boundaries

from ingestion.semantic_style import semantic_styles_equivalent

from .change_mask import cell_allowed
from .parsed_intent import (
    COPY_SHEET,
    INSERT_COLUMN,
    INSERT_ROW,
    REPLACE_VALUES,
    SORT_RANGE,
    UNMERGE_RANGE,
    UPDATE_CELL,
    WRITE_FORMULA,
    WRITE_VALUE,
)

from .execution_contract import ORACLE_FIELD_NAMES, OracleVisibleToExecutionError


FAILURE_PLAN = "plan_failed"
FAILURE_EXECUTION = "execution_failed"
FAILURE_VERIFICATION = "verification_failed"
FAILURE_OUT_OF_SCOPE = "out_of_scope_change"
FAILURE_EFFECT_MISSING = "structural_effect_missing"

WRITE_OPERATIONS = frozenset({UPDATE_CELL, WRITE_VALUE, WRITE_FORMULA, REPLACE_VALUES})
STRUCTURAL_OPERATIONS = frozenset({"delete_row", INSERT_ROW, INSERT_COLUMN, SORT_RANGE, UNMERGE_RANGE, COPY_SHEET})

FORBIDDEN_DECISION_FIELDS = frozenset(ORACLE_FIELD_NAMES) | {
    "operation_family",
    "operation_kind",
    "oracle",
    "golden",
}


def verify_plan_effect(
    change_plan: dict | None,
    original_snapshot: dict | None = None,
    modified_snapshot: dict | None = None,
    structural_diff: dict | None = None,
    execution_result: dict | None = None,
    changed_cells: list | None = None,
    allowed_change_mask: dict | str | None = None,
) -> dict:
    """Verify the executed plan against snapshots/diff/mask. Never uses oracle/golden."""
    plan = dict(change_plan or {})
    original = original_snapshot or {}
    modified = modified_snapshot or {}
    diff = structural_diff or {}
    editor = execution_result or {}
    cells = list(changed_cells if changed_cells is not None else editor.get("changed_cells") or [])
    plan = {key: value for key, value in plan.items() if str(key) not in FORBIDDEN_DECISION_FIELDS}

    errors: list[str] = []
    codes: list[str] = []
    checks: dict[str, bool] = {
        "plan_present": bool(plan.get("operation")),
        "effect_observed": False,
        "scope_respected": True,
        "structure_coherent": True,
    }

    if plan.get("status") in {"failed", "plan_failed"} or not plan.get("operation"):
        return _report(
            status="failed",
            operation=plan.get("operation"),
            errors=["Plano ausente ou falho."],
            codes=[FAILURE_PLAN],
            checks=checks,
        )

    operation = plan.get("operation")
    if operation == INSERT_COLUMN and editor.get("status") == "applied" and editor.get("input_xlsx") and editor.get("output_xlsx"):
        from .insert_column_audit import verify_insert_column
        from ingestion.structural_snapshot import extract_structural_snapshot
        before_path = diff.get("original_file") or editor["input_xlsx"]
        after_path = diff.get("modified_file") or editor["output_xlsx"]
        dependency = verify_insert_column(before_path, after_path, plan["target_sheet"], _column_index(plan["insertion_position"]))
        try:
            def content(snapshot):
                return {key: value for key, value in snapshot.items() if key != "file_name"}
            bound = (content(extract_structural_snapshot(str(before_path))) == content(original)
                     and content(extract_structural_snapshot(str(after_path))) == content(modified))
        except Exception:
            bound = False
        passed = dependency["covered_status"] == "PASS" and bound
        result = _report(status="passed" if passed else "failed", operation=operation,
                         errors=[] if passed else dependency["errors"] + (["SNAPSHOT_FILE_BINDING_MISMATCH"] if not bound else []),
                         codes=[] if passed else [FAILURE_VERIFICATION],
                         checks={"plan_present": True, "effect_observed": passed, "scope_respected": passed, "structure_coherent": passed})
        result["dependency_verification"] = dependency
        return result
    if editor.get("status") == "failed":
        return _report(
            status="failed",
            operation=operation,
            errors=list(editor.get("errors") or ["Execucao nao aplicada."]),
            codes=[FAILURE_EXECUTION],
            checks=checks,
        )

    if operation in WRITE_OPERATIONS:
        _verify_write(plan, original, modified, diff, cells, allowed_change_mask, errors, codes, checks)
    elif operation == "delete_row":
        _verify_delete_row(plan, original, modified, errors, codes, checks)
    elif operation == INSERT_ROW:
        _verify_insert_row(plan, original, modified, errors, codes, checks)
    elif operation == INSERT_COLUMN:
        _verify_insert_column(plan, original, modified, errors, codes, checks)
    elif operation == SORT_RANGE:
        _verify_sort_range(plan, original, modified, errors, codes, checks)
    elif operation == UNMERGE_RANGE:
        _verify_unmerge_range(plan, original, modified, errors, codes, checks)
    elif operation == COPY_SHEET:
        _verify_copy_sheet(plan, original, modified, errors, codes, checks)
    else:
        errors.append(f"Operacao nao verificavel: {operation}")
        codes.append(FAILURE_VERIFICATION)

    status = "passed" if not errors else "failed"
    report = _report(status=status, operation=operation, errors=errors, codes=codes, checks=checks)
    if operation == INSERT_COLUMN:
        report["dependency_verification"] = {"covered_status": "NOT_VERIFIED", "reason": "FILE_BOUND_VERIFICATION_NOT_AVAILABLE"}
    _assert_inputs_oracle_free(report)
    return report


def _verify_write(
    plan: dict,
    original: dict,
    modified: dict,
    diff: dict,
    changed_cells: list,
    mask,
    errors: list[str],
    codes: list[str],
    checks: dict[str, bool],
) -> None:
    operation = plan.get("operation")
    sheet_name = plan.get("target_sheet")
    original_sheet = _find_sheet(original, sheet_name)
    modified_sheet = _find_sheet(modified, sheet_name)
    if original and not original_sheet:
        errors.append("Aba alvo ausente no snapshot de entrada.")
        codes.append(FAILURE_PLAN)
        checks["structure_coherent"] = False
        return
    if modified and not modified_sheet:
        errors.append("Aba alvo ausente no snapshot de saida.")
        codes.append(FAILURE_EFFECT_MISSING)
        checks["structure_coherent"] = False
        return

    expected_destinations = _write_destinations(plan, original_sheet)
    if not expected_destinations:
        errors.append("Destino planejado ausente.")
        codes.append(FAILURE_PLAN)
        return

    observed_changes = _observed_write_changes(diff, changed_cells, original, modified)
    in_scope = {
        (sheet, coord)
        for sheet, coord in observed_changes
        if sheet == sheet_name and coord in expected_destinations
    }
    out_of_scope = {
        (sheet, coord)
        for sheet, coord in observed_changes
        if sheet != sheet_name or coord not in expected_destinations
    }

    if mask:
        for sheet, coord in observed_changes:
            if not cell_allowed(sheet, coord, mask):
                out_of_scope.add((sheet, coord))
                errors.append(f"escrita fora do mask: {sheet}!{coord}")
                codes.append(FAILURE_OUT_OF_SCOPE)

    if out_of_scope:
        checks["scope_respected"] = False
        if FAILURE_OUT_OF_SCOPE not in codes:
            codes.append(FAILURE_OUT_OF_SCOPE)
        errors.append("alteracao extra fora do escopo")

    if original and modified:
        other_ok = _other_sheets_preserved(original, modified, {sheet_name})
        if not other_ok:
            checks["scope_respected"] = False
            codes.append(FAILURE_OUT_OF_SCOPE)
            errors.append("aba nao alvo foi alterada")
        if not _structure_preserved_for_writes(original_sheet, modified_sheet):
            checks["structure_coherent"] = False
            codes.append(FAILURE_OUT_OF_SCOPE)
            errors.append("estrutura fora do escopo de escrita foi alterada")

    dest_changed = bool(in_scope)
    if original_sheet and modified_sheet:
        original_lookup = _cell_lookup_sheet(original_sheet)
        modified_lookup = _cell_lookup_sheet(modified_sheet)
        dest_changed = any(
            not _cells_equivalent(
                original_lookup.get(str(coord).upper()),
                modified_lookup.get(str(coord).upper()),
            )
            for coord in expected_destinations
        )

    checks["effect_observed"] = dest_changed
    if not dest_changed:
        errors.append("destino nao alterado")
        codes.append(FAILURE_EFFECT_MISSING)

    if dest_changed and original_sheet and modified_sheet:
        _verify_write_payload(plan, original_sheet, modified_sheet, expected_destinations, errors, codes)

    if operation == REPLACE_VALUES and original_sheet and modified_sheet:
        _verify_replace_payload(plan, original_sheet, modified_sheet, errors, codes, checks)


def _verify_write_payload(
    plan: dict,
    original_sheet: dict,
    modified_sheet: dict,
    destinations: set[str],
    errors: list[str],
    codes: list[str],
) -> None:
    """Verify write payload at observed destinations.

    A requested interior coordinate of a merged range is acceptable when the
    write landed on the merge anchor.
    """
    operation = plan.get("operation")
    modified_lookup = _cell_lookup_sheet(modified_sheet)
    for coord in destinations:
        after = modified_lookup.get(str(coord).upper())
        if after is None:
            continue
        is_formula = bool(after.get("is_formula")) or str(after.get("value") or "").startswith("=")
        if operation == WRITE_FORMULA:
            from .formula_contract import formula_at
            wanted = formula_at(plan, coord)
            if not is_formula:
                errors.append("valor quando deveria ser formula")
                codes.append(FAILURE_VERIFICATION)
            elif wanted and _verified_formula_text(after.get("value")) != wanted:
                errors.append("destino incorreto")
                codes.append(FAILURE_VERIFICATION)
        else:
            if is_formula:
                errors.append("formula quando deveria ser valor")
                codes.append(FAILURE_VERIFICATION)
            expected_value = _expected_write_value(plan, original_sheet)
            if expected_value is not None and not _scalar_equivalent(after.get("value"), expected_value):
                errors.append("destino incorreto")
                codes.append(FAILURE_VERIFICATION)
            if operation == WRITE_VALUE and plan.get('expression') in {None,'layout_caption'} and not _literal_type_matches(after,expected_value):
                errors.append('tipo do valor literal incorreto')
                codes.append(FAILURE_VERIFICATION)


def _expected_write_value(plan: dict, original_sheet: dict) -> Any:
    operation = plan.get("operation")
    if operation == WRITE_FORMULA:
        return None
    if operation == REPLACE_VALUES:
        return None
    if operation == UPDATE_CELL:
        return plan.get("new_value")
    expression = plan.get("expression")
    if expression in {None, "layout_caption"}:
        return plan.get("literal_value") if plan.get("literal_value") not in (None, "") else plan.get("new_value")
    sources = [str(item).upper() for item in plan.get("source_cells") or []]
    if expression == "concatenate" and len(sources) >= 2:
        left = _cell_text(_cell_at(original_sheet, sources[0]))
        right = _cell_text(_cell_at(original_sheet, sources[1]))
        if left is None or right is None:
            return None
        separator = plan.get("separator")
        if separator is None:
            separator = " "
        return f"{left}{separator}{right}"
    if expression == "extract_token" and sources:
        text = _cell_text(_cell_at(original_sheet, sources[0]))
        return text.split()[0] if text else None
    if expression == "extract_prefix" and sources and plan.get("extract_length"):
        text = _cell_text(_cell_at(original_sheet, sources[0]))
        return text[: int(plan["extract_length"])] if text else None
    if expression == "add" and len(sources) >= 2:
        left = _cell_number(_cell_at(original_sheet, sources[0]))
        right = _cell_number(_cell_at(original_sheet, sources[1]))
        if left is None or right is None:
            return None
        total = left + right
        if isinstance(left, int) and isinstance(right, int) and not isinstance(left, bool) and not isinstance(right, bool):
            return int(total)
        return float(total)
    return None


def _verify_replace_payload(
    plan: dict,
    original_sheet: dict,
    modified_sheet: dict,
    errors: list[str],
    codes: list[str],
    checks: dict[str, bool],
) -> None:
    rng = plan.get("source_range")
    old_value = plan.get("replacement_old")
    new_value = plan.get("replacement_new")
    if not rng or old_value in (None, ""):
        errors.append("intervalo de substituicao ausente")
        codes.append(FAILURE_PLAN)
        return
    try:
        coords = _iter_range(rng)
        coordinate_set = set(coords)
    except ValueError:
        errors.append("intervalo de substituicao invalido")
        codes.append(FAILURE_PLAN)
        return
    original_lookup = _cell_lookup_sheet(original_sheet)
    modified_lookup = _cell_lookup_sheet(modified_sheet)
    matched = False
    for coord in coords:
        before = original_lookup.get(coord)
        if not before or _is_formula_cell(before):
            continue
        if str(before.get("value")) != str(old_value):
            continue
        matched = True
        after = modified_lookup.get(coord)
        if after is None or str(after.get("value")) != str(new_value) or not _literal_type_matches(after,new_value):
            errors.append("destino nao alterado")
            codes.append(FAILURE_EFFECT_MISSING)
            checks["effect_observed"] = False
    if matched:
        checks["effect_observed"] = checks["effect_observed"] or FAILURE_EFFECT_MISSING not in codes
    outside = set(original_lookup) | set(modified_lookup)
    for coord in outside:
        if coord in coordinate_set:
            before = original_lookup.get(coord)
            if before and not _is_formula_cell(before) and str(before.get("value")) == str(old_value):
                continue
        if not _cells_equivalent(original_lookup.get(coord), modified_lookup.get(coord)):
            errors.append("alteracao extra fora do escopo")
            codes.append(FAILURE_OUT_OF_SCOPE)
            checks["scope_respected"] = False


def _verify_delete_row(plan, original, modified, errors, codes, checks):
    name, position = plan.get("target_sheet"), plan.get("deletion_position")
    before, after = _find_sheet(original, name), _find_sheet(modified, name)
    if not before or not after or type(position) is not int or not 1 <= position <= int(before.get("max_row") or 0):
        errors.append("Invalid row deletion contract")
        codes.append(FAILURE_PLAN)
        return
    expected = {(r - 1 if r > position else r, c): value for (r, c), value in _grid(before).items() if r != position}
    actual = _grid(after)
    keys = set(expected) | set(actual)
    coherent = all(_cells_equivalent(expected.get(key), actual.get(key)) for key in keys)
    scope = _other_sheets_preserved(original, modified, {name})
    checks.update(effect_observed=coherent, structure_coherent=coherent, scope_respected=coherent and scope)
    if not coherent or not scope:
        errors.append("Row deletion effect or scope differs from the complete planned shift")
        codes.append(FAILURE_OUT_OF_SCOPE)


def _verify_insert_row(
    plan: dict,
    original: dict,
    modified: dict,
    errors: list[str],
    codes: list[str],
    checks: dict[str, bool],
) -> None:
    sheet_name = plan.get("target_sheet")
    position = plan.get("insertion_position")
    if position in (None, ""):
        errors.append("indice de linha ausente")
        codes.append(FAILURE_PLAN)
        return
    insert_at = int(position)
    original_sheet = _find_sheet(original, sheet_name)
    modified_sheet = _find_sheet(modified, sheet_name)
    if not original_sheet or not modified_sheet:
        errors.append("snapshots insuficientes para insert_row")
        codes.append(FAILURE_VERIFICATION)
        return
    before_max = int(original_sheet.get("max_row") or 0)
    after_max = int(modified_sheet.get("max_row") or 0)
    if after_max != before_max + 1:
        errors.append("efeito estrutural ausente: max_row nao aumentou em 1")
        codes.append(FAILURE_EFFECT_MISSING)
        checks["effect_observed"] = False
        checks["structure_coherent"] = False
    else:
        checks["effect_observed"] = True

    original_cells = _grid(original_sheet)
    modified_cells = _grid(modified_sheet)
    expected: dict[tuple[int, int], dict] = {}
    for (row, col), cell in original_cells.items():
        new_row = row + 1 if row >= insert_at else row
        expected[(new_row, col)] = cell
    for key, cell in expected.items():
        actual = modified_cells.get(key)
        if not _cells_equivalent(cell, actual):
            errors.append("shift de linha incompativel com a posicao planejada")
            codes.append(FAILURE_VERIFICATION)
            checks["structure_coherent"] = False
            break
    for (row, col), cell in modified_cells.items():
        if row == insert_at:
            if not _is_empty_cell(cell):
                errors.append("linha inserida nao esta vazia na posicao planejada")
                codes.append(FAILURE_VERIFICATION)
                checks["structure_coherent"] = False
            continue
        expected_row = row - 1 if row > insert_at else row
        if (expected_row, col) not in original_cells and not _is_empty_cell(cell):
            errors.append("alteracao extra fora do escopo")
            codes.append(FAILURE_OUT_OF_SCOPE)
            checks["scope_respected"] = False

    if not _other_sheets_preserved(original, modified, {sheet_name}):
        errors.append("aba nao alvo foi alterada")
        codes.append(FAILURE_OUT_OF_SCOPE)
        checks["scope_respected"] = False
    expected_merges = _shift_merges_rows(original_sheet.get("merged_cells") or [], insert_at)
    if _normalize_merges(modified_sheet.get("merged_cells") or []) != expected_merges:
        errors.append("merges fora do shift esperado")
        codes.append(FAILURE_OUT_OF_SCOPE)
        checks["scope_respected"] = False


def _verify_insert_column(
    plan: dict,
    original: dict,
    modified: dict,
    errors: list[str],
    codes: list[str],
    checks: dict[str, bool],
) -> None:
    sheet_name = plan.get("target_sheet")
    position = plan.get("insertion_position")
    if position in (None, ""):
        errors.append("indice de coluna ausente")
        codes.append(FAILURE_PLAN)
        return
    insert_at = _column_index(position)
    original_sheet = _find_sheet(original, sheet_name)
    modified_sheet = _find_sheet(modified, sheet_name)
    if not original_sheet or not modified_sheet:
        errors.append("snapshots insuficientes para insert_column")
        codes.append(FAILURE_VERIFICATION)
        return
    before_max = int(original_sheet.get("max_column") or 0)
    after_max = int(modified_sheet.get("max_column") or 0)
    if after_max != before_max + 1:
        errors.append("efeito estrutural ausente: max_column nao aumentou em 1")
        codes.append(FAILURE_EFFECT_MISSING)
        checks["effect_observed"] = False
        checks["structure_coherent"] = False
    else:
        checks["effect_observed"] = True

    original_cells = _grid(original_sheet)
    modified_cells = _grid(modified_sheet)
    expected: dict[tuple[int, int], dict] = {}
    for (row, col), cell in original_cells.items():
        new_col = col + 1 if col >= insert_at else col
        expected[(row, new_col)] = cell
    for key, cell in expected.items():
        actual = modified_cells.get(key)
        if not _cells_equivalent(cell, actual):
            errors.append("shift de coluna incompativel com a posicao planejada")
            codes.append(FAILURE_VERIFICATION)
            checks["structure_coherent"] = False
            break
    for (row, col), cell in modified_cells.items():
        if col == insert_at:
            if not _is_empty_cell(cell):
                errors.append("coluna inserida nao esta vazia na posicao planejada")
                codes.append(FAILURE_VERIFICATION)
                checks["structure_coherent"] = False
            continue
        expected_col = col - 1 if col > insert_at else col
        if (row, expected_col) not in original_cells and not _is_empty_cell(cell):
            errors.append("alteracao extra fora do escopo")
            codes.append(FAILURE_OUT_OF_SCOPE)
            checks["scope_respected"] = False

    if not _other_sheets_preserved(original, modified, {sheet_name}):
        errors.append("aba nao alvo foi alterada")
        codes.append(FAILURE_OUT_OF_SCOPE)
        checks["scope_respected"] = False
    expected_merges = _shift_merges_columns(original_sheet.get("merged_cells") or [], insert_at)
    if _normalize_merges(modified_sheet.get("merged_cells") or []) != expected_merges:
        errors.append("merges fora do shift esperado")
        codes.append(FAILURE_OUT_OF_SCOPE)
        checks["scope_respected"] = False


def _verify_sort_range(
    plan: dict,
    original: dict,
    modified: dict,
    errors: list[str],
    codes: list[str],
    checks: dict[str, bool],
) -> None:
    sheet_name = plan.get("target_sheet")
    rng = plan.get("sort_range") or plan.get("source_range")
    original_sheet = _find_sheet(original, sheet_name)
    modified_sheet = _find_sheet(modified, sheet_name)
    if not rng:
        errors.append("intervalo de ordenacao ausente")
        codes.append(FAILURE_PLAN)
        return
    if not original_sheet or not modified_sheet:
        errors.append("snapshots insuficientes para sort_range")
        codes.append(FAILURE_VERIFICATION)
        return
    try:
        min_col, min_row, max_col, max_row = range_boundaries(str(rng).upper())
    except ValueError:
        errors.append("intervalo de ordenacao invalido")
        codes.append(FAILURE_PLAN)
        return

    original_rows = _matrix(original_sheet, min_row, max_row, min_col, max_col)
    modified_rows = _matrix(modified_sheet, min_row, max_row, min_col, max_col)
    if _multiset(original_rows) != _multiset(modified_rows):
        errors.append("multiconjunto do intervalo de ordenacao nao foi preservado")
        codes.append(FAILURE_VERIFICATION)
        checks["structure_coherent"] = False
    else:
        checks["effect_observed"] = True

    expected = sorted(original_rows, key=_sort_key)
    if plan.get("sort_order") == "descending":
        expected = list(reversed(sorted(original_rows, key=_sort_key)))
    if modified_rows != expected:
        errors.append("ordenacao nao respeita a chave planejada")
        codes.append(FAILURE_VERIFICATION)
        checks["structure_coherent"] = False

    original_grid = _grid(original_sheet)
    modified_grid = _grid(modified_sheet)
    all_keys = set(original_grid) | set(modified_grid)
    for row, col in all_keys:
        inside = min_row <= row <= max_row and min_col <= col <= max_col
        if inside:
            continue
        if not _cells_equivalent(original_grid.get((row, col)), modified_grid.get((row, col))):
            errors.append("alteracao extra fora do escopo")
            codes.append(FAILURE_OUT_OF_SCOPE)
            checks["scope_respected"] = False
            break

    if not _other_sheets_preserved(original, modified, {sheet_name}):
        errors.append("aba nao alvo foi alterada")
        codes.append(FAILURE_OUT_OF_SCOPE)
        checks["scope_respected"] = False
    if _normalize_merges(original_sheet.get("merged_cells") or []) != _normalize_merges(
        modified_sheet.get("merged_cells") or []
    ):
        errors.append("merges fora do intervalo foram alterados")
        codes.append(FAILURE_OUT_OF_SCOPE)
        checks["scope_respected"] = False


def _verify_unmerge_range(
    plan: dict,
    original: dict,
    modified: dict,
    errors: list[str],
    codes: list[str],
    checks: dict[str, bool],
) -> None:
    sheet_name = plan.get("target_sheet")
    rng = str(plan.get("source_range") or "").upper()
    original_sheet = _find_sheet(original, sheet_name)
    modified_sheet = _find_sheet(modified, sheet_name)
    if not rng:
        errors.append("intervalo a desmesclar ausente")
        codes.append(FAILURE_PLAN)
        return
    if not original_sheet or not modified_sheet:
        errors.append("snapshots insuficientes para unmerge_range")
        codes.append(FAILURE_VERIFICATION)
        return
    before = _normalize_merges(original_sheet.get("merged_cells") or [])
    after = _normalize_merges(modified_sheet.get("merged_cells") or [])
    if rng not in before:
        errors.append("merge planejado nao existia antes")
        codes.append(FAILURE_PLAN)
        checks["structure_coherent"] = False
        return
    if rng in after:
        errors.append("merge planejado ainda presente")
        codes.append(FAILURE_EFFECT_MISSING)
        checks["effect_observed"] = False
        checks["structure_coherent"] = False
    else:
        checks["effect_observed"] = True
    if before - {rng} != after:
        errors.append("merge externo alterado")
        codes.append(FAILURE_OUT_OF_SCOPE)
        checks["scope_respected"] = False

    try:
        min_col, min_row, max_col, max_row = range_boundaries(rng)
    except ValueError:
        return
    original_grid = _grid(original_sheet)
    modified_grid = _grid(modified_sheet)
    anchor = (min_row, min_col)
    for row, col in set(original_grid) | set(modified_grid):
        inside = min_row <= row <= max_row and min_col <= col <= max_col
        if inside:
            if (row, col) == anchor:
                if not _cells_equivalent(original_grid.get(anchor), modified_grid.get(anchor)):
                    errors.append("conteudo do merge nao preservado no ancora")
                    codes.append(FAILURE_VERIFICATION)
            continue
        if not _cells_equivalent(original_grid.get((row, col)), modified_grid.get((row, col))):
            errors.append("alteracao extra fora do escopo")
            codes.append(FAILURE_OUT_OF_SCOPE)
            checks["scope_respected"] = False
            break
    if not _other_sheets_preserved(original, modified, {sheet_name}):
        errors.append("aba nao alvo foi alterada")
        codes.append(FAILURE_OUT_OF_SCOPE)
        checks["scope_respected"] = False


def _verify_copy_sheet(
    plan: dict,
    original: dict,
    modified: dict,
    errors: list[str],
    codes: list[str],
    checks: dict[str, bool],
) -> None:
    source_name = plan.get("target_sheet")
    new_name = plan.get("new_sheet_name")
    if not source_name or not new_name:
        errors.append("origem ou nome da nova aba ausente")
        codes.append(FAILURE_PLAN)
        return
    original_names = _sheet_names(original)
    modified_names = _sheet_names(modified)
    if source_name not in original_names:
        errors.append("aba origem ausente no snapshot de entrada")
        codes.append(FAILURE_PLAN)
        return
    if new_name not in modified_names:
        errors.append("sheet ausente")
        codes.append(FAILURE_EFFECT_MISSING)
        checks["effect_observed"] = False
        return
    checks["effect_observed"] = True
    if source_name not in modified_names:
        errors.append("original alterada")
        codes.append(FAILURE_OUT_OF_SCOPE)
        checks["scope_respected"] = False
    expected_names = original_names + [new_name]
    if modified_names != expected_names:
        extra = [name for name in modified_names if name not in original_names and name != new_name]
        missing = [name for name in original_names if name not in modified_names]
        if extra:
            errors.append("sheet extra indevida")
            codes.append(FAILURE_OUT_OF_SCOPE)
        if missing:
            errors.append("original alterada")
            codes.append(FAILURE_OUT_OF_SCOPE)
        if not extra and not missing and modified_names != expected_names:
            errors.append("ordem ou conjunto de abas inesperado")
            codes.append(FAILURE_VERIFICATION)
        checks["scope_respected"] = False

    source_before = _find_sheet(original, source_name)
    source_after = _find_sheet(modified, source_name)
    copied = _find_sheet(modified, new_name)
    if not _sheets_content_equivalent(source_before, source_after):
        errors.append("original alterada")
        codes.append(FAILURE_OUT_OF_SCOPE)
        checks["scope_respected"] = False
    if not _sheets_content_equivalent(source_before, copied, ignore_title=True):
        errors.append("conteudo da nova sheet nao corresponde a copia observavel da original")
        codes.append(FAILURE_VERIFICATION)
        checks["structure_coherent"] = False
    preserved = set(original_names) - {source_name}
    if not _other_sheets_preserved(original, modified, {source_name, new_name} | preserved, exact=preserved):
        errors.append("aba nao alvo foi alterada")
        codes.append(FAILURE_OUT_OF_SCOPE)
        checks["scope_respected"] = False


def _write_destinations(plan: dict, original_sheet: dict | None) -> set[str]:
    operation = plan.get("operation")
    if operation == REPLACE_VALUES:
        rng = plan.get("source_range")
        old_value = plan.get("replacement_old")
        if not rng or original_sheet is None:
            try:
                return set(_iter_range(rng)) if rng else set()
            except ValueError:
                return set()
        coords = set()
        lookup = _cell_lookup_sheet(original_sheet)
        for coord in _iter_range(rng):
            cell = lookup.get(coord)
            if cell and not _is_formula_cell(cell) and str(cell.get("value")) == str(old_value):
                coords.add(coord)
        return coords
    targets = [str(item).upper() for item in plan.get("target_cells") or [] if item]
    dest = plan.get("destination") or plan.get("target_coordinate")
    if dest:
        targets.append(str(dest).upper())
    resolved = set()
    for coord in targets:
        resolved.add(_merge_anchor(original_sheet, coord) if original_sheet else coord)
    return {item for item in resolved if item}


def _observed_write_changes(diff: dict, changed_cells: list, original: dict, modified: dict) -> set[tuple[str, str]]:
    observed: set[tuple[str, str]] = set()
    for bucket in (
        diff.get("changed_cells") or [],
        diff.get("added_cells") or [],
        diff.get("removed_cells") or [],
        diff.get("unexpected_value_changes") or [],
        changed_cells,
    ):
        for item in bucket:
            if not isinstance(item, dict):
                continue
            sheet = item.get("sheet_name")
            coord = str(item.get("coordinate") or "").upper()
            if sheet and coord:
                observed.add((sheet, coord))
    if original and modified:
        original_lookup = _workbook_lookup(original)
        modified_lookup = _workbook_lookup(modified)
        keys = set(original_lookup) | set(modified_lookup)
        for key in keys:
            if not _cells_equivalent(original_lookup.get(key), modified_lookup.get(key)):
                observed.add(key)
    return observed


def _structure_preserved_for_writes(original_sheet: dict, modified_sheet: dict) -> bool:
    return (
        _normalize_merges(original_sheet.get("merged_cells") or [])
        == _normalize_merges(modified_sheet.get("merged_cells") or [])
        and (original_sheet.get("tables") or []) == (modified_sheet.get("tables") or [])
        and int(original_sheet.get("data_validations_count") or 0)
        == int(modified_sheet.get("data_validations_count") or 0)
    )


def _other_sheets_preserved(
    original: dict,
    modified: dict,
    excluded: set[str],
    exact: set[str] | None = None,
) -> bool:
    """Non-target sheets are preserved iff observable semantics match.

    Isolated max_row / max_column / used-range shifts after save/reload are
    not treated as modifications.
    """
    names = exact if exact is not None else set(_sheet_names(original)) - excluded
    for name in names:
        if not _sheets_content_equivalent(_find_sheet(original, name), _find_sheet(modified, name)):
            return False
    return True


def _sheets_content_equivalent(left: dict | None, right: dict | None, ignore_title: bool = False) -> bool:
    if left is None or right is None:
        return left is right
    if not ignore_title and left.get("sheet_name") != right.get("sheet_name"):
        return False
    if _normalize_merges(left.get("merged_cells") or []) != _normalize_merges(right.get("merged_cells") or []):
        return False
    if (left.get("tables") or []) != (right.get("tables") or []):
        return False
    if int(left.get("data_validations_count") or 0) != int(right.get("data_validations_count") or 0):
        return False
    if int(left.get("conditional_formatting_count") or 0) != int(right.get("conditional_formatting_count") or 0):
        return False
    if left.get("freeze_panes") != right.get("freeze_panes"):
        return False
    if left.get("sheet_state") != right.get("sheet_state"):
        return False
    left_grid = _grid(left)
    right_grid = _grid(right)
    if set(left_grid) != set(right_grid):
        return False
    return all(_cells_equivalent(left_grid[key], right_grid[key]) for key in left_grid)


def _grid(sheet: dict) -> dict[tuple[int, int], dict]:
    grid = {}
    for cell in sheet.get("cells") or []:
        row = int(cell.get("row") or 0)
        col = int(cell.get("column") or 0)
        if not row or not col:
            coord = str(cell.get("coordinate") or "")
            if not coord:
                continue
            col, row, _, _ = range_boundaries(coord.upper())
        grid[(row, col)] = cell
    return grid


def _matrix(sheet: dict, min_row: int, max_row: int, min_col: int, max_col: int) -> list[tuple]:
    grid = _grid(sheet)
    rows = []
    for row in range(min_row, max_row + 1):
        values = []
        for col in range(min_col, max_col + 1):
            cell = grid.get((row, col))
            values.append(None if cell is None else cell.get("value"))
        rows.append(tuple(values))
    return rows


def _multiset(rows: list[tuple]) -> dict[tuple, int]:
    counts: dict[tuple, int] = {}
    for row in rows:
        counts[row] = counts.get(row, 0) + 1
    return counts


def _sort_key(row: tuple) -> tuple:
    value = row[0] if row else None
    return (value is None, str(value).lower() if value is not None else "")


def _shift_merges_rows(ranges: list[str], insert_at: int) -> set[str]:
    shifted = set()
    for rng in ranges:
        min_col, min_row, max_col, max_row = range_boundaries(str(rng).upper())
        if max_row < insert_at:
            shifted.add(_range_text(min_col, min_row, max_col, max_row))
        elif min_row >= insert_at:
            shifted.add(_range_text(min_col, min_row + 1, max_col, max_row + 1))
        else:
            shifted.add(_range_text(min_col, min_row, max_col, max_row + 1))
    return shifted


def _shift_merges_columns(ranges: list[str], insert_at: int) -> set[str]:
    shifted = set()
    for rng in ranges:
        min_col, min_row, max_col, max_row = range_boundaries(str(rng).upper())
        if max_col < insert_at:
            shifted.add(_range_text(min_col, min_row, max_col, max_row))
        elif min_col >= insert_at:
            shifted.add(_range_text(min_col + 1, min_row, max_col + 1, max_row))
        else:
            shifted.add(_range_text(min_col, min_row, max_col + 1, max_row))
    return shifted


def _range_text(min_col: int, min_row: int, max_col: int, max_row: int) -> str:
    start = f"{get_column_letter(min_col)}{min_row}"
    end = f"{get_column_letter(max_col)}{max_row}"
    return start if start == end else f"{start}:{end}"


def _merge_anchor(sheet: dict, coordinate: str) -> str:
    wanted = str(coordinate).upper()
    try:
        col, row, _, _ = range_boundaries(wanted)
    except ValueError:
        return wanted
    for rng in sheet.get("merged_cells") or []:
        min_col, min_row, max_col, max_row = range_boundaries(str(rng).upper())
        if min_row <= row <= max_row and min_col <= col <= max_col:
            return f"{get_column_letter(min_col)}{min_row}"
    return wanted


def _column_index(position: Any) -> int:
    text = str(position).strip()
    if text.isdigit():
        return int(text)
    col, _, _, _ = range_boundaries(f"{text.upper()}1")
    return col


def _iter_range(cell_range: str) -> list[str]:
    min_col, min_row, max_col, max_row = range_boundaries(str(cell_range).upper())
    return [
        f"{get_column_letter(col)}{row}"
        for row in range(min_row, max_row + 1)
        for col in range(min_col, max_col + 1)
    ]


def _find_sheet(snapshot: dict, sheet_name: str | None) -> dict | None:
    for sheet in snapshot.get("sheets") or []:
        if sheet.get("sheet_name") == sheet_name:
            return sheet
    return None


def _sheet_names(snapshot: dict) -> list[str]:
    return [sheet.get("sheet_name") for sheet in snapshot.get("sheets") or []]


def _cell_lookup_sheet(sheet: dict) -> dict[str, dict]:
    return {str(cell.get("coordinate") or "").upper(): cell for cell in sheet.get("cells") or []}


def _workbook_lookup(snapshot: dict) -> dict[tuple[str, str], dict]:
    lookup = {}
    for sheet in snapshot.get("sheets") or []:
        name = sheet.get("sheet_name")
        for coord, cell in _cell_lookup_sheet(sheet).items():
            if coord:
                lookup[(name, coord)] = cell
    return lookup


def _cell_at(sheet: dict, coordinate: str) -> dict | None:
    return _cell_lookup_sheet(sheet).get(str(coordinate).upper())


def _cells_equivalent(left: dict | None, right: dict | None) -> bool:
    if left is None and right is None:
        return True
    if left is None:
        return _is_empty_cell(right)
    if right is None:
        return _is_empty_cell(left)
    if bool(left.get("is_formula")) != bool(right.get("is_formula")):
        return False
    left_kind, right_kind = _cell_kind(left), _cell_kind(right)
    if left_kind is not None and right_kind is not None and left_kind != right_kind:
        return False
    if not _scalar_equivalent(left.get("value"), right.get("value")):
        return False
    return semantic_styles_equivalent(left, right)


def _cell_kind(cell):
    kind = cell.get('data_type') if cell else None
    return 's' if kind in {'s','str','inlineStr'} else kind


def _literal_type_matches(cell,value):
    actual = _cell_kind(cell)
    wanted = 'b' if type(value) is bool else 's' if isinstance(value,str) else 'n' if type(value) in (int,float) else None
    return actual is None or wanted is None or actual == wanted


def _is_empty_cell(cell: dict | None) -> bool:
    if not cell:
        return True
    value = cell.get("value")
    return value in (None, "") and not cell.get("is_formula")


def _is_formula_cell(cell: dict | None) -> bool:
    if not cell:
        return False
    return bool(cell.get("is_formula")) or str(cell.get("value") or "").startswith("=")


def _cell_text(cell: dict | None) -> str | None:
    if not cell or _is_formula_cell(cell):
        return None
    value = cell.get("value")
    if value is None:
        return None
    text = str(value)
    return text if text.strip() else None


def _cell_number(cell: dict | None):
    if not cell or _is_formula_cell(cell):
        return None
    value = cell.get("value")
    if isinstance(value, bool) or value in (None, ""):
        return None
    if isinstance(value, (int, float)):
        return value
    try:
        if "." in str(value):
            return float(value)
        return int(value)
    except (TypeError, ValueError):
        return None


def _scalar_equivalent(left, right) -> bool:
    if left == right:
        return True
    if str(left) == str(right):
        return True
    try:
        return math.isclose(float(left), float(right), rel_tol=1e-12, abs_tol=1e-12)
    except (TypeError, ValueError):
        return False


def _normalize_merges(ranges: list[str]) -> set[str]:
    return {str(item).upper() for item in ranges if item}


def _assert_inputs_oracle_free(payload: Any) -> None:
    leaked = _find_forbidden_fields(payload)
    if leaked:
        raise OracleVisibleToExecutionError(
            f"Oracle/golden fields leaked into plan-effect verifier: {', '.join(leaked)}"
        )


def _find_forbidden_fields(payload: Any, depth: int = 0) -> list[str]:
    if depth > 6 or not isinstance(payload, (dict, list, tuple)):
        return []
    found: list[str] = []
    if isinstance(payload, dict):
        for key, value in payload.items():
            name = str(key)
            if name in FORBIDDEN_DECISION_FIELDS:
                found.append(name)
            else:
                found.extend(_find_forbidden_fields(value, depth + 1))
    else:
        for item in payload:
            found.extend(_find_forbidden_fields(item, depth + 1))
    return sorted(set(found))


def _report(*, status: str, operation: str | None, errors: list[str], codes: list[str], checks: dict) -> dict:
    unique_codes = list(dict.fromkeys(codes))
    unique_errors = list(dict.fromkeys(errors))
    return {
        "status": status,
        "operation": operation,
        "errors": unique_errors,
        "failure_codes": unique_codes,
        "checks": checks,
        "oracle_fields_used": False,
        "golden_fields_used": False,
        "used_operation_family": False,
        "source": "plan_effect_verifier",
    }


def _verified_formula_text(value):
    from .formula_contract import snapshot_formula_text
    return snapshot_formula_text(value)
