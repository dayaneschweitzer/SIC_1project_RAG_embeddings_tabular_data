from __future__ import annotations

from collections.abc import Mapping
from dataclasses import dataclass, field
from types import MappingProxyType
from typing import Literal

from openpyxl.utils import column_index_from_string, range_boundaries

from .action_dsl import (
    SUPPORTED_OPERATIONS,
    ActionDSL,
    ActionDSLValidationError,
    ActionSource,
    ActionTarget,
)
from .parsed_intent import PARSE_PARSED


ACTION_GROUNDING_VERSION = "action_grounding_v1"
GroundingStatus = Literal["grounded", "insufficient_context", "invalid_intent"]
Bounds = tuple[int, int, int, int]


@dataclass(frozen=True)
class FieldProvenance:
    source: Literal["instruction", "instruction+evidence"] = "instruction"
    evidence_block_ids: tuple[str, ...] = ()

    def to_dict(self) -> dict:
        result = {"source": self.source}
        if self.source == "instruction+evidence":
            result["evidence_block_ids"] = list(self.evidence_block_ids)
        return result


@dataclass(frozen=True)
class GroundingResult:
    status: GroundingStatus
    action: ActionDSL | None = None
    evidence_block_ids: tuple[str, ...] = ()
    requirements: tuple[str, ...] = ()
    satisfied_requirements: tuple[str, ...] = ()
    missing_requirements: tuple[str, ...] = ()
    field_provenance: Mapping[str, FieldProvenance] = field(default_factory=dict)
    schema_version: str = field(default=ACTION_GROUNDING_VERSION, init=False)

    def __post_init__(self) -> None:
        if self.status not in {"grounded", "insufficient_context", "invalid_intent"}:
            raise ValueError("Invalid grounding status")
        if (self.status == "grounded") != isinstance(self.action, ActionDSL):
            raise ValueError("Only grounded results may contain an ActionDSL")
        if self.status != "grounded" and self.action is not None:
            raise ValueError("Ungrounded results must have action=None")
        for name in ("evidence_block_ids", "requirements", "satisfied_requirements", "missing_requirements"):
            object.__setattr__(self, name, tuple(getattr(self, name)))
        object.__setattr__(self, "field_provenance", MappingProxyType(dict(sorted(self.field_provenance.items()))))

    def to_dict(self) -> dict:
        return {
            "schema_version": self.schema_version,
            "status": self.status,
            "action": self.action.to_dict() if self.action is not None else None,
            "evidence_block_ids": list(self.evidence_block_ids),
            "requirements": list(self.requirements),
            "satisfied_requirements": list(self.satisfied_requirements),
            "missing_requirements": list(self.missing_requirements),
            "field_provenance": {key: value.to_dict() for key, value in self.field_provenance.items()},
        }


@dataclass(frozen=True)
class _Requirement:
    name: str
    kind: Literal["sheet", "reference", "replacement", "insertion", "merge"]
    fields: tuple[str, ...]
    reference: str | None = None
    value: object = None
    axis: Literal["row", "column"] | None = None
    sheet: str | None = None
    allow_column_blanks: bool = False
    allow_new_target: bool = False


@dataclass(frozen=True)
class _Evidence:
    block_id: str
    regions: tuple[Bounds, ...]
    cell_values: tuple[tuple[Bounds, object], ...]
    merged_ranges: tuple[Bounds, ...]
    sheet_structure: bool
    complete_columns: tuple[int, ...] = ()
    complete_sheet: bool = False


