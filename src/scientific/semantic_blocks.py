from collections import Counter, defaultdict
import json
import re
import unicodedata

from openpyxl.utils import get_column_letter, range_boundaries
from openpyxl.utils.cell import coordinate_to_tuple


SCHEMA_VERSION = "1.0"


def build_semantic_blocks(snapshot: dict, snapshot_path: str | None = None) -> dict:
    """
    Constroi blocos semanticos deterministas a partir do snapshot estrutural.

    A representacao gerada nao substitui o structural_snapshot.json: ela cria
    unidades recuperaveis auxiliares, sempre com rastreabilidade para as
    celulas originais.
    """
    return {
        "schema_version": SCHEMA_VERSION,
        "source_file": snapshot.get("file_name"),
        "snapshot_file": str(snapshot_path) if snapshot_path else None,
        "blocks": list(iter_semantic_blocks(snapshot, snapshot_path)),
    }


def iter_semantic_blocks(snapshot: dict, snapshot_path: str | None = None):

    for sheet_index, sheet in enumerate(snapshot.get("sheets", []), start=1):
        context = _build_sheet_context(sheet)
        sheet_slug = _slugify(sheet.get("sheet_name", f"sheet_{sheet_index}"))

        yield (
            _build_sheet_overview_block(
                snapshot=snapshot,
                snapshot_path=snapshot_path,
                sheet=sheet,
                sheet_index=sheet_index,
                sheet_slug=sheet_slug,
                context=context,
            )
        )

        if not context["cells"]:
            continue

        yield (
            _build_used_range_block(
                snapshot=snapshot,
                snapshot_path=snapshot_path,
                sheet=sheet,
                sheet_index=sheet_index,
                sheet_slug=sheet_slug,
                context=context,
            )
        )

        yield (
            _build_header_context_block(
                snapshot=snapshot,
                snapshot_path=snapshot_path,
                sheet=sheet,
                sheet_index=sheet_index,
                sheet_slug=sheet_slug,
                context=context,
            )
        )

        for row_number in sorted(context["rows"]):
            yield (
                _build_row_block(
                    snapshot=snapshot,
                    snapshot_path=snapshot_path,
                    sheet=sheet,
                    sheet_index=sheet_index,
                    sheet_slug=sheet_slug,
                    context=context,
                    row_number=row_number,
                )
            )

        for column_number in sorted(context["columns"]):
            yield (
                _build_column_block(
                    snapshot=snapshot,
                    snapshot_path=snapshot_path,
                    sheet=sheet,
                    sheet_index=sheet_index,
                    sheet_slug=sheet_slug,
                    context=context,
                    column_number=column_number,
                )
            )

        for cell in context["cells"]:
            if cell.get("is_formula") or context["merged_ranges_by_cell"].get(cell["coordinate"]):
                yield (
                    _build_cell_reference_block(
                        snapshot=snapshot,
                        snapshot_path=snapshot_path,
                        sheet=sheet,
                        sheet_index=sheet_index,
                        sheet_slug=sheet_slug,
                        context=context,
                        cell=cell,
                    )
                )


def save_semantic_blocks(semantic_blocks: dict, output_path: str) -> None:
    with open(output_path, "w", encoding="utf-8") as file:
        json.dump(semantic_blocks, file, ensure_ascii=False, indent=2)


def summarize_semantic_blocks(semantic_blocks: dict) -> dict:
    blocks = semantic_blocks.get("blocks", [])
    block_types = Counter(block.get("block_type") for block in blocks)
    referenced_cells = set()

    for block in blocks:
        sheet_name = block.get("sheet_name", "")
        for cell in block.get("cells", []):
            coordinate = cell.get("coordinate") if isinstance(cell, dict) else cell
            if coordinate:
                referenced_cells.add(f"{sheet_name}!{coordinate}")

    return {
        "total_semantic_blocks": len(blocks),
        "semantic_block_types": dict(sorted(block_types.items())),
        "total_referenced_cells": len(referenced_cells),
    }


