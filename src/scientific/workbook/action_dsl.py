from __future__ import annotations

from collections.abc import Mapping
from dataclasses import dataclass, field
from math import isfinite
import re
from types import MappingProxyType
from typing import Literal, get_args


ACTION_DSL_VERSION = "action_dsl_v1"

ActionOperation = Literal[
    "update_cell",
    "replace_values",
    "write_value",
    "write_formula",
    "insert_row",
    "delete_row",
    "insert_column",
    "sort_range",
    "unmerge_range",
    "copy_sheet",
]
ActionArgument = str | int | float | bool | None
SUPPORTED_OPERATIONS = frozenset(get_args(ActionOperation))
WRITE_VALUE_EXPRESSIONS = frozenset(
    {"concatenate", "extract_token", "extract_prefix", "lookup", "add", "layout_caption"}
)
_FORMULA_EXPRESSIONS = frozenset({"sum", "average", "divide", "subtract", "add"})
_ARGUMENT_KEYS = {
    "update_cell": frozenset({"new_value"}),
    "replace_values": frozenset({"replacement_old", "replacement_new"}),
    "write_value": frozenset({"expression", "literal_value", "separator", "extract_length"}),
    "write_formula": frozenset({"expression", "formula", "fill_mode", "array_mode"}),
    "insert_row": frozenset({"position"}),
    "delete_row": frozenset({"position"}),
    "insert_column": frozenset({"position"}),
    "sort_range": frozenset({"sort_key", "sort_order"}),
    "unmerge_range": frozenset(),
    "copy_sheet": frozenset(),
}
_CELL_PATTERN = re.compile(r"\$?([A-Za-z]{1,3})\$?([1-9][0-9]*)")


class ActionDSLValidationError(ValueError):
    pass


def _column_index(column: str) -> int:
    index = 0
    for letter in column.upper():
        index = index * 26 + ord(letter) - ord("A") + 1
    return index


def _cell_bounds(cell: str) -> tuple[int, int]:
    match = _CELL_PATTERN.fullmatch(cell) if isinstance(cell, str) else None
    if match is None:
        raise ActionDSLValidationError(f"Invalid A1 cell: {cell!r}")
    column, row = _column_index(match[1]), int(match[2])
    if column > 16384 or row > 1048576:
        raise ActionDSLValidationError(f"A1 cell outside worksheet limits: {cell!r}")
    return column, row


def _range_bounds(cell_range: str) -> tuple[int, int, int, int]:
    if not isinstance(cell_range, str):
        raise ActionDSLValidationError("range must be an A1 string")
    endpoints = cell_range.split(":")
    if len(endpoints) not in (1, 2):
        raise ActionDSLValidationError(f"Invalid A1 range: {cell_range!r}")
    min_col, min_row = _cell_bounds(endpoints[0])
    max_col, max_row = _cell_bounds(endpoints[-1])
    if min_col > max_col or min_row > max_row:
        raise ActionDSLValidationError(f"Reversed A1 range: {cell_range!r}")
    return min_col, min_row, max_col, max_row


def _validate_location(cells: tuple[str, ...], cell_range: str | None) -> tuple[str, ...]:
    if not isinstance(cells, (list, tuple)):
        raise ActionDSLValidationError("cells must be an ordered list or tuple")
    bounds = _range_bounds(cell_range) if cell_range is not None else None
    for cell in cells:
        column, row = _cell_bounds(cell)
        if bounds and not (bounds[0] <= column <= bounds[2] and bounds[1] <= row <= bounds[3]):
            raise ActionDSLValidationError(f"Cell {cell!r} is outside range {cell_range!r}")
    return tuple(cells)


def _require_text(value: object, name: str) -> None:
    if not isinstance(value, str) or not value.strip():
        raise ActionDSLValidationError(f"{name} must be a nonempty string")


def _validate_formula(formula: object) -> None:
    if not isinstance(formula, str) or not formula.startswith("=") or not formula[1:].strip():
        raise ActionDSLValidationError("formula must start with '=' and contain an expression")


