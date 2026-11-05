from openpyxl import load_workbook
from openpyxl.worksheet.formula import ArrayFormula, DataTableFormula
from pathlib import Path
import json
import zipfile
import re

from .semantic_style import SEMANTIC_STYLE_FIELDS, extract_cell_semantic_style

STRUCTURAL_SNAPSHOT_SCHEMA_VERSION = "semantic_style_v1"
REQUIRED_CELL_SEMANTIC_STYLE_FIELDS = tuple(
    field for field in SEMANTIC_STYLE_FIELDS if field != "number_format"
)


def extract_xml_encoding_info(xlsx_path: str) -> dict:
    """
    Inspeciona os XMLs internos do arquivo XLSX para identificar declarações de encoding.
    XLSX é um arquivo ZIP contendo XMLs internos.
    """
    encoding_info = {}

    try:
        with zipfile.ZipFile(xlsx_path, "r") as archive:
            xml_files = [name for name in archive.namelist() if name.endswith(".xml")]

            for xml_file in xml_files:
                with archive.open(xml_file) as file:
                    first_bytes = file.read(200)
                    first_text = first_bytes.decode("utf-8", errors="ignore")

                    match = re.search(r'encoding=["\']([^"\']+)["\']', first_text)

                    encoding_info[xml_file] = {
                        "declared_encoding": match.group(1) if match else None
                    }

    except Exception as error:
        encoding_info["error"] = str(error)

    return encoding_info


def extract_workbook_metadata(workbook, xlsx_path: str) -> dict:
    props = workbook.properties

    metadata = {
        "creator": props.creator,
        "last_modified_by": props.lastModifiedBy,
        "created": props.created.isoformat() if props.created else None,
        "modified": props.modified.isoformat() if props.modified else None,
        "title": props.title,
        "subject": props.subject,
        "description": props.description,
        "keywords": props.keywords,
        "category": props.category,
        "content_status": props.contentStatus,
        "language": props.language,
        "version": props.version,
        "defined_names": [],
        "xml_encoding_info": extract_xml_encoding_info(xlsx_path)
    }

    for defined_name in workbook.defined_names.values():
        metadata["defined_names"].append({
            "name": defined_name.name,
            "comment": defined_name.comment,
            "attr_text": defined_name.attr_text
        })

    return metadata


def extract_sheet_metadata(sheet) -> dict:
    tables = []

    for table_name in sheet.tables:
        table_obj = sheet.tables[table_name]
        tables.append({
            "name": table_name,
            "ref": getattr(table_obj, "ref", str(table_obj))
        })

    data_validations_count = 0
    if sheet.data_validations is not None:
        data_validations_count = len(sheet.data_validations.dataValidation)

    conditional_formatting_count = len(sheet.conditional_formatting)

    return {
        "sheet_name": sheet.title,
        "max_row": sheet.max_row,
        "max_column": sheet.max_column,
        "sheet_state": sheet.sheet_state,
        "merged_cells": [str(rng) for rng in sheet.merged_cells.ranges],
        "tables": tables,
        "data_validations_count": data_validations_count,
        "conditional_formatting_count": conditional_formatting_count,
        "freeze_panes": str(sheet.freeze_panes) if sheet.freeze_panes else None
    }


def extract_cell_data(cell) -> dict:
    value = serialize_cell_value(cell.value)
    data = {
        "coordinate": cell.coordinate,
        "row": cell.row,
        "column": cell.column,
        "value": value,
        "data_type": cell.data_type,
        "is_formula": is_formula_value(cell.value),
        "number_format": cell.number_format,
        "style_id": cell.style_id,
        "font_id": cell._style.fontId if cell.has_style else None,
        "fill_id": cell._style.fillId if cell.has_style else None,
        "border_id": cell._style.borderId if cell.has_style else None,
        "alignment_id": cell._style.alignmentId if cell.has_style else None,
        "protection_id": cell._style.protectionId if cell.has_style else None,
    }
    data.update(extract_cell_semantic_style(cell))
    return data


def serialize_cell_value(value) -> str:
    if isinstance(value, ArrayFormula):
        return f"ArrayFormula(ref={value.ref}, text={value.text})"
    if isinstance(value, DataTableFormula):
        return f"DataTableFormula(ref={value.ref})"
    return str(value)


def is_formula_value(value) -> bool:
    return (
        isinstance(value, (ArrayFormula, DataTableFormula))
        or (isinstance(value, str) and value.startswith("="))
    )


def is_snapshot_schema_compatible(snapshot: dict | None) -> bool:
    """True iff the snapshot carries the semantic-style schema the verifier expects."""
    if not isinstance(snapshot, dict):
        return False
    if snapshot.get("structural_snapshot_schema_version") != STRUCTURAL_SNAPSHOT_SCHEMA_VERSION:
        return False
    sheets = snapshot.get("sheets")
    if not isinstance(sheets, list):
        return False
    for sheet in sheets:
        if not isinstance(sheet, dict):
            return False
        for cell in sheet.get("cells") or []:
            if not isinstance(cell, dict):
                return False
            for field in REQUIRED_CELL_SEMANTIC_STYLE_FIELDS:
                if field not in cell:
                    return False
    return True


def load_structural_snapshot(path: str | Path) -> dict | None:
    try:
        payload = json.loads(Path(path).read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError, TypeError):
        return None
    return payload if isinstance(payload, dict) else None


def extract_structural_snapshot(xlsx_path: str) -> dict:
    workbook = load_workbook(xlsx_path, data_only=False)
    try:
        snapshot = {
            "structural_snapshot_schema_version": STRUCTURAL_SNAPSHOT_SCHEMA_VERSION,
            "file_name": Path(xlsx_path).name,
            "workbook_metadata": extract_workbook_metadata(workbook, xlsx_path),
            "sheets": []
        }

        for sheet in workbook.worksheets:
            sheet_data = extract_sheet_metadata(sheet)
            sheet_data["cells"] = []

            for row in sheet.iter_rows():
                for cell in row:
                    if cell.value is not None:
                        sheet_data["cells"].append(extract_cell_data(cell))

            snapshot["sheets"].append(sheet_data)

        return snapshot
    finally:
        workbook.close()


def save_snapshot(snapshot: dict, output_path: str) -> None:
    with open(output_path, "w", encoding="utf-8") as file:
        json.dump(snapshot, file, ensure_ascii=False, indent=2)