def ground_action(intent: dict, evidences: list[dict]) -> GroundingResult:
    if not isinstance(intent, Mapping) or intent.get("parse_status") != PARSE_PARSED:
        return GroundingResult(status="invalid_intent")
    operation = intent.get("operation")
    if not isinstance(operation, str) or operation not in SUPPORTED_OPERATIONS:
        return GroundingResult(status="invalid_intent")
    try:
        candidate = _action_from_intent(intent)
    except ActionDSLValidationError:
        return GroundingResult(status="invalid_intent")

    requirements = _action_requirements(candidate)
    provenance = _instruction_provenance(candidate)
    satisfied, evidence_ids = set(), set()
    used_by_sheet, blocks_by_sheet = {}, {}
    for requirement in (*requirements[1:], requirements[0]):
        sheet = requirement.sheet if requirement.sheet is not None else candidate.sheet
        if sheet not in blocks_by_sheet:
            blocks_by_sheet[sheet] = _evidence_for_sheet(evidences, sheet)
        blocks = blocks_by_sheet[sheet]
        used = used_by_sheet.setdefault(sheet, set())
        proof = _prove(requirement, blocks, used)
        if proof is None:
            continue
        satisfied.add(requirement.name)
        used.update(proof)
        ids = {blocks[index].block_id for index in proof if blocks[index].block_id}
        evidence_ids.update(ids)
        for name in requirement.fields:
            previous = provenance[name].evidence_block_ids
            provenance[name] = FieldProvenance("instruction+evidence", tuple(sorted(ids | set(previous))))

    names = tuple(requirement.name for requirement in requirements)
    missing = tuple(name for name in names if name not in satisfied)
    return GroundingResult(
        status="insufficient_context" if missing else "grounded",
        action=None if missing else candidate,
        evidence_block_ids=tuple(sorted(evidence_ids)),
        requirements=names,
        satisfied_requirements=tuple(name for name in names if name in satisfied),
        missing_requirements=missing,
        field_provenance=provenance,
    )


def _canonical(reference: str | None) -> str | None:
    return reference.replace("$", "").upper() if reference is not None else None


def _reference_alias(intent: Mapping, *keys: str) -> str | None:
    values = [intent[key] for key in keys if intent.get(key) is not None]
    bounds = [_validated_bounds(value) for value in values]
    if bounds and any(item != bounds[0] for item in bounds[1:]):
        raise ActionDSLValidationError("Conflicting declared references")
    return _canonical(values[0]) if values else None


def _literal_value(intent: Mapping):
    literal = intent.get("literal_value")
    new_value = intent.get("new_value")
    if literal is not None and new_value is not None:
        if type(literal) is not type(new_value) or literal != new_value:
            raise ActionDSLValidationError("Conflicting declared literal values")
    return literal if literal is not None else new_value


def _action_from_intent(intent: Mapping) -> ActionDSL:
    operation = intent["operation"]
    source = ActionSource()
    target = ActionTarget()
    arguments = {}
    if operation in {"write_value", "write_formula"}:
        cells = intent.get("source_cells")
        declared = ActionSource(cells=() if cells is None else cells, range=intent.get("source_range"))
        source = ActionSource(cells=tuple(_canonical(cell) for cell in declared.cells), range=_canonical(declared.range),
                              ranges=tuple(_canonical(value) for value in intent.get("source_ranges", [])),
                              ranges_sheet=intent.get("source_ranges_sheet"))
    if operation in {"update_cell", "write_value", "write_formula"}:
        destination = _reference_alias(intent, "destination", "target_coordinate")
        target = (ActionTarget(range=intent["target_range"]) if intent.get("fill_mode") == "relative_rows"
                  else ActionTarget(cells=(destination,) if destination is not None else ()))
    if operation == "update_cell":
        arguments = {"new_value": intent.get("new_value")}
    elif operation == "write_value":
        arguments = {key: intent[key] for key in ("expression", "separator", "extract_length") if intent.get(key) is not None}
        literal = _literal_value(intent)
        if literal is not None:
            arguments["literal_value"] = literal
    elif operation == "write_formula":
        arguments = {key: intent[key] for key in ("expression", "formula", "fill_mode", "array_mode") if intent.get(key) is not None}
    elif operation in {"replace_values", "sort_range", "unmerge_range"}:
        keys = ("source_range", "sort_range") if operation == "sort_range" else ("source_range",)
        cell_range = _reference_alias(intent, *keys)
        source = ActionSource(range=cell_range)
        target = ActionTarget(range=cell_range)
        if operation == "replace_values":
            arguments = {key: intent.get(key) for key in ("replacement_old", "replacement_new")}
        elif operation == "sort_range":
            arguments = {key: intent.get(key) for key in ("sort_key", "sort_order")}
    elif operation == "delete_row":
        source = ActionSource(cells=tuple(intent.get("source_cells") or []))
        arguments = {"position": intent.get("deletion_position")}
    elif operation in {"insert_row", "insert_column"}:
        axis = "row" if operation == "insert_row" else "column"
        if intent.get("insertion_axis") not in (None, axis):
            raise ActionDSLValidationError("Conflicting insertion axis")
        arguments = {"position": intent.get("insertion_position")}
    elif operation == "copy_sheet":
        target = ActionTarget(new_sheet=intent.get("new_sheet_name"))
    return ActionDSL(operation=operation, sheet=intent.get("target_sheet"), source=source, target=target, arguments=arguments)