def _build_sheet_context(sheet: dict) -> dict:
    cells = sorted(
        sheet.get("cells", []),
        key=lambda cell: (int(cell.get("row", 0)), int(cell.get("column", 0))),
    )

    rows = defaultdict(list)
    columns = defaultdict(list)

    for cell in cells:
        rows[int(cell["row"])].append(cell)
        columns[int(cell["column"])].append(cell)

    for row_cells in rows.values():
        row_cells.sort(key=lambda cell: int(cell["column"]))

    for column_cells in columns.values():
        column_cells.sort(key=lambda cell: int(cell["row"]))

    merged_ranges_by_cell = _map_merged_ranges_by_cell(sheet.get("merged_cells", []), cells)
    header_candidates = _detect_header_candidates(rows)
    headers_by_column = _build_headers_by_column(header_candidates, columns)

    return {
        "cells": cells,
        "rows": rows,
        "columns": columns,
        "used_range": _range_for_cells(cells),
        "merged_ranges_by_cell": merged_ranges_by_cell,
        "header_candidates": header_candidates,
        "headers_by_column": headers_by_column,
    }


def _build_sheet_overview_block(
    snapshot: dict,
    snapshot_path: str | None,
    sheet: dict,
    sheet_index: int,
    sheet_slug: str,
    context: dict,
) -> dict:
    cells = context["cells"]
    range_value = context["used_range"]
    formulas_count = sum(1 for cell in cells if cell.get("is_formula"))
    tables_count = len(sheet.get("tables", []))
    merged_count = len(sheet.get("merged_cells", []))
    validations_count = sheet.get("data_validations_count", 0)
    conditional_count = sheet.get("conditional_formatting_count", 0)

    content = (
        f"Aba {sheet.get('sheet_name')} com {sheet.get('max_row')} linhas, "
        f"{sheet.get('max_column')} colunas, {len(cells)} celulas preenchidas, "
        f"{formulas_count} formulas, {merged_count} regioes mescladas, "
        f"{tables_count} tabelas internas, {validations_count} validacoes e "
        f"{conditional_count} formatacoes condicionais."
    )

    retrieval_text = (
        f"Resumo estrutural da aba {sheet.get('sheet_name')} do arquivo "
        f"{snapshot.get('file_name')}. Linhas: {sheet.get('max_row')}. "
        f"Colunas: {sheet.get('max_column')}. Celulas preenchidas: {len(cells)}. "
        f"Formulas: {formulas_count}. Celulas mescladas: {merged_count}. "
        f"Tabelas internas: {tables_count}. Validacoes: {validations_count}. "
        f"Formatacao condicional: {conditional_count}."
    )

    return _base_block(
        snapshot=snapshot,
        snapshot_path=snapshot_path,
        sheet=sheet,
        block_id=_block_id(sheet_index, sheet_slug, "sheet_overview", "overview"),
        block_type="sheet_overview",
        range_value=range_value,
        content=content,
        retrieval_text=retrieval_text,
        cells=cells,
        header_context=_header_context_summary(context),
        row_context={},
        column_context={},
        structural_metadata={
            "max_row": sheet.get("max_row"),
            "max_column": sheet.get("max_column"),
            "filled_cells": len(cells),
            "formulas": formulas_count,
            "merged_cells": sheet.get("merged_cells", []),
            "tables": sheet.get("tables", []),
            "data_validations_count": validations_count,
            "conditional_formatting_count": conditional_count,
            "freeze_panes": sheet.get("freeze_panes"),
            "sheet_state": sheet.get("sheet_state"),
        },
    )


def _build_used_range_block(
    snapshot: dict,
    snapshot_path: str | None,
    sheet: dict,
    sheet_index: int,
    sheet_slug: str,
    context: dict,
) -> dict:
    cells = context["cells"]
    range_value = context["used_range"]
    content = (
        f"Intervalo usado {range_value} da aba {sheet.get('sheet_name')} "
        f"com {len(cells)} celulas preenchidas."
    )
    retrieval_text = (
        f"Intervalo usado da aba {sheet.get('sheet_name')}: {range_value}. "
        f"Celulas preenchidas e valores: {_format_cell_values(cells)}."
    )

    return _base_block(
        snapshot=snapshot,
        snapshot_path=snapshot_path,
        sheet=sheet,
        block_id=_block_id(sheet_index, sheet_slug, "used_range", _range_identifier(range_value)),
        block_type="used_range",
        range_value=range_value,
        content=content,
        retrieval_text=retrieval_text,
        cells=cells,
        header_context=_header_context_summary(context),
        row_context={"rows": sorted(context["rows"])},
        column_context={"columns": _column_context_list(context["columns"])},
        structural_metadata={
            "filled_cells": len(cells),
            "min_row": min(int(cell["row"]) for cell in cells),
            "max_row": max(int(cell["row"]) for cell in cells),
            "min_column": min(int(cell["column"]) for cell in cells),
            "max_column": max(int(cell["column"]) for cell in cells),
        },
    )


