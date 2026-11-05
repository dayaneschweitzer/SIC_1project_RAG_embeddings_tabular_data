"""Deterministic shared tabular serialization for MiniLM and TABERTA.

This is not an official TABERTA HybridView reproduction. Both encoders receive
the same UTF-8 string. Query text is out of scope and remains natural language.
"""

from __future__ import annotations

import math
import unicodedata
from collections.abc import Callable
from typing import Any

SERIALIZATION_VERSION = "tabular_serialization_v1"
TEXT_VERSION_TAG = "v1"
EMPTY_TOKEN = "EMPTY"
TOKEN_BUDGET = 256
LINE_SEPARATOR = "\n"

BLOCK_TYPE_MAP = {
    "sheet_overview": "sheet_overview",
    "used_range": "used_range",
    "header_context": "header_context",
    "row_block": "row_block",
    "column_block": "column_block",
    "cell_reference_block": "cell_reference_block",
}

TokenCounter = Callable[[str], int]


class UnsupportedValueTypeError(ValueError):
    """Raised when a cell value has no canonical serialization."""


class StructuralBudgetError(ValueError):
    """Raised when mandatory structural fields exceed the dual token budget."""


def escape_field(value: str) -> str:
    r"""Escape backslash, pipe, and newlines. Do not escape ':' or '='."""
    return (
        value.replace("\\", "\\\\")
        .replace("|", "\\|")
        .replace("\r\n", "\n")
        .replace("\r", "\n")
        .replace("\n", "\\n")
    )


def normalize_scalar(value: Any) -> str:
    """Canonical scalar text for snapshot strings and native scalar values."""
    if value is None:
        return EMPTY_TOKEN
    if isinstance(value, bool):
        return "true" if value else "false"
    if isinstance(value, int):
        return format(value, "d")
    if isinstance(value, float):
        if math.isnan(value) or math.isinf(value):
            raise UnsupportedValueTypeError(f"non-finite float cannot be serialized: {value!r}")
        # CPython 3.x shortest round-trip for IEEE-754 binary64. Locale-independent
        # (always '.'), preserves -0.0, and does not insert thousands separators.
        return repr(value)
    if isinstance(value, str):
        if value == "":
            return EMPTY_TOKEN
        return value
    raise UnsupportedValueTypeError(
        f"no canonical serialization for type {type(value).__name__}: {value!r}"
    )


def nfc(value: str) -> str:
    return unicodedata.normalize("NFC", value)


def serialize_block(
    block: dict,
    *,
    count_minilm: TokenCounter,
    count_taberta: TokenCounter,
    token_budget: int = TOKEN_BUDGET,
) -> dict:
    """Serialize one semantic block into a shared dual-budget string."""
    mapped_type = _mapped_block_type(block.get("block_type"))
    assembler = _DualBudgetAssembler(
        count_minilm=count_minilm,
        count_taberta=count_taberta,
        token_budget=token_budget,
    )

    headers = _headers_for_block(block)
    structural_lines = [
        f"ver={TEXT_VERSION_TAG}",
        f"type={mapped_type}",
        f"sheet={escape_field(nfc(str(block.get('sheet_name') or '')))}",
        f"range={escape_field(nfc(str(block.get('range') or '')))}",
        f"headers={_join_fields(headers)}",
    ]
    for line in structural_lines:
        if not assembler.try_add(line):
            raise StructuralBudgetError(
                "mandatory structural fields exceed the dual token budget of "
                f"{token_budget} (MiniLM={count_minilm(assembler.preview(line))}, "
                f"TABERTA={count_taberta(assembler.preview(line))})"
            )

    body_segments = _body_segments(mapped_type, block)
    truncated = False
    for line, replace_prefix in body_segments:
        if assembler.try_add(line, replace_prefix=replace_prefix):
            continue
        truncated = True
        break

    text = assembler.text()
    minilm_tokens = count_minilm(text)
    taberta_tokens = count_taberta(text)
    if minilm_tokens > token_budget or taberta_tokens > token_budget:
        raise StructuralBudgetError("serialized text exceeds dual token budget after assembly")

    return {
        "serialization_text": text,
        "serialization_version": SERIALIZATION_VERSION,
        "source_block_id": str(block.get("block_id") or ""),
        "truncated": truncated,
        "taberta_token_count": taberta_tokens,
        "minilm_token_count": minilm_tokens,
    }


def _mapped_block_type(block_type: str | None) -> str:
    if block_type in BLOCK_TYPE_MAP:
        return BLOCK_TYPE_MAP[block_type]
    if block_type in BLOCK_TYPE_MAP.values():
        return str(block_type)
    raise ValueError(f"unsupported semantic block type: {block_type!r}")


def _join_fields(values: list[str]) -> str:
    return "|".join(escape_field(nfc(value)) for value in values)