def _instruction_provenance(action: ActionDSL) -> dict[str, FieldProvenance]:
    names = ["operation", "sheet"]
    names.extend(f"source.cells.{cell}" for cell in action.source.cells)
    names.extend(f"source.ranges.{index}" for index in range(len(action.source.ranges)))
    if action.source.ranges_sheet is not None:
        names.append("source.ranges_sheet")
    names.extend(f"target.cells.{cell}" for cell in action.target.cells)
    if action.source.range is not None:
        names.append("source.range")
    if action.target.range is not None:
        names.append("target.range")
    if action.target.new_sheet is not None:
        names.append("target.new_sheet")
    names.extend(f"arguments.{key}" for key in sorted(action.arguments))
    return {name: FieldProvenance() for name in names}


def _action_requirements(action: ActionDSL) -> tuple[_Requirement, ...]:
    result = [_Requirement("sheet", "sheet", ("sheet",))]
    for index, reference in enumerate(action.source.ranges):
        fields = (f"source.ranges.{index}",) + (("source.ranges_sheet", "arguments.formula") if action.source.ranges_sheet is not None else ())
        result.append(_Requirement(f"source.ranges.{index}", "reference", fields,
                                   reference=reference, sheet=action.source.ranges_sheet,
                                   allow_column_blanks=action.source.ranges_sheet is not None))
    operation = action.operation
    if action.source.ranges_sheet is not None:
        for cell in action.target.cells:
            result.append(_Requirement(f"target.cells.{cell}", "reference", (f"target.cells.{cell}",), reference=cell, allow_new_target=True))
    if operation == "update_cell":
        cell = action.target.cells[0]
        result.append(_Requirement(f"target.cells.{cell}", "reference", (f"target.cells.{cell}",), reference=cell))
    elif operation in {"write_value", "write_formula"}:
        expression = action.arguments.get("expression")
        if operation == "write_formula" or expression != "layout_caption":
            if action.source.range and (operation == "write_formula" or expression in {None, "lookup"}):
                result.append(_Requirement("source.range", "reference", ("source.range",), reference=action.source.range))
            for cell in dict.fromkeys(action.source.cells):
                name = f"source.cells.{cell}"
                result.append(_Requirement(name, "reference", (name,), reference=cell))
    elif operation in {"replace_values", "sort_range", "unmerge_range"}:
        result.append(_Requirement("source.range", "reference", ("source.range", "target.range"), reference=action.source.range))
        if operation == "replace_values":
            result.append(_Requirement("arguments.replacement_old", "replacement", ("arguments.replacement_old",),
                                       reference=action.source.range, value=action.arguments["replacement_old"]))
        elif operation == "unmerge_range":
            result.append(_Requirement("source.range.merged", "merge", ("source.range", "target.range"), reference=action.source.range))
    elif operation == "delete_row":
        result.extend(_Requirement("deletion_source." + str(i), "reference", (f"source.cells.{cell}", "arguments.position"), reference=cell)
                      for i, cell in enumerate(action.source.cells))
    elif operation in {"insert_row", "insert_column"}:
        result.append(_Requirement("arguments.position", "insertion", ("arguments.position",),
                                   value=action.arguments["position"], axis="row" if operation == "insert_row" else "column"))
    return tuple(result)


def _validated_bounds(reference: str) -> Bounds:
    ActionSource(range=reference)
    return range_boundaries(_canonical(reference))


def _evidence_bounds(reference: object, *, cell_only: bool = False) -> Bounds | None:
    if not isinstance(reference, str):
        return None
    try:
        bounds = _validated_bounds(reference)
    except ActionDSLValidationError:
        return None
    if cell_only and (bounds[0], bounds[1]) != (bounds[2], bounds[3]):
        return None
    return bounds