def _build_header_context_block(
    snapshot: dict,
    snapshot_path: str | None,
    sheet: dict,
    sheet_index: int,
    sheet_slug: str,
    context: dict,
) -> dict:
    header_cells = []
    for candidate in context["header_candidates"]:
        header_cells.extend(candidate["cells"])

    if not header_cells:
        header_cells = context["cells"][:1]

    range_value = _range_for_cells(header_cells)
    candidates_text = "; ".join(
        f"linha {candidate['row']} ({', '.join(candidate['reasons'])})"
        for candidate in context["header_candidates"]
    )
    if not candidates_text:
        candidates_text = "nenhuma linha candidata identificada"

    headers_text = _format_headers_by_column(context["headers_by_column"])
    content = (
        f"Contexto de cabecalhos da aba {sheet.get('sheet_name')}: "
        f"{candidates_text}. Cabecalhos por coluna: {headers_text}."
    )
    retrieval_text = (
        f"Cabecalhos candidatos da aba {sheet.get('sheet_name')}. "
        f"Linhas candidatas: {candidates_text}. "
        f"Cabecalhos associados a colunas: {headers_text}."
    )

    return _base_block(
        snapshot=snapshot,
        snapshot_path=snapshot_path,
        sheet=sheet,
        block_id=_block_id(sheet_index, sheet_slug, "header_context", "headers"),
        block_type="header_context",
        range_value=range_value,
        content=content,
        retrieval_text=retrieval_text,
        cells=header_cells,
        header_context=_header_context_summary(context),
        row_context={"candidate_rows": [candidate["row"] for candidate in context["header_candidates"]]},
        column_context={"headers_by_column": context["headers_by_column"]},
        structural_metadata={
            "heuristics": [
                "linha com maioria de valores textuais",
                "linha anterior a uma regiao com dados",
                "primeira linha preenchida relevante da aba",
            ],
            "candidate_count": len(context["header_candidates"]),
        },
    )


def _build_row_block(
    snapshot: dict,
    snapshot_path: str | None,
    sheet: dict,
    sheet_index: int,
    sheet_slug: str,
    context: dict,
    row_number: int,
) -> dict:
    cells = context["rows"][row_number]
    range_value = _range_for_cells(cells)
    headers = _associated_headers_for_row(row_number, cells, context)
    is_header_candidate = any(candidate["row"] == row_number for candidate in context["header_candidates"])
    formulas = [cell for cell in cells if cell.get("is_formula")]

    content = (
        f"Linha {row_number} da aba {sheet.get('sheet_name')} com "
        f"{len(cells)} celulas preenchidas: {_format_cell_values(cells)}."
    )
    retrieval_text = (
        f"Linha {row_number} da aba {sheet.get('sheet_name')}. "
        f"Valores: {_format_cell_values(cells)}. "
        f"Cabecalhos associados: {_format_headers_by_column(headers)}."
    )

    return _base_block(
        snapshot=snapshot,
        snapshot_path=snapshot_path,
        sheet=sheet,
        block_id=_block_id(sheet_index, sheet_slug, "row_block", f"row_{row_number:06d}"),
        block_type="row_block",
        range_value=range_value,
        content=content,
        retrieval_text=retrieval_text,
        cells=cells,
        header_context={
            "associated_headers": headers,
            "is_header_candidate": is_header_candidate,
        },
        row_context={
            "row_number": row_number,
            "cell_count": len(cells),
            "is_header_candidate": is_header_candidate,
            "formulas": [_cell_reference(cell) for cell in formulas],
        },
        column_context={"columns": _column_context_list({cell["column"]: [cell] for cell in cells})},
        structural_metadata={
            "predominant_type": _predominant_type(cells),
            "has_formula": bool(formulas),
        },
    )