def _headers_for_block(block: dict) -> list[str]:
    header_context = block.get("header_context") or {}
    by_column = header_context.get("headers_by_column")
    if not by_column and "associated_headers" in header_context:
        by_column = header_context.get("associated_headers")
    if not by_column:
        column_header = header_context.get("column_header")
        if column_header:
            by_column = {str(column_header.get("column") or 0): column_header}

    cells = _sorted_cells(block.get("cells") or [])
    if cells:
        columns = sorted({int(cell["column"]) for cell in cells})
    elif by_column:
        columns = sorted(int(key) for key in by_column)
    else:
        return []

    headers: list[str] = []
    for column in columns:
        header = None
        if by_column:
            header = by_column.get(str(column)) or by_column.get(column)
        if not header:
            headers.append(EMPTY_TOKEN)
            continue
        headers.append(normalize_scalar(header.get("value")))
    if all(item == EMPTY_TOKEN for item in headers):
        return []
    return headers


def _sorted_cells(cells: list[dict]) -> list[dict]:
    return sorted(
        cells,
        key=lambda cell: (int(cell.get("row") or 0), int(cell.get("column") or 0)),
    )


def _body_segments(mapped_type: str, block: dict) -> list[tuple[str, str | None]]:
    if mapped_type == "row_block":
        values = [normalize_scalar(cell.get("value")) for cell in _sorted_cells(block.get("cells") or [])]
        if not values:
            return []
        return [(f"row={_join_fields(values)}", None)]

    if mapped_type == "column_block":
        cells = _sorted_cells(block.get("cells") or [])
        segments: list[tuple[str, str | None]] = []
        values: list[str] = []
        for cell in cells:
            values.append(normalize_scalar(cell.get("value")))
            segments.append((f"col={_join_fields(values)}", "col="))
        return segments

    if mapped_type == "used_range":
        return _used_range_segments(block)

    if mapped_type == "cell_reference_block":
        cells = _sorted_cells(block.get("cells") or [])
        if not cells:
            return []
        cell = cells[0]
        coordinate = nfc(str(cell.get("coordinate") or block.get("range") or ""))
        value = normalize_scalar(cell.get("value"))
        return [
            (f"coord={escape_field(coordinate)}", None),
            (f"value={escape_field(nfc(value))}", None),
        ]

    if mapped_type == "sheet_overview":
        meta = block.get("structural_metadata") or {}
        merged = meta.get("merged_cells") or []
        tables = meta.get("tables") or []
        stats = [
            f"max_row:{_stat_int(meta.get('max_row'))}",
            f"max_column:{_stat_int(meta.get('max_column'))}",
            f"filled:{_stat_int(meta.get('filled_cells'))}",
            f"formulas:{_stat_int(meta.get('formulas'))}",
            f"merged:{len(merged) if isinstance(merged, list) else _stat_int(merged)}",
            f"tables:{len(tables) if isinstance(tables, list) else _stat_int(tables)}",
        ]
        return [(f"stats={_join_fields(stats)}", None)]

    if mapped_type == "header_context":
        return []

    raise ValueError(f"unsupported mapped block type: {mapped_type!r}")


def _used_range_segments(block: dict) -> list[tuple[str, str | None]]:
    rows: dict[int, list[str]] = {}
    for cell in _sorted_cells(block.get("cells") or []):
        rows.setdefault(int(cell["row"]), []).append(normalize_scalar(cell.get("value")))
    return [(f"r{row_number}={_join_fields(values)}", None) for row_number, values in rows.items()]


def _stat_int(value: Any) -> str:
    if value is None:
        return "0"
    if isinstance(value, bool):
        raise UnsupportedValueTypeError("boolean is not a valid sheet statistic")
    if isinstance(value, int):
        return format(value, "d")
    if isinstance(value, float) and value.is_integer():
        return format(int(value), "d")
    raise UnsupportedValueTypeError(f"non-integer statistic: {value!r}")


class _DualBudgetAssembler:
    def __init__(
        self,
        *,
        count_minilm: TokenCounter,
        count_taberta: TokenCounter,
        token_budget: int,
    ) -> None:
        self._count_minilm = count_minilm
        self._count_taberta = count_taberta
        self._token_budget = token_budget
        self._lines: list[str] = []

    def preview(self, line: str, replace_prefix: str | None = None) -> str:
        return LINE_SEPARATOR.join(self._candidate_lines(line, replace_prefix))

    def try_add(self, line: str, replace_prefix: str | None = None) -> bool:
        candidate_lines = self._candidate_lines(line, replace_prefix)
        candidate = LINE_SEPARATOR.join(candidate_lines)
        if self._count_minilm(candidate) > self._token_budget:
            return False
        if self._count_taberta(candidate) > self._token_budget:
            return False
        self._lines = candidate_lines
        return True

    def _candidate_lines(self, line: str, replace_prefix: str | None) -> list[str]:
        if not replace_prefix:
            return [*self._lines, line]
        replaced = False
        lines: list[str] = []
        for existing in self._lines:
            if not replaced and existing.startswith(replace_prefix):
                lines.append(line)
                replaced = True
            else:
                lines.append(existing)
        if not replaced:
            lines.append(line)
        return lines

    def text(self) -> str:
        return LINE_SEPARATOR.join(self._lines)