def _evidence_for_sheet(evidences: list[dict], sheet: str) -> tuple[_Evidence, ...]:
    result = []
    for evidence in evidences or []:
        if not isinstance(evidence, Mapping) or evidence.get("sheet_name") != sheet:
            continue
        regions = set()
        bounds = _evidence_bounds(evidence.get("range"))
        if bounds is not None:
            regions.add(bounds)
        values = []
        cells = evidence.get("cells")
        for cell in cells if isinstance(cells, (list, tuple)) else ():
            coordinate = cell.get("coordinate") if isinstance(cell, Mapping) else cell
            bounds = _evidence_bounds(coordinate, cell_only=True)
            if bounds is None:
                continue
            regions.add(bounds)
            if isinstance(cell, Mapping) and "value" in cell:
                values.append((bounds, cell["value"]))
        metadata = evidence.get("structural_metadata")
        metadata = metadata if isinstance(metadata, Mapping) else {}
        merges = set()
        for name in ("merged_ranges", "merged_cells"):
            references = metadata.get(name)
            for reference in references if isinstance(references, (list, tuple)) else ():
                bounds = _evidence_bounds(reference)
                if bounds is not None:
                    merges.add(bounds)
        dimensions = (metadata.get("max_row"), metadata.get("max_column"))
        structure = evidence.get("block_type") == "sheet_overview" or all(type(value) is int and value >= 1 for value in dimensions)
        block_id = evidence.get("block_id")
        extent = _evidence_bounds(evidence.get("range"))
        complete_columns = ((extent[0],) if extent and extent[0] == extent[2]
                            and evidence.get("block_type") == "column_block"
                            and metadata.get("source_block_complete") is True else ())
        result.append(_Evidence(
            block_id=block_id if isinstance(block_id, str) else "",
            regions=tuple(sorted(regions)), cell_values=tuple(values),
            merged_ranges=tuple(sorted(merges)), sheet_structure=structure, complete_columns=complete_columns,
            complete_sheet=bool(extent and metadata.get('source_block_complete') is True
                                and evidence.get('block_type') in {'used_range', 'sheet_overview'}
                                and all(type(value) is int and value >= 1 for value in dimensions)
                                and extent == (1, 1, dimensions[1], dimensions[0])
                                and all(_contains(extent, bounds) for bounds in regions | merges)),
        ))
    return tuple(sorted(result, key=lambda item: (item.block_id, item.regions, item.merged_ranges, item.sheet_structure)))


def _prove(requirement: _Requirement, blocks: tuple[_Evidence, ...], used: set[int]) -> tuple[int, ...] | None:
    order = sorted(range(len(blocks)), key=lambda index: (index not in used, index))
    if requirement.kind == "reference":
        remaining = [_validated_bounds(requirement.reference)]
        proof = []
        for index in order:
            if requirement.allow_new_target and blocks[index].complete_sheet:
                return tuple(dict.fromkeys(proof + [index]))
            previous = remaining
            regions = blocks[index].regions
            if requirement.allow_column_blanks:
                regions += tuple((column, 1, column, 1048576) for column in blocks[index].complete_columns)
            for region in regions:
                remaining = [piece for rectangle in remaining for piece in _subtract(rectangle, region)]
            if remaining != previous:
                proof.append(index)
            if not remaining:
                return tuple(proof)
        return None
    for index in order:
        block = blocks[index]
        if requirement.kind == "sheet":
            return (index,)
        if requirement.kind == "replacement":
            region = _validated_bounds(requirement.reference)
            if any(value is not None and str(value) == str(requirement.value) and _contains(region, cell)
                   for cell, value in block.cell_values):
                return (index,)
        elif requirement.kind == "merge":
            if _validated_bounds(requirement.reference) in block.merged_ranges:
                return (index,)
        elif requirement.kind == "insertion":
            position = requirement.value
            index_value = int(position) if str(position).isdigit() else column_index_from_string(str(position).upper())
            lower, upper = (1, 3) if requirement.axis == "row" else (0, 2)
            if block.sheet_structure or any(region[lower] <= index_value <= region[upper] for region in block.regions):
                return (index,)
    return None


def _contains(outer: Bounds, inner: Bounds) -> bool:
    return outer[0] <= inner[0] <= inner[2] <= outer[2] and outer[1] <= inner[1] <= inner[3] <= outer[3]


def _subtract(rectangle: Bounds, cover: Bounds) -> tuple[Bounds, ...]:
    left, top, right, bottom = rectangle
    x0, y0 = max(left, cover[0]), max(top, cover[1])
    x1, y1 = min(right, cover[2]), min(bottom, cover[3])
    if x0 > x1 or y0 > y1:
        return (rectangle,)
    pieces = []
    if top < y0:
        pieces.append((left, top, right, y0 - 1))
    if y1 < bottom:
        pieces.append((left, y1 + 1, right, bottom))
    if left < x0:
        pieces.append((left, y0, x0 - 1, y1))
    if x1 < right:
        pieces.append((x1 + 1, y0, right, y1))
    return tuple(pieces)
