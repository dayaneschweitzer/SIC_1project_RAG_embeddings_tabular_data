from __future__ import annotations

from copy import deepcopy
from datetime import date, datetime
from decimal import Decimal, InvalidOperation
import os
from pathlib import Path
from tempfile import TemporaryDirectory
import xml.etree.ElementTree as ET
from zipfile import ZipFile

from openpyxl import load_workbook
from openpyxl.utils import get_column_letter, range_boundaries
from openpyxl.worksheet.formula import ArrayFormula, DataTableFormula

from .change_mask import column_position_to_index, plan_mask_errors
from modification.merge_policy import merge_anchor_coordinate, row_col_to_coordinate
from .ooxml_inspection import MAIN, UnsupportedOOXMLFormat, inspect_ooxml
from .ooxml_serialization import preserve_source_serialization
from .ooxml_verifier_v2 import PASS, make_ooxml_contract, verify_ooxml_v2
from .parsed_intent import (
    COPY_SHEET,
    INSERT_COLUMN,
    INSERT_ROW,
    REPLACE_VALUES,
    SORT_RANGE,
    STRUCTURED_OPERATIONS,
    UNMERGE_RANGE,
    WRITE_FORMULA,
    WRITE_VALUE,
)

EXECUTOR_OPERATIONS = frozenset({"update_cell"} | set(STRUCTURED_OPERATIONS))
WRITE_VALUE_EXPRESSIONS = frozenset(
    {
        "concatenate",
        "extract_token",
        "extract_prefix",
        "lookup",
        "add",
        "layout_caption",
    }
)


class ExecutionRejected(ValueError):
    """Raised internally when a planned operation cannot be applied safely."""


def apply_change_plan(
    input_xlsx: str,
    change_plan: dict,
    output_xlsx: str,
    allowed_change_mask: dict | str | None = None,
) -> dict:
    input_path, output_path = Path(input_xlsx), Path(output_xlsx)
    if change_plan.get("operation") == INSERT_COLUMN:
        from .insert_column_audit import preflight
        try:
            preflight(input_path, change_plan["target_sheet"], column_position_to_index(change_plan["insertion_position"]))
        except Exception as exc:
            return {**_failed(input_xlsx, [str(exc)]), "limitation_code": "INSERT_DEPENDENCY_UNSUPPORTED", "preservation_status": "NOT_VERIFIED"}
    if input_path.resolve() == output_path.resolve():
        return _failed(input_xlsx, ["Output must differ from input."])
    try:
        inventory = inspect_ooxml(input_path)
    except UnsupportedOOXMLFormat as exc:
        return {**_failed(input_xlsx, [str(exc)]), "limitation_code": "OOXML_FORMAT_NOT_SUPPORTED",
                "preservation_status": "NOT_VERIFIED"}
    cell_metadata = [{"sheet": name, **reference} for name, sheet in inventory["sheets"].items()
                     for reference in sheet["cell_metadata"]]
    if cell_metadata:
        return {**_failed(input_xlsx, ["Backend cannot safely preserve referenced cell metadata."]),
                "limitation_code": "CELL_METADATA_NOT_SUPPORTED", "preservation_status": "NOT_VERIFIED",
                "unsupported_cell_metadata": cell_metadata}
    if change_plan.get("operation") == COPY_SHEET and inventory["package_objects"]:
        return {**_failed(input_xlsx, ["copy_sheet cannot preserve package thumbnail dependencies."]),
                "limitation_code": "COPY_PACKAGE_DEPENDENCY_UNSUPPORTED", "preservation_status": "NOT_VERIFIED",
                "unsupported_dependencies": {"scope": "package", "property": "package_linked_objects",
                                             "signatures": inventory["package_objects"]}}
    mapping = {name: name for name in inventory["sheets"]}
    if change_plan.get("operation") == COPY_SHEET:
        source, target = change_plan.get("target_sheet"), change_plan.get("new_sheet_name")
        if source in inventory["sheets"]:
            source_inventory = inventory["sheets"][source]
            with ZipFile(input_path) as archive:
                root = ET.fromstring(archive.read(source_inventory["part"]))
            unsupported = [tag for tag in ("tableParts", "extLst") if root.find(f"{{{MAIN}}}{tag}") is not None]
            if source_inventory["objects"]:
                unsupported.append("linked_objects")
            if unsupported:
                return {**_failed(input_xlsx, ["copy_sheet cannot safely clone: " + ", ".join(unsupported)]),
                        "limitation_code": "COPY_FEATURE_NOT_SUPPORTED", "preservation_status": "NOT_VERIFIED"}
            if target and target not in mapping:
                mapping[target] = source
    contract = make_ooxml_contract(input_path, authority="executor_backend_invariants_v1", sheet_mapping=mapping)
    output_path.parent.mkdir(parents=True, exist_ok=True)
    with TemporaryDirectory(prefix="sic_edit_", dir=output_path.parent) as temporary:
        staged = Path(temporary) / "candidate.xlsx"
        report = _apply_change_plan_unchecked(input_xlsx, change_plan, str(staged), allowed_change_mask)
        if report.get("status") != "applied":
            return report
        if change_plan.get("operation") == INSERT_COLUMN:
            from .insert_column_audit import verify_insert_column
            dependency = verify_insert_column(input_path, staged, change_plan["target_sheet"], column_position_to_index(change_plan["insertion_position"]))
            if dependency["covered_status"] != PASS:
                return {**_failed(input_xlsx, ["Insert-column dependency verification failed"]), "limitation_code": "INSERT_DEPENDENCY_VERIFICATION_FAILED", "dependency_verification": dependency}
            report["dependency_verification"] = dependency
            contract["insertion_transitions"] = [{"before": str(input_path), "after": str(staged), "sheet": change_plan["target_sheet"], "position": column_position_to_index(change_plan["insertion_position"])}]
            for rule in contract["requirements"]:
                if rule.get("property") == "validation_rules":
                    rule["mode"] = "insert_column_chain_v2"
        verification = verify_ooxml_v2(input_path, staged, contract=contract)
        if verification["covered_status"] != PASS:
            return {**_failed(input_xlsx, ["OOXML preservation could not be established; output not published."]),
                    "limitation_code": "OOXML_PRESERVATION_UNSAFE", "ooxml_verification": verification,
                    "preservation_status": verification["overall_status"]}
        os.replace(staged, output_path)
        verification["identities"]["after"]["path"] = str(output_path)
        return {**report, "output_xlsx": str(output_path), "ooxml_verification": verification,
                "preservation_status": verification["overall_status"]}


