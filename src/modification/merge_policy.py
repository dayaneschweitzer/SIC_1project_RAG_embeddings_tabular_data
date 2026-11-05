"""Writes inside merged ranges resolve to the top-left anchor.

This policy never consults oracle/golden data.
"""

from __future__ import annotations

from openpyxl.utils import get_column_letter, range_boundaries


def coordinate_to_row_col(coordinate: str) -> tuple[int, int]:
    min_col, min_row, max_col, max_row = range_boundaries(str(coordinate).upper())
    if (min_col, min_row) != (max_col, max_row):
        raise ValueError(f"expected a single cell, got {coordinate}")
    return min_row, min_col


def row_col_to_coordinate(row: int, col: int) -> str:
    return f"{get_column_letter(col)}{row}"


def merge_anchor_coordinate(worksheet, coordinate: str) -> str:
    """Return the top-left anchor when coordinate sits inside a merged range."""
    row, col = coordinate_to_row_col(coordinate)
    for merged in worksheet.merged_cells.ranges:
        if int(merged.min_row) <= row <= int(merged.max_row) and int(merged.min_col) <= col <= int(
            merged.max_col
        ):
            return row_col_to_coordinate(int(merged.min_row), int(merged.min_col))
    return str(coordinate).upper()
