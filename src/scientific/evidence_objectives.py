from copy import deepcopy
import re

from openpyxl.formula.tokenizer import Tokenizer
from openpyxl.utils.cell import coordinate_to_tuple, range_boundaries, get_column_letter


VERSION = "objective_evidence_v1"


class EvidenceSearchResults(list):
    def __init__(self, results, routing_trace):
        super().__init__(results)
        self.routing_trace = routing_trace


def _text(value):
    return " ".join(str(value).casefold().split())


def _bounds(value):
    try:
        result = range_boundaries(value)
        return result if all(type(v) is int and v > 0 for v in result) else None
    except (ValueError, TypeError):
        return None


def _contains(outer, inner):
    return bool(outer and inner and outer[0] <= inner[0] <= inner[2] <= outer[2]
                and outer[1] <= inner[1] <= inner[3] <= outer[3])


def evidence_matches(requirement, document):
    if document.get("sheet_name") != requirement["sheet"]:
        return False
    kind = requirement["kind"]
    cells = [c for c in document.get("cells", []) if isinstance(c, dict) and c.get("coordinate")]
    if kind in {"column_header", "label"}:
        for cell in cells:
            if "value" not in cell or kind == "column_header" and re.sub(r"\d", "", cell["coordinate"]).upper() != requirement["column"]:
                continue
            value, label = _text(cell["value"]), _text(requirement["label"])
            if value == label or requirement.get("allow_annotation") and value.startswith(label + " ("):
                return True
        return False
    if kind == "complete_sheet":
        return document.get("block_type") in {"used_range", "sheet_overview"} and document.get("structural_metadata", {}).get("source_block_complete") is True
    if kind == "existing_formula":
        return any(str(c.get("value", "")).upper() == requirement["formula"].upper() for c in cells)
    if kind == "column_domain":
        column = requirement["column"]
        bounds = _bounds(document.get("range"))
        index = coordinate_to_tuple(column + "1")[1]
        return bool(document.get("structural_metadata", {}).get("source_block_complete") is True
                    and document.get("block_type") in {"column_block", "sheet_overview", "used_range"}
                    and bounds and bounds[0] <= index <= bounds[2]
                    and any(re.sub(r"\d", "", c["coordinate"]).upper() == column for c in cells))
    if kind == "sheet_structure":
        metadata = document.get("structural_metadata", {})
        return document.get("block_type") == "sheet_overview" or all(type(metadata.get(k)) is int and metadata[k] > 0 for k in ["max_row", "max_column"])
    if kind == "sheet":
        return True
    reference = requirement["reference"]
    if kind == "cell_value":
        return any(c["coordinate"].replace("$", "").upper() == reference.replace("$", "").upper() and "value" in c for c in cells)
    if requirement.get("allow_column_blanks") and document.get("block_type") == "column_block" and document.get("structural_metadata", {}).get("source_block_complete") is True:
        extent, requested = _bounds(document.get("range")), _bounds(reference)
        if extent and requested and extent[0] == extent[2] == requested[0] == requested[2]:
            return True
    return _contains(_bounds(document.get("range")), _bounds(reference)) or any(_contains(_bounds(c["coordinate"]), _bounds(reference)) for c in cells)


