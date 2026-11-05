"""Rótulos estruturais e de arquitetura; a complexidade é metadado, sem papel decisório."""
from __future__ import annotations

from pathlib import Path
from openpyxl import load_workbook

SOURCE_REFERENCE = "exp1-deterministic-baseline-v1"
SOURCE_COMMIT = "ccc007e74a00ef2d72ff9eb0bdad6363a578a514"

CONDITION_LABELS = {
    "C1": "RAG single-agent com edição controlada",
    "C2": "RAG multiagente com edição controlada",
}

MODIFICATION_ARCHITECTURES = {
    "C1": "single-agent-rag-modification",
    "C2": "multiagent-rag-modification-without-repair",
}

BLOCK_LABELS = {
    "sheet_overview": "Visão geral estrutural da aba",
    "used_range": "Intervalo usado da aba",
    "header_context": "Cabeçalhos candidatos e contexto das colunas",
    "row_block": "Linha com cabeçalhos associados",
    "column_block": "Coluna com contexto de cabeçalho",
    "cell_reference_block": "Célula com fórmula ou pertencente a uma mesclagem",
}

COMPLEXITY_LABELS = {
    "moderada": "Complexidade estrutural moderada",
    "alta": "Complexidade estrutural alta",
    "critica": "Complexidade estrutural crítica",
}


def inspect_workbook(path: Path) -> dict[str, int | str]:
    is_xlsm = path.suffix.lower() == ".xlsm"
    workbook = load_workbook(path, data_only=False, read_only=False, keep_vba=is_xlsm)
    try:
        used_cells = 0
        formulas = 0
        merged_cells = 0
        tables = 0
        validations = 0
        conditional_formatting = 0
        for sheet in workbook.worksheets:
            cells = getattr(sheet, "_cells", {}).values()
            for cell in cells:
                if cell.value not in (None, ""):
                    used_cells += 1
                    if cell.data_type == "f":
                        formulas += 1
            merged_cells += len(sheet.merged_cells.ranges)
            tables += len(sheet.tables)
            validations += len(sheet.data_validations.dataValidation) if sheet.data_validations else 0
            conditional_formatting += len(sheet.conditional_formatting)
        return {
            "sheets_count": len(workbook.worksheets),
            "used_cells_count": used_cells,
            "formulas_count": formulas,
            "merged_cells_count": merged_cells,
            "tables_count": tables,
            "data_validations_count": validations,
            "conditional_formatting_count": conditional_formatting,
            "defined_names_count": len(workbook.defined_names),
            "has_macros": str(bool(is_xlsm and workbook.vba_archive)).lower(),
        }
    finally:
        workbook.close()


def classify_structural_complexity(metrics: dict[str, int | str]) -> tuple[int, str]:
    sheets = int(metrics["sheets_count"])
    cells = int(metrics["used_cells_count"])
    formulas = int(metrics["formulas_count"])
    merged = int(metrics["merged_cells_count"])
    tables = int(metrics["tables_count"])
    validations = int(metrics["data_validations_count"])
    conditional = int(metrics["conditional_formatting_count"])
    defined_names = int(metrics["defined_names_count"])
    has_macros = str(metrics["has_macros"]).lower() == "true"
    formula_density = formulas / cells if cells else 0

    score = 0
    if sheets > 1:
        score += 3
    if sheets >= 4:
        score += 2
    if cells >= 10000:
        score += 2
    elif cells >= 1000:
        score += 1
    if formulas > 0:
        score += 2
    if formulas >= 500:
        score += 4
    elif formulas >= 100:
        score += 3
    elif formula_density >= 0.30:
        score += 3
    elif formula_density >= 0.10:
        score += 1
    if merged > 0:
        score += 2
    if merged >= 10:
        score += 2
    if tables > 0:
        score += 2
    if validations > 0:
        score += 2
    if conditional > 0:
        score += 2
    if defined_names > 0:
        score += 1
    if has_macros:
        score += 5

    special_categories = sum(
        value > 0 for value in (merged, tables, validations, conditional, defined_names)
    )
    critical = (
        has_macros
        or formulas >= 500
        or (sheets > 1 and formulas > 0 and (formula_density >= 0.15 or special_categories > 0))
        or special_categories >= 3
        or score >= 12
    )
    high = (
        sheets > 1
        or cells >= 10000
        or formulas > 0
        or special_categories > 0
        or score >= 5
    )
    if critical:
        return score, "critica"
    if high:
        return score, "alta"
    return score, "moderada"