def _apply_change_plan_unchecked(input_xlsx, change_plan, output_xlsx, allowed_change_mask=None):
    operation = change_plan.get("operation")
    if operation == "update_cell":
        mask_errors = plan_mask_errors(change_plan, allowed_change_mask)
        if mask_errors:
            return _failed(input_xlsx, mask_errors)
        return _apply_update_cell(input_xlsx, change_plan, output_xlsx)

    if operation not in STRUCTURED_OPERATIONS:
        raise ValueError(f"Unsupported operation: {operation}")

    mask_errors = plan_mask_errors(change_plan, allowed_change_mask)
    if mask_errors:
        return _failed(input_xlsx, mask_errors)

    input_path = Path(input_xlsx)
    output_path = Path(output_xlsx)
    output_path.parent.mkdir(parents=True, exist_ok=True)
    workbook = load_workbook(str(input_path), data_only=False)
    try:
        changed_cells = _apply_structured_operation(workbook, change_plan, allowed_change_mask)
    except ExecutionRejected as exc:
        workbook.close()
        return _failed(str(input_path), [str(exc)])

    preserve_source_serialization(workbook, input_path, change_plan)
    workbook.save(str(output_path))
    workbook.close()
    if operation == INSERT_COLUMN:
        from .insert_column_audit import preflight
        from .insert_column_dependencies import persist_append_extent
        insertion = preflight(input_path, change_plan["target_sheet"], column_position_to_index(change_plan["insertion_position"]))
        if insertion["append"]:
            persist_append_extent(output_path, change_plan["target_sheet"], insertion["before_max_column"] + 1)
    return {
        "status": "applied",
        "input_xlsx": str(input_path),
        "output_xlsx": str(output_path),
        "changed_cells": changed_cells,
        "errors": [],
    }