def derive_evidence_requirements(task, documents=()):
    if task.get("status") != "interpreted":
        return []
    documents = list(documents)
    result = []
    for goal in task["requirements"]:
        def add(suffix, kind, sheet, **values):
            result.append({"id": goal["id"] + "." + suffix, "goal_id": goal["id"], "kind": kind, "sheet": sheet, **values})
        if goal["kind"] == "typed_expression":
            from scientific.workbook.semantic_ir import Goal
            from scientific.workbook.semantic_compiler import _sheet_names
            typed = Goal.from_dict(goal["ir_goal"])
            try:
                sheets = _sheet_names(typed.target, documents)
            except ValueError:
                sheets = (typed.target.sheet,)
            for index, sheet in enumerate(sheets):
                add(str(index) + ".sheet", "sheet_structure", sheet)
                for number, ref in enumerate(typed.references()):
                    source_sheet = ref.sheet if ref.sheet not in (None, '*') else sheet
                    if ref.is_symbolic:
                        add(str(index) + ".source." + str(number), "complete_sheet", source_sheet)
                    elif ref.is_cell:
                        add(str(index) + ".source." + str(number), "cell_value", source_sheet, reference=ref.address)
                    elif ref.is_range:
                        add(str(index) + ".source." + str(number), "range", source_sheet, reference=ref.address)
                    else:
                        add(str(index) + ".source." + str(number), "column_domain", source_sheet, column=ref.address)
        elif goal["kind"] == "stable_group_rank":
            for role in ["measure", "group", "target"]:
                add(role + ".header", "column_header", goal["sheet"], column=goal[role], label=goal[role + "_label"], allow_annotation=role == "measure")
                add(role + ".domain", "column_domain", goal["sheet"], column=goal[role])
            target = next(r for r in result if r["id"] == goal["id"] + ".target.domain")
            dependencies = {}
            for document in documents:
                if not evidence_matches(target, document):
                    continue
                for cell in document.get("cells", []):
                    if re.sub(r"\d", "", cell.get("coordinate", "")).upper() != goal["target"]:
                        continue
                    value = str(cell.get("value") or "")
                    formula = value.split(", text=", 1)[1][:-1] if value.startswith("ArrayFormula(") and ", text=" in value else value
                    if not formula.startswith("="):
                        continue
                    try:
                        tokens = Tokenizer(formula).items
                    except Exception:
                        continue
                    for token in tokens:
                        if token.type != "OPERAND" or token.subtype != "RANGE" or not re.fullmatch(r"\$?[A-Z]{1,3}\$?[1-9]\d*(?::\$?[A-Z]{1,3}\$?[1-9]\d*)?", token.value, re.I):
                            continue
                        bounds = _bounds(token.value)
                        if bounds and bounds[0] == bounds[2]:
                            column = get_column_letter(bounds[0])
                            if column not in {goal[name] for name in ["measure", "group", "target"]}:
                                dependencies.setdefault(column, set()).add(document["block_id"])
            for column, witnesses in sorted(dependencies.items()):
                add("dependency." + column, "column_domain", goal["sheet"], column=column,
                    derivation="indexed_target_formula_reference", derivation_block_ids=sorted(witnesses))
        elif goal["kind"] == "conditional_sum":
            from scientific.workbook.native_sum import resolve_sheet
            sheets = {}
            for role in ("source", "target"):
                try:
                    sheets[role] = resolve_sheet(goal[role + "_sheet_reference"], documents)
                except ValueError:
                    sheets[role] = None
            add("criterion", "cell_value", sheets["target"], reference=goal["criterion"])
            add("destination", "region", sheets["target"], reference=goal["destination"])
            add("criteria_range", "region", sheets["source"], reference=goal["criteria_range"], allow_column_blanks=True)
            add("sum_range", "region", sheets["source"], reference=goal["sum_range"], allow_column_blanks=True)
        elif goal["kind"] == "conditional_existing_lookup":
            from scientific.workbook.native_conditional import conditional_sheet
            try:
                sheet = conditional_sheet(goal, documents)
            except ValueError:
                sheet = None
            add("sheet", "sheet", sheet)
            for role, reference in goal["ranges"].items():
                add(role, "region", sheet, reference=reference)
            add("lookup_key", "region", sheet, reference=goal["lookup_key_range"])
        elif goal["kind"] == "labelled_year_constant":
            inventories = {tuple(d.get("structural_metadata", {}).get("workbook_content_sheets", [])) for d in documents
                           if d.get("structural_metadata", {}).get("workbook_content_sheets")}
            sheet = next(iter(inventories))[0] if len(inventories) == 1 and len(next(iter(inventories))) == 1 else None
            add("label", "label", sheet, label=goal["label"])
            for year in goal["years"]:
                add("year." + str(year), "label", sheet, label=f"Year {year}")
        elif goal["kind"] in {"normalize_column_spaces", "conditional_text_suffix", "timestamp_difference"}:
            from scientific.workbook.native_columns import operation_sheets
            try:
                sheets = operation_sheets(goal, documents)
            except ValueError:
                sheets = (None,)
            for i, sheet in enumerate(sheets):
                add("complete_sheet." + str(i), "complete_sheet", sheet)
        elif goal["kind"] in {"text_contains_choice", "filter_delete_rows", "sum_ignoring_error"}:
            from scientific.workbook.native_operations import sheet_for
            try:
                sheet = sheet_for(goal, documents)
            except ValueError:
                sheet = goal.get("sheet")
            add("sheet", "sheet", sheet)
            if goal["kind"] == "filter_delete_rows":
                add("complete_sheet", "complete_sheet", sheet)
                add("predicate_column", "column_domain", sheet, column=goal["column"])
            else:
                add("source", "region", sheet, reference=goal["source_range"])
                if goal["kind"] == "sum_ignoring_error":
                    add("destination", "existing_formula", sheet, formula=goal["current_formula"])
                else:
                    bounds = _bounds(goal["source_range"])
                    if bounds:
                        add("destination", "region", sheet, reference=f'{goal["target_column"]}{bounds[1]}:{goal["target_column"]}{bounds[3]}')
        else:
            intent = goal["intent"]
            sheet = intent["target_sheet"]
            add("sheet", "sheet", sheet)
            for number, cell in enumerate(intent.get("source_cells") or []):
                add("source." + str(number), "cell_value", sheet, reference=cell)
            for field in ["source_range", "destination", "target_coordinate"]:
                if intent.get(field):
                    add(field, "region", sheet, reference=intent[field])
            if intent["operation"] in {"copy_sheet", "insert_row", "insert_column"}:
                add("structure", "sheet_structure", sheet)
    return result