def _build_column_block(
    snapshot: dict,
    snapshot_path: str | None,
    sheet: dict,
    sheet_index: int,
    sheet_slug: str,
    context: dict,
    column_number: int,
) -> dict:
    cells = context["columns"][column_number]
    range_value = _range_for_cells(cells)
    column_letter = get_column_letter(column_number)
    header = context["headers_by_column"].get(str(column_number))
    header_text = header.get("value") if header else "sem cabecalho identificado"

    content = (
        f"Coluna {column_letter} ({column_number}) da aba {sheet.get('sheet_name')} "
        f"com cabecalho {header_text} e {len(cells)} celulas preenchidas."
    )
    retrieval_text = (
        f"Coluna {column_letter} da aba {sheet.get('sheet_name')}. "
        f"Cabecalho: {header_text}. Tipo predominante: {_predominant_type(cells)}. "
        f"Valores: {_format_cell_values(cells)}."
    )

    return _base_block(
        snapshot=snapshot,
        snapshot_path=snapshot_path,
        sheet=sheet,
        block_id=_block_id(sheet_index, sheet_slug, "column_block", f"col_{column_number:04d}"),
        block_type="column_block",
        range_value=range_value,
        content=content,
        retrieval_text=retrieval_text,
        cells=cells,
        header_context={"column_header": header},
        row_context={"rows": sorted(int(cell["row"]) for cell in cells)},
        column_context={
            "column_number": column_number,
            "column_letter": column_letter,
            "header": header,
            "cell_count": len(cells),
            "predominant_type": _predominant_type(cells),
        },
        structural_metadata={
            "has_formula": any(cell.get("is_formula") for cell in cells),
            "data_types": dict(sorted(Counter(cell.get("data_type") for cell in cells).items())),
        },
    )


def _build_cell_reference_block(
    snapshot: dict,
    snapshot_path: str | None,
    sheet: dict,
    sheet_index: int,
    sheet_slug: str,
    context: dict,
    cell: dict,
) -> dict:
    coordinate = cell["coordinate"]
    merged_ranges = context["merged_ranges_by_cell"].get(coordinate, [])
    reasons = []

    if cell.get("is_formula"):
        reasons.append("formula")

    if merged_ranges:
        reasons.append("celula_em_regiao_mesclada")

    content = (
        f"Referencia estrutural da celula {coordinate} na aba {sheet.get('sheet_name')}: "
        f"valor {cell.get('value')}."
    )
    retrieval_text = (
        f"Celula {coordinate} da aba {sheet.get('sheet_name')}. "
        f"Motivos do bloco: {', '.join(reasons)}. Valor: {cell.get('value')}. "
        f"Tipo: {cell.get('data_type')}."
    )

    return _base_block(
        snapshot=snapshot,
        snapshot_path=snapshot_path,
        sheet=sheet,
        block_id=_block_id(sheet_index, sheet_slug, "cell_reference_block", coordinate.lower()),
        block_type="cell_reference_block",
        range_value=coordinate,
        content=content,
        retrieval_text=retrieval_text,
        cells=[cell],
        header_context={"associated_headers": _associated_headers_for_row(int(cell["row"]), [cell], context)},
        row_context={"row_number": int(cell["row"])},
        column_context={
            "column_number": int(cell["column"]),
            "column_letter": get_column_letter(int(cell["column"])),
        },
        structural_metadata={
            "reasons": reasons,
            "merged_ranges": merged_ranges,
            "is_formula": bool(cell.get("is_formula")),
            "number_format": cell.get("number_format"),
            "style_id": cell.get("style_id"),
        },
    )


def _base_block(
    snapshot: dict,
    snapshot_path: str | None,
    sheet: dict,
    block_id: str,
    block_type: str,
    range_value: str | None,
    content: str,
    retrieval_text: str,
    cells: list[dict],
    header_context: dict,
    row_context: dict,
    column_context: dict,
    structural_metadata: dict,
) -> dict:
    cell_references = [_cell_reference(cell) for cell in cells]
    cell_coordinates = [cell["coordinate"] for cell in cell_references]

    return {
        "block_id": block_id,
        "block_type": block_type,
        "sheet_name": sheet.get("sheet_name"),
        "range": range_value,
        "content": content,
        "retrieval_text": retrieval_text,
        "cells": cell_references,
        "header_context": header_context,
        "row_context": row_context,
        "column_context": column_context,
        "structural_metadata": structural_metadata,
        "traceability": {
            "source_file": snapshot.get("file_name"),
            "snapshot_file": str(snapshot_path) if snapshot_path else None,
            "sheet_name": sheet.get("sheet_name"),
            "range": range_value,
            "cell_coordinates": cell_coordinates,
            "source": "structural_snapshot",
        },
    }