def executor_supports(operation: str, expression: str | None = None) -> bool:
    if operation == "update_cell":
        return True
    if operation not in STRUCTURED_OPERATIONS:
        return False
    if operation == WRITE_VALUE:
        return expression in WRITE_VALUE_EXPRESSIONS or expression is None
    return True


def _apply_update_cell(input_xlsx: str, change_plan: dict, output_xlsx: str) -> dict:
    input_path = Path(input_xlsx)
    output_path = Path(output_xlsx)
    output_path.parent.mkdir(parents=True, exist_ok=True)

    workbook = load_workbook(str(input_path), data_only=False)
    sheet = workbook[change_plan["target_sheet"]]

    changed_cells = []
    for coordinate in change_plan.get("target_cells", []):
        cell = sheet[coordinate]
        before_value = cell.value
        before_style_id = cell.style_id
        before_number_format = cell.number_format

        cell.value = _coerce_new_value(change_plan.get("new_value"), before_value)

        changed_cells.append(
            {
                "sheet_name": change_plan["target_sheet"],
                "coordinate": coordinate,
                "before_value": None if before_value is None else str(before_value),
                "after_value": None if cell.value is None else str(cell.value),
                "style_id_before": before_style_id,
                "style_id_after": cell.style_id,
                "number_format_before": before_number_format,
                "number_format_after": cell.number_format,
            }
        )

    preserve_source_serialization(workbook, input_path, change_plan)
    workbook.save(str(output_path))
    workbook.close()
    return {
        "status": "applied",
        "input_xlsx": str(input_path),
        "output_xlsx": str(output_path),
        "changed_cells": changed_cells,
    }


def _apply_structured_operation(workbook, change_plan: dict, mask) -> list[dict]:
    operation = change_plan.get("operation")
    if operation == REPLACE_VALUES:
        return _apply_replace_values(workbook, change_plan, mask)
    if operation == WRITE_VALUE:
        return _apply_write_value(workbook, change_plan, mask)
    if operation == WRITE_FORMULA:
        return _apply_write_formula(workbook, change_plan, mask)
    if operation == "delete_row":
        return _apply_delete_row(workbook, change_plan)
    if operation == INSERT_ROW:
        return _apply_insert_row(workbook, change_plan)
    if operation == INSERT_COLUMN:
        return _apply_insert_column(workbook, change_plan)
    if operation == SORT_RANGE:
        return _apply_sort_range(workbook, change_plan)
    if operation == UNMERGE_RANGE:
        return _apply_unmerge_range(workbook, change_plan)
    if operation == COPY_SHEET:
        return _apply_copy_sheet(workbook, change_plan)
    raise ExecutionRejected(f"Operacao nao suportada: {operation}")


def _apply_delete_row(workbook, change_plan):
    sheet = _require_sheet(workbook, change_plan.get("target_sheet"))
    position = change_plan.get("deletion_position")
    if type(position) is not int or not 1 <= position <= sheet.max_row:
        raise ExecutionRejected("Invalid deletion position")
    unsafe = (bool(workbook.defined_names) or any(c.data_type == "f" for s in workbook for row in s for c in row)
              or bool(sheet.merged_cells.ranges) or bool(sheet.tables) or bool(sheet.auto_filter.ref)
              or bool(sheet.conditional_formatting) or bool(sheet.data_validations.count)
              or bool(sheet._charts) or bool(sheet._images) or bool(sheet.row_breaks.brk) or sheet.protection.sheet
              or bool(sheet.print_area) or bool(sheet.print_title_rows) or bool(sheet.print_title_cols)
              or any(c.hyperlink or c.comment for row in sheet for c in row))
    if unsafe:
        workbook._observational_diagnostics = getattr(workbook, "_observational_diagnostics", []) + [{"code": "ROW_DELETION_REFERENCES_OR_FEATURES_NOT_QUALIFIED"}]
    dimensions = {index: deepcopy(value) for index, value in sheet.row_dimensions.items() if index != position}
    sheet.delete_rows(position, 1)
    sheet.row_dimensions.clear()
    for index, value in dimensions.items():
        new_index = index - 1 if index > position else index
        value.index = new_index
        sheet.row_dimensions[new_index] = value
    return [{"sheet_name": sheet.title, "deleted_row": position}]