def select_objective_evidence(candidates, requirements, budget, *, documents=None, threshold=None):
    if type(budget) is not int or budget < 0:
        raise ValueError("Evidence budget must be a nonnegative integer")
    by_requirement = {r["id"]: r for r in requirements}
    ordered = sorted(candidates, key=lambda c: (-c["score"], c.get("rank", 0), str(c.get("candidate_id") or c["metadata"].get("block_id", ""))))
    def identity(candidate):
        return candidate["metadata"].get("block_id") or candidate["candidate_id"]
    coverage = {}
    unique = []
    for candidate in ordered:
        block = identity(candidate)
        if block in coverage:
            continue
        document = documents.get(block, {}) if documents is not None else candidate["metadata"]
        if document and document.get("block_id") != block:
            raise ValueError("Evidence/index block identity mismatch")
        coverage[block] = {r["id"] for r in requirements if evidence_matches(r, document)}
        unique.append(candidate)
    eligible = [c for c in unique if threshold is None or c["score"] >= threshold]
    selected, used, touched, events = [], set(), set(), []
    while eligible and len(selected) < budget:
        def gain(candidate):
            new = coverage[identity(candidate)] - used
            goals = {by_requirement[r]["goal_id"] for r in new}
            return len(goals - touched), len(new), candidate["score"]
        candidate = max(eligible, key=gain)
        eligible.remove(candidate)
        new = coverage[identity(candidate)] - used
        selected.append(candidate)
        events.append({"block_id": identity(candidate), "new_requirement_ids": sorted(new), "score": candidate["score"]})
        used.update(new)
        touched.update(by_requirement[r]["goal_id"] for r in new)
    for candidate in unique:
        if len(selected) >= budget:
            break
        if all(identity(candidate) != identity(c) for c in selected):
            selected.append(candidate)
    trace = {"schema_version": VERSION, "requirements": deepcopy(requirements), "budget": budget,
             "selected": events, "uncovered_requirement_ids": sorted(set(by_requirement) - used),
             "providers": [{"block_id": identity(c), "semantic_rank": c.get("rank"), "score": c["score"],
                            "requirement_ids": sorted(coverage[identity(c)]),
                            "meets_threshold": threshold is None or c["score"] >= threshold}
                           for c in unique if coverage[identity(c)]],
             "scope": "Evidence routing and coverage only; grounding remains independently required"}
    return selected, trace