def _detect_header_candidates(rows: dict[int, list[dict]]) -> list[dict]:
    if not rows:
        return []

    sorted_rows = sorted(rows)
    first_filled_row = sorted_rows[0]
    candidates = []

    for index, row_number in enumerate(sorted_rows):
        row_cells = rows[row_number]
        filled_count = len(row_cells)
        text_count = sum(1 for cell in row_cells if _is_textual_cell(cell))
        reasons = []

        if text_count > filled_count / 2:
            reasons.append("majority_textual")

        if row_number == first_filled_row:
            reasons.append("first_filled_row")

        next_row = sorted_rows[index + 1] if index + 1 < len(sorted_rows) else None
        if next_row == row_number + 1 and _row_looks_like_data(rows[next_row]):
            reasons.append("precedes_data_row")

        if reasons:
            candidates.append(
                {
                    "row": row_number,
                    "range": _range_for_cells(row_cells),
                    "reasons": reasons,
                    "textual_cells": text_count,
                    "filled_cells": filled_count,
                    "cells": row_cells,
                }
            )

    return candidates


def _build_headers_by_column(header_candidates: list[dict], columns: dict[int, list[dict]]) -> dict:
    headers_by_column = {}
    candidates_desc = sorted(header_candidates, key=_header_candidate_priority, reverse=True)

    for column_number in sorted(columns):
        selected_header = None

        for candidate in candidates_desc:
            for cell in candidate["cells"]:
                if int(cell["column"]) == column_number and _is_textual_cell(cell):
                    selected_header = {
                        "column": column_number,
                        "column_letter": get_column_letter(column_number),
                        "coordinate": cell["coordinate"],
                        "row": int(cell["row"]),
                        "value": cell.get("value"),
                        "source": "header_candidate",
                    }
                    break
            if selected_header:
                break

        if selected_header is None:
            for cell in columns[column_number]:
                if _is_textual_cell(cell):
                    selected_header = {
                        "column": column_number,
                        "column_letter": get_column_letter(column_number),
                        "coordinate": cell["coordinate"],
                        "row": int(cell["row"]),
                        "value": cell.get("value"),
                        "source": "first_textual_cell_in_column",
                    }
                    break

        if selected_header:
            headers_by_column[str(column_number)] = selected_header

    return headers_by_column


def _associated_headers_for_row(row_number: int, cells: list[dict], context: dict) -> dict:
    associated = {}
    previous_candidates = [
        candidate for candidate in context["header_candidates"] if candidate["row"] < row_number
    ]
    previous_candidates.sort(key=_header_candidate_priority, reverse=True)

    for cell in cells:
        column_number = int(cell["column"])
        selected_header = None

        for candidate in previous_candidates:
            for header_cell in candidate["cells"]:
                if int(header_cell["column"]) == column_number and _is_textual_cell(header_cell):
                    selected_header = {
                        "column": column_number,
                        "column_letter": get_column_letter(column_number),
                        "coordinate": header_cell["coordinate"],
                        "row": int(header_cell["row"]),
                        "value": header_cell.get("value"),
                        "source": "nearest_previous_header_candidate",
                    }
                    break
            if selected_header:
                break

        if selected_header is None:
            selected_header = context["headers_by_column"].get(str(column_number))

        if selected_header:
            associated[str(column_number)] = selected_header

    return associated


def _header_context_summary(context: dict) -> dict:
    return {
        "candidate_header_rows": [
            {
                "row": candidate["row"],
                "range": candidate["range"],
                "reasons": candidate["reasons"],
                "textual_cells": candidate["textual_cells"],
                "filled_cells": candidate["filled_cells"],
            }
            for candidate in context["header_candidates"]
        ],
        "headers_by_column": context["headers_by_column"],
    }