def _apply_replace_values(workbook, change_plan: dict, mask) -> list[dict]:
    sheet = _require_sheet(workbook, change_plan.get("target_sheet"))
    cell_range = change_plan.get("source_range")
    old_value = change_plan.get("replacement_old")
    new_value = change_plan.get("replacement_new")
    if not cell_range or old_value in (None, "") or new_value in (None, ""):
        raise ExecutionRejected("replace_values exige intervalo, valor antigo e valor novo.")
    changed = []
    for coordinate in _iter_range_coordinates(cell_range):
        if not _mask_allows_cell(sheet.title, coordinate, mask):
            continue
        cell = sheet[coordinate]
        if _is_formula_value(cell.value):
            continue
        if _cell_text(cell.value) != str(old_value):
            continue
        changed.append(_write_cell(sheet, coordinate, new_value, normalize_merge=True))
    return changed


def _apply_write_value(workbook, change_plan: dict, mask) -> list[dict]:
    sheet = _require_sheet(workbook, change_plan.get("target_sheet"))
    dest = change_plan.get("destination") or change_plan.get("target_coordinate")
    if not dest:
        raise ExecutionRejected("Destino ausente.")
    if not _mask_allows_cell(sheet.title, dest, mask):
        raise ExecutionRejected(f"celula {dest} fora de allowed_change_mask.")
    expression = change_plan.get("expression")
    if expression == "concatenate":
        value = _concatenate_sources(sheet, change_plan)
    elif expression == "extract_token":
        value = _extract_token(sheet, change_plan)
    elif expression == "extract_prefix":
        value = _extract_prefix(sheet, change_plan)
    elif expression == "lookup":
        value = _lookup_text_value(sheet, change_plan)
    elif expression == "add":
        value = _add_numeric_sources(sheet, change_plan)
    elif expression == "layout_caption" or expression is None:
        value = change_plan.get("literal_value")
        if value in (None, ""):
            value = change_plan.get("new_value")
        if value in (None, ""):
            raise ExecutionRejected("Valor literal ausente.")
    else:
        raise ExecutionRejected(f"Expressao write_value nao suportada: {expression}")
    return [_write_cell(sheet, dest, value, normalize_merge=True)]


def _apply_write_formula(workbook, change_plan: dict, mask) -> list[dict]:
    sheet = _require_sheet(workbook, change_plan.get("target_sheet"))
    dest = change_plan.get("destination") or change_plan.get("target_coordinate")
    formula = change_plan.get("formula")
    if not dest:
        raise ExecutionRejected("Destino ausente.")
    if not formula or not str(formula).startswith("="):
        raise ExecutionRejected("Formula ausente ou invalida.")
    if not _mask_allows_cell(sheet.title, dest, mask):
        raise ExecutionRejected(f"celula {dest} fora de allowed_change_mask.")
    if change_plan.get("fill_mode") == "relative_rows":
        from openpyxl.formula.translate import Translator
        coordinates = list(_iter_range_coordinates(change_plan["target_range"]))
        if not coordinates or coordinates[0] != dest or len(coordinates) > 10000:
            raise ExecutionRejected("Invalid formula fill domain")
        if any(not _mask_allows_cell(sheet.title, cell, mask) for cell in coordinates):
            raise ExecutionRejected("Formula fill exceeds the allowed mask")
        if any(merge_anchor_coordinate(sheet, cell) != cell for cell in coordinates):
            raise ExecutionRejected("Formula fill cannot target merged interiors")
        return [_write_cell(sheet, cell, Translator(str(formula), origin=dest).translate_formula(cell), normalize_merge=False)
                for cell in coordinates]
    return [_write_cell(sheet, dest, str(formula), normalize_merge=True)]


