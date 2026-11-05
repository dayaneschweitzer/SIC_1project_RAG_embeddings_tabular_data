from __future__ import annotations

from datetime import date, datetime
from decimal import Decimal, InvalidOperation
from pathlib import Path

from openpyxl import load_workbook
from openpyxl.utils import get_column_letter, range_boundaries
from openpyxl.worksheet.formula import ArrayFormula, DataTableFormula

from .change_mask import column_position_to_index, plan_mask_errors
from .merge_policy import merge_anchor_coordinate, row_col_to_coordinate
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

    workbook.save(str(output_path))
    workbook.close()
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

    workbook.save(str(output_path))
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
    sheet.insert_cols(col_index)
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
    copied = workbook.copy_worksheet(workbook[source_name])
    copied.title = new_name
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