@dataclass(frozen=True)
class ActionSource:
    cells: tuple[str, ...] = ()
    range: str | None = None
    ranges: tuple[str, ...] = ()
    ranges_sheet: str | None = None

    def __post_init__(self) -> None:
        object.__setattr__(self, "cells", _validate_location(self.cells, self.range))
        if not isinstance(self.ranges, (tuple, list)):
            raise ActionDSLValidationError("Source ranges must be ordered")
        for value in self.ranges:
            _range_bounds(value)
        object.__setattr__(self, "ranges", tuple(self.ranges))
        if self.ranges_sheet is not None:
            _require_text(self.ranges_sheet, "ranges_sheet")
            if not self.ranges:
                raise ActionDSLValidationError("ranges_sheet requires explicit source ranges")

    def to_dict(self) -> dict:
        value = {"cells": list(self.cells), "range": self.range}
        if self.ranges:
            value["ranges"] = list(self.ranges)
        if self.ranges_sheet is not None:
            value["ranges_sheet"] = self.ranges_sheet
        return value


@dataclass(frozen=True)
class ActionTarget:
    cells: tuple[str, ...] = ()
    range: str | None = None
    new_sheet: str | None = None

    def __post_init__(self) -> None:
        object.__setattr__(self, "cells", _validate_location(self.cells, self.range))
        if self.new_sheet is not None:
            _require_text(self.new_sheet, "new_sheet")

    def to_dict(self) -> dict:
        return {"cells": list(self.cells), "range": self.range, "new_sheet": self.new_sheet}


