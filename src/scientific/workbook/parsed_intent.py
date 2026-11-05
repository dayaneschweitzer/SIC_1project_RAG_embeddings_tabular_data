"""Canonical modification intent. Describes operations, not benchmark family IDs."""

from __future__ import annotations

from dataclasses import asdict, dataclass, field
from typing import Any

UPDATE_CELL = "update_cell"
REPLACE_VALUES = "replace_values"
WRITE_VALUE = "write_value"
WRITE_FORMULA = "write_formula"
DELETE_ROW = "delete_row"
INSERT_ROW = "insert_row"
INSERT_COLUMN = "insert_column"
SORT_RANGE = "sort_range"
UNMERGE_RANGE = "unmerge_range"
COPY_SHEET = "copy_sheet"
UNSUPPORTED = "unsupported"

PARSE_PARSED = "parsed"
PARSE_UNSUPPORTED = "unsupported"
PARSE_AMBIGUOUS = "ambiguous"

PLAN_PLANNED = "planned"
PLAN_FAILED = "failed"

STRUCTURED_OPERATIONS = frozenset(
    {
        REPLACE_VALUES,
        WRITE_VALUE,
        WRITE_FORMULA,
        INSERT_ROW,
        DELETE_ROW,
        INSERT_COLUMN,
        SORT_RANGE,
        UNMERGE_RANGE,
        COPY_SHEET,
    }
)

PARSER_NAME = "deterministic_regex"


@dataclass
class ParsedModificationIntent:
    request: str
    normalized_request: str
    operation: str
    parse_status: str = PARSE_UNSUPPORTED
    target_sheet: str | None = None
    destination: str | None = None
    source_cells: list[str] = field(default_factory=list)
    source_range: str | None = None
    literal_value: str | None = None
    formula: str | None = None
    expression: str | None = None
    replacement_old: str | None = None
    replacement_new: str | None = None
    sort_range: str | None = None
    sort_key: str | None = None
    sort_order: str | None = None
    insertion_position: int | str | None = None
    insertion_axis: str | None = None
    new_sheet_name: str | None = None
    extract_length: int | None = None
    separator: str | None = None
    confidence: str = "low"
    warnings: list[str] = field(default_factory=list)
    parser: str = PARSER_NAME
    target_coordinate: str | None = None
    target_value: str | None = None
    new_value: str | None = None

    def to_dict(self) -> dict[str, Any]:
        payload = asdict(self)
        payload["source_cells"] = list(self.source_cells)
        payload["warnings"] = list(self.warnings)
        return payload