def _apply_insert_row(workbook, change_plan: dict) -> list[dict]:
    sheet = _require_sheet(workbook, change_plan.get("target_sheet"))
    position = change_plan.get("insertion_position")
    if position in (None, ""):
        raise ExecutionRejected("Indice de linha ausente.")
    row_index = int(position)
    if row_index < 1:
        raise ExecutionRejected(f"Indice de linha invalido: {position}")
    before_count = sheet.max_row or 0
    sheet.insert_rows(row_index)
    return [
        {
            "sheet_name": sheet.title,
            "coordinate": f"A{row_index}",
            "before_value": None,
            "after_value": f"inserted_row:{row_index}",
            "structural": True,
            "max_row_before": before_count,
            "max_row_after": sheet.max_row,
        }
    ]


def _apply_insert_column(workbook, change_plan: dict) -> list[dict]:
    sheet = _require_sheet(workbook, change_plan.get("target_sheet"))
    position = change_plan.get("insertion_position")
    if position in (None, ""):
        raise ExecutionRejected("Indice de coluna ausente.")
    col_index = column_position_to_index(position)
    if col_index < 1:
        raise ExecutionRejected(f"Indice de coluna invalido: {position}")
    before_count = sheet.max_column or 0
    from .insert_column_dependencies import update_dependencies
    try:
        update_dependencies(workbook, sheet.title, col_index)
    except ValueError as exc:
        workbook._observational_diagnostics = getattr(workbook, "_observational_diagnostics", []) + [{"code": "INSERT_DEPENDENCY_UNSUPPORTED", "detail": str(exc)}]
    affected_merges = [
        (str(merged), merged.min_col, merged.min_row, merged.max_col, merged.max_row)
        for merged in sheet.merged_cells.ranges
        if merged.max_col >= col_index
    ]
    for reference, _, _, _, _ in affected_merges:
        sheet.unmerge_cells(reference)
    sheet.insert_cols(col_index)
    for _, min_col, min_row, max_col, max_row in affected_merges:
        sheet.merge_cells(
            start_row=min_row,
            end_row=max_row,
            start_column=min_col + 1 if min_col >= col_index else min_col,
            end_column=max_col + 1,
        )
    letter = get_column_letter(col_index)
    return [
        {
            "sheet_name": sheet.title,
            "coordinate": f"{letter}1",
            "before_value": None,
            "after_value": f"inserted_column:{letter}",
            "structural": True,
            "max_column_before": before_count,
            "max_column_after": sheet.max_column,
        }
    ]


def _apply_sort_range(workbook, change_plan: dict) -> list[dict]:
    sheet = _require_sheet(workbook, change_plan.get("target_sheet"))
    rng = change_plan.get("sort_range") or change_plan.get("source_range")
    if not rng:
        raise ExecutionRejected("Intervalo de ordenacao ausente.")
    if change_plan.get("sort_key") not in {None, "first_column"}:
        raise ExecutionRejected("Chave de ordenacao nao suportada.")
    min_col, min_row, max_col, max_row = _valid_range(rng)
    before = []
    rows = []
    for row in range(min_row, max_row + 1):
        values = [sheet.cell(row, col).value for col in range(min_col, max_col + 1)]
        before.append(list(values))
        rows.append(values)
    rows.sort(key=lambda item: (item[0] is None, str(item[0]).lower() if item[0] is not None else ""))
    changed = []
    for offset, values in enumerate(rows):
        for col_offset, value in enumerate(values):
            coordinate = row_col_to_coordinate(min_row + offset, min_col + col_offset)
            previous = before[offset][col_offset]
            sheet.cell(min_row + offset, min_col + col_offset).value = value
            if previous != value:
                changed.append(
                    {
                        "sheet_name": sheet.title,
                        "coordinate": coordinate,
                        "before_value": None if previous is None else str(previous),
                        "after_value": None if value is None else str(value),
                    }
                )
    return changed


def _apply_unmerge_range(workbook, change_plan: dict) -> list[dict]:
    sheet = _require_sheet(workbook, change_plan.get("target_sheet"))
    rng = change_plan.get("source_range")
    if not rng:
        raise ExecutionRejected("Intervalo a desmesclar ausente.")
    wanted = str(rng).upper()
    existing = {str(item).upper() for item in sheet.merged_cells.ranges}
    if wanted not in existing:
        raise ExecutionRejected(f"Merge inexistente: {wanted}")
    sheet.unmerge_cells(wanted)
    return [
        {
            "sheet_name": sheet.title,
            "coordinate": wanted.split(":")[0],
            "before_value": wanted,
            "after_value": "unmerged",
            "structural": True,
        }
    ]