@dataclass(frozen=True)
class ActionDSL:
    operation: ActionOperation | None = None
    sheet: str | None = None
    source: ActionSource = field(default_factory=ActionSource)
    target: ActionTarget = field(default_factory=ActionTarget)
    arguments: Mapping[str, ActionArgument] = field(default_factory=dict)
    schema_version: str = ACTION_DSL_VERSION

    def __post_init__(self) -> None:
        if not isinstance(self.arguments, Mapping):
            raise ActionDSLValidationError("arguments must be a mapping")
        for key, value in self.arguments.items():
            if not isinstance(key, str):
                raise ActionDSLValidationError("argument keys must be strings")
            if value is not None and not isinstance(value, (str, int, float, bool)):
                raise ActionDSLValidationError(f"Argument {key!r} must be a scalar value")
            if isinstance(value, float) and not isfinite(value):
                raise ActionDSLValidationError(f"Argument {key!r} must be finite")
        object.__setattr__(self, "arguments", MappingProxyType(dict(self.arguments)))
        self.validate()

    def validate(self) -> None:
        if self.schema_version != ACTION_DSL_VERSION:
            raise ActionDSLValidationError(f"Unknown schema_version: {self.schema_version!r}")
        if not isinstance(self.operation, str) or self.operation not in SUPPORTED_OPERATIONS:
            raise ActionDSLValidationError(f"Unknown operation: {self.operation!r}")
        _require_text(self.sheet, "sheet")
        if not isinstance(self.source, ActionSource):
            raise ActionDSLValidationError("source must be an ActionSource")
        if not isinstance(self.target, ActionTarget):
            raise ActionDSLValidationError("target must be an ActionTarget")
        unknown = set(self.arguments) - _ARGUMENT_KEYS[self.operation]
        if unknown:
            raise ActionDSLValidationError(f"Unsupported arguments: {', '.join(sorted(unknown))}")
        if self.target.new_sheet is not None and self.operation != "copy_sheet":
            raise ActionDSLValidationError("new_sheet is only valid for copy_sheet")

        if self.source.ranges_sheet is not None and self.operation != "write_formula":
            raise ActionDSLValidationError("Qualified source ranges currently require write_formula")
        if self.operation in {"update_cell", "write_value", "write_formula"}:
            self._validate_write_target()
        if self.operation == "update_cell":
            self._require_value("new_value")
        elif self.operation == "write_value":
            self._validate_write_value()
        elif self.operation == "write_formula":
            if self.arguments.get("array_mode") is not None and (self.arguments["array_mode"] != "preserve_single_cell" or self.arguments.get("fill_mode") != "relative_rows"):
                raise ActionDSLValidationError("Unknown array formula preservation mode")
            _validate_formula(self.arguments.get("formula"))
            expression = self.arguments.get("expression")
            if expression is not None:
                if expression not in _FORMULA_EXPRESSIONS:
                    raise ActionDSLValidationError("Unsupported formula expression")
                if expression in {"add", "divide", "subtract"}:
                    self._require_source_cells(2)
                elif not (self.source.cells or self.source.range):
                    raise ActionDSLValidationError("Formula expression requires source cells or range")
        elif self.operation in {"replace_values", "sort_range", "unmerge_range"}:
            self._validate_in_place_range()
            if self.operation == "replace_values":
                self._require_value("replacement_old")
                self._require_value("replacement_new")
            elif self.operation == "sort_range":
                if self.arguments.get("sort_key") != "first_column":
                    raise ActionDSLValidationError("sort_key must be 'first_column'")
                if self.arguments.get("sort_order") != "ascending":
                    raise ActionDSLValidationError("sort_order must be 'ascending'")
        elif self.operation in {"insert_row", "insert_column", "delete_row"}:
            self._validate_position()
            if self.operation == "delete_row":
                self._require_source_cells(1)
                if any(_cell_bounds(c)[1] != int(self.arguments["position"]) for c in self.source.cells):
                    raise ActionDSLValidationError("Deletion evidence must belong to the deleted row")
        elif self.operation == "copy_sheet":
            _require_text(self.target.new_sheet, "new_sheet")
            if self.target.new_sheet == self.sheet:
                raise ActionDSLValidationError("new_sheet must differ from sheet")

    def _validate_write_target(self) -> None:
        if not (self.target.cells or self.target.range):
            raise ActionDSLValidationError("Writing requires target cells or range")
        if self.operation == "write_formula" and self.arguments.get("fill_mode") is not None:
            if self.arguments["fill_mode"] != "relative_rows" or not self.target.range or self.target.cells:
                raise ActionDSLValidationError("Relative formula fill requires one vertical target range")
            left, top, right, bottom = _range_bounds(self.target.range)
            if left != right or bottom - top + 1 > 10000:
                raise ActionDSLValidationError("Formula fill must be vertical and bounded to 10000 cells")
            if any(token in str(self.arguments.get("formula") or "") for token in ("!", "[")):
                raise ActionDSLValidationError("Cross-sheet/external formula fill is not qualified")
            return
        if self.operation in {"write_value", "write_formula"}:
            if len(self.target.cells) > 1:
                raise ActionDSLValidationError("Writing requires a single target cell")
            if self.target.range:
                min_col, min_row, max_col, max_row = _range_bounds(self.target.range)
                if (min_col, min_row) != (max_col, max_row):
                    raise ActionDSLValidationError("Writing requires a single-cell target range")

    def _require_value(self, key: str) -> None:
        if self.arguments.get(key) in (None, ""):
            raise ActionDSLValidationError(f"{key} is required")

    def _require_source_cells(self, minimum: int) -> None:
        if len(self.source.cells) < minimum:
            raise ActionDSLValidationError(f"Expression requires at least {minimum} source cells")

    def _validate_write_value(self) -> None:
        expression = self.arguments.get("expression")
        if expression is None or expression == "layout_caption":
            self._require_value("literal_value")
        elif expression not in WRITE_VALUE_EXPRESSIONS:
            raise ActionDSLValidationError(f"Unsupported write_value expression: {expression!r}")
        elif expression in {"concatenate", "add"}:
            self._require_source_cells(2)
        else:
            self._require_source_cells(1)
        if expression == "concatenate" and "separator" in self.arguments:
            if not isinstance(self.arguments["separator"], str):
                raise ActionDSLValidationError("separator must be a string")
        if expression == "extract_prefix":
            length = self.arguments.get("extract_length")
            if type(length) is not int or length < 1:
                raise ActionDSLValidationError("extract_length must be a positive integer")

    def _validate_in_place_range(self) -> None:
        if not self.source.range:
            raise ActionDSLValidationError(f"{self.operation} requires source.range")
        if not self.target.range:
            raise ActionDSLValidationError(f"{self.operation} requires target.range")
        if _range_bounds(self.source.range) != _range_bounds(self.target.range):
            raise ActionDSLValidationError("Source and target ranges must match for in-place operations")

    def _validate_position(self) -> None:
        position = self.arguments.get("position")
        if type(position) is int:
            index = position
        elif isinstance(position, str) and re.fullmatch(r"[1-9][0-9]*", position):
            index = int(position)
        elif (
            self.operation == "insert_column"
            and isinstance(position, str)
            and re.fullmatch(r"[A-Za-z]{1,3}", position)
        ):
            index = _column_index(position)
        else:
            raise ActionDSLValidationError("position must be a positive index or column letter")
        maximum = 1048576 if self.operation in {"insert_row", "delete_row"} else 16384
        if not 1 <= index <= maximum:
            raise ActionDSLValidationError("position is outside worksheet limits")

    def to_dict(self) -> dict:
        return {
            "schema_version": self.schema_version,
            "operation": self.operation,
            "sheet": self.sheet,
            "source": self.source.to_dict(),
            "target": self.target.to_dict(),
            "arguments": {key: self.arguments[key] for key in sorted(self.arguments)},
        }