def _cell_reference(cell: dict) -> dict:
    column_number = int(cell["column"])
    return {
        "coordinate": cell.get("coordinate"),
        "row": int(cell.get("row")),
        "column": column_number,
        "column_letter": get_column_letter(column_number),
        "value": cell.get("value"),
        "data_type": cell.get("data_type"),
        "is_formula": bool(cell.get("is_formula")),
        "number_format": cell.get("number_format"),
        "style_id": cell.get("style_id"),
    }


def _range_for_cells(cells: list[dict]) -> str | None:
    if not cells:
        return None

    min_row = min(int(cell["row"]) for cell in cells)
    max_row = max(int(cell["row"]) for cell in cells)
    min_column = min(int(cell["column"]) for cell in cells)
    max_column = max(int(cell["column"]) for cell in cells)

    start = f"{get_column_letter(min_column)}{min_row}"
    end = f"{get_column_letter(max_column)}{max_row}"

    if start == end:
        return start

    return f"{start}:{end}"


def _format_cell_values(cells: list[dict]) -> str:
    if not cells:
        return "sem celulas preenchidas"

    return "; ".join(f"{cell['coordinate']}={cell.get('value')}" for cell in cells)


def _format_headers_by_column(headers: dict | None) -> str:
    if not headers:
        return "sem cabecalhos associados"

    formatted = []
    for column_key in sorted(headers, key=lambda key: int(key)):
        header = headers[column_key]
        formatted.append(
            f"{header.get('column_letter', get_column_letter(int(column_key)))}: "
            f"{header.get('value')} ({header.get('coordinate')})"
        )

    return "; ".join(formatted)


def _column_context_list(columns: dict) -> list[dict]:
    return [
        {
            "column_number": int(column_number),
            "column_letter": get_column_letter(int(column_number)),
            "cell_count": len(cells),
        }
        for column_number, cells in sorted(columns.items(), key=lambda item: int(item[0]))
    ]


def _predominant_type(cells: list[dict]) -> str | None:
    if not cells:
        return None

    counts = Counter(cell.get("data_type") for cell in cells)
    return sorted(counts.items(), key=lambda item: (-item[1], str(item[0])))[0][0]


def _is_textual_cell(cell: dict) -> bool:
    if cell.get("is_formula"):
        return False

    data_type = str(cell.get("data_type") or "").lower()
    if data_type in {"s", "str", "inlinestr"}:
        return True

    if data_type in {"n", "d", "b"}:
        return False

    value = str(cell.get("value") or "").strip()
    return bool(value) and not _looks_numeric(value)


def _row_looks_like_data(cells: list[dict]) -> bool:
    if not cells:
        return False

    text_count = sum(1 for cell in cells if _is_textual_cell(cell))
    formula_count = sum(1 for cell in cells if cell.get("is_formula"))
    non_text_count = len(cells) - text_count

    return non_text_count > 0 or formula_count > 0 or text_count <= len(cells) / 2


def _header_candidate_priority(candidate: dict) -> tuple[bool, int]:
    return ("majority_textual" in candidate.get("reasons", []), int(candidate["row"]))


def _looks_numeric(value: str) -> bool:
    try:
        float(str(value).replace(",", "."))
    except ValueError:
        return False

    return True


def _map_merged_ranges_by_cell(merged_ranges: list[str], cells: list[dict]) -> dict:
    if not merged_ranges:
        return {}

    mapped = defaultdict(list)

    for cell in cells:
        coordinate = cell["coordinate"]
        row_number, column_number = coordinate_to_tuple(coordinate)

        for merged_range in merged_ranges:
            min_column, min_row, max_column, max_row = range_boundaries(merged_range)
            if min_row <= row_number <= max_row and min_column <= column_number <= max_column:
                mapped[coordinate].append(merged_range)

    return dict(mapped)


def _block_id(sheet_index: int, sheet_slug: str, block_type: str, identifier: str) -> str:
    return f"sheet_{sheet_index:03d}_{sheet_slug}__{block_type}__{_slugify(identifier)}"


def _range_identifier(range_value: str | None) -> str:
    if not range_value:
        return "empty"

    return range_value.replace(":", "_to_")


def _slugify(value: str) -> str:
    normalized = unicodedata.normalize("NFKD", str(value))
    ascii_value = normalized.encode("ascii", "ignore").decode("ascii")
    slug = re.sub(r"[^a-zA-Z0-9]+", "_", ascii_value).strip("_").lower()
    return slug or "item"