def _apply_copy_sheet(workbook, change_plan: dict) -> list[dict]:
    source_name = change_plan.get("target_sheet")
    new_name = change_plan.get("new_sheet_name")
    if not source_name or not new_name:
        raise ExecutionRejected("Copia de aba exige origem e nome novo.")
    if source_name not in workbook.sheetnames:
        raise ExecutionRejected(f"Aba inexistente: {source_name}")
    if new_name in workbook.sheetnames:
        raise ExecutionRejected(f"Nome de aba ja existe: {new_name}")
    source = workbook[source_name]
    copied = workbook.copy_worksheet(source)
    copied.title = new_name
    copied.views = deepcopy(source.views)
    copied.data_validations = deepcopy(source.data_validations)
    copied.conditional_formatting = deepcopy(source.conditional_formatting)
    return [
        {
            "sheet_name": new_name,
            "coordinate": "A1",
            "before_value": None,
            "after_value": f"copied_from:{source_name}",
            "structural": True,
        }
    ]


def _concatenate_sources(sheet, change_plan: dict) -> str:
    sources = list(change_plan.get("source_cells") or [])
    if len(sources) < 2:
        raise ExecutionRejected("Concatenacao exige duas celulas fonte.")
    left = _require_text_source(sheet, sources[0])
    right = _require_text_source(sheet, sources[1])
    separator = change_plan.get("separator")
    if separator is None:
        separator = " "
    return f"{left}{separator}{right}"


def _extract_token(sheet, change_plan: dict) -> str:
    sources = list(change_plan.get("source_cells") or [])
    if not sources:
        raise ExecutionRejected("Extracao exige celula fonte.")
    text = _require_text_source(sheet, sources[0])
    return text.split()[0]


def _extract_prefix(sheet, change_plan: dict) -> str:
    sources = list(change_plan.get("source_cells") or [])
    length = change_plan.get("extract_length")
    if not sources:
        raise ExecutionRejected("Extracao exige celula fonte.")
    if not length:
        raise ExecutionRejected("Comprimento do prefixo ausente.")
    text = _require_text_source(sheet, sources[0])
    return text[: int(length)]


def _lookup_text_value(sheet, change_plan: dict) -> str:
    sources = list(change_plan.get("source_cells") or [])
    if not sources:
        raise ExecutionRejected("Lookup exige celula chave.")
    key = _require_text_source(sheet, sources[0])
    mapping = _sheet_text_lookup(sheet)
    if key not in mapping:
        raise ExecutionRejected(f"chave de lookup nao encontrada: {key}")
    return mapping[key]


def _sheet_text_lookup(sheet) -> dict[str, str]:
    max_row = sheet.max_row or 1
    max_col = sheet.max_column or 1
    text_cols = []
    for col in range(1, max_col + 1):
        n_text = sum(
            1 for row in range(1, max_row + 1) if _text_value(sheet.cell(row, col).value) is not None
        )
        if n_text >= 2:
            text_cols.append(col)
    if len(text_cols) < 2:
        text_cols = [
            col
            for col in range(1, max_col + 1)
            if any(_text_value(sheet.cell(row, col).value) is not None for row in range(1, max_row + 1))
        ]
    if len(text_cols) < 2:
        raise ExecutionRejected("tabela de lookup textual nao encontrada.")
    key_col, val_col = text_cols[0], text_cols[1]
    mapping: dict[str, str] = {}
    for row in range(1, max_row + 1):
        key = _text_value(sheet.cell(row, key_col).value)
        value = _text_value(sheet.cell(row, val_col).value)
        if key and value:
            mapping.setdefault(key, value)
    return mapping


def _add_numeric_sources(sheet, change_plan: dict):
    sources = list(change_plan.get("source_cells") or [])
    if len(sources) < 2:
        raise ExecutionRejected("Soma constante exige duas celulas fonte.")
    left = _require_numeric_source(sheet, sources[0])
    right = _require_numeric_source(sheet, sources[1])
    total = left + right
    if isinstance(left, int) and isinstance(right, int):
        return int(total)
    return float(total)


def _write_cell(sheet, coordinate: str, value, *, normalize_merge: bool) -> dict:
    target = merge_anchor_coordinate(sheet, coordinate) if normalize_merge else str(coordinate).upper()
    cell = sheet[target]
    before_value = cell.value
    cell.value = value
    return {
        "sheet_name": sheet.title,
        "coordinate": target,
        "before_value": None if before_value is None else str(before_value),
        "after_value": None if cell.value is None else str(cell.value),
        "requested_coordinate": str(coordinate).upper(),
    }


def _require_sheet(workbook, sheet_name: str | None):
    if not sheet_name or sheet_name not in workbook.sheetnames:
        raise ExecutionRejected(f"Aba inexistente: {sheet_name}")
    return workbook[sheet_name]


def _require_text_source(sheet, coordinate: str) -> str:
    try:
        value = sheet[coordinate].value
    except Exception as exc:
        raise ExecutionRejected(f"referencia invalida: {coordinate}") from exc
    if _is_formula_value(value):
        raise ExecutionRejected(f"celula fonte e formula: {coordinate}")
    text = _text_value(value)
    if text is None:
        raise ExecutionRejected(f"celula fonte vazia ou nao textual: {coordinate}")
    return text


def _require_numeric_source(sheet, coordinate: str):
    try:
        value = sheet[coordinate].value
    except Exception as exc:
        raise ExecutionRejected(f"referencia invalida: {coordinate}") from exc
    number = _numeric_value(value)
    if number is None:
        raise ExecutionRejected(f"celula fonte vazia ou nao numerica: {coordinate}")
    return number


def _text_value(value) -> str | None:
    if value is None or _is_formula_value(value) or isinstance(value, bool):
        return None
    if isinstance(value, str) and value.strip() and not value.startswith("="):
        return value
    return None


def _numeric_value(value):
    if _is_formula_value(value) or isinstance(value, bool) or value is None:
        return None
    if isinstance(value, int) and not isinstance(value, bool):
        return value
    if isinstance(value, float):
        if value != value or value in {float("inf"), float("-inf")}:
            return None
        return value
    if isinstance(value, str):
        raise ExecutionRejected(f"coercao numerica recusada para texto: {value!r}")
    return None


def _cell_text(value) -> str | None:
    if value is None or _is_formula_value(value):
        return None
    if isinstance(value, str):
        return value
    return str(value)


def _is_formula_value(value) -> bool:
    return isinstance(value, (ArrayFormula, DataTableFormula)) or (
        isinstance(value, str) and value.startswith("=")
    )


def _iter_range_coordinates(cell_range: str) -> list[str]:
    min_col, min_row, max_col, max_row = _valid_range(cell_range)
    return [
        row_col_to_coordinate(row, col)
        for row in range(min_row, max_row + 1)
        for col in range(min_col, max_col + 1)
    ]


def _valid_range(cell_range: str) -> tuple[int, int, int, int]:
    try:
        return range_boundaries(str(cell_range).upper())
    except ValueError as exc:
        raise ExecutionRejected(f"intervalo invalido: {cell_range}") from exc


def _mask_allows_cell(sheet_name: str, coordinate: str, mask) -> bool:
    from .change_mask import cell_allowed

    return cell_allowed(sheet_name, coordinate, mask)


def _failed(input_xlsx: str, errors: list[str]) -> dict:
    return {
        "status": "failed",
        "input_xlsx": input_xlsx,
        "output_xlsx": None,
        "changed_cells": [],
        "errors": errors,
    }


def _coerce_new_value(new_value, before_value):
    if not isinstance(new_value, str):
        return new_value

    if isinstance(before_value, bool):
        return new_value
    if isinstance(before_value, int):
        try:
            return int(Decimal(new_value))
        except (InvalidOperation, ValueError):
            return new_value
    if isinstance(before_value, float):
        try:
            return float(Decimal(new_value))
        except (InvalidOperation, ValueError):
            return new_value
    if isinstance(before_value, datetime):
        try:
            return datetime.fromisoformat(new_value)
        except ValueError:
            return new_value
    if isinstance(before_value, date):
        try:
            return date.fromisoformat(new_value)
        except ValueError:
            return new_value
    return new_value
