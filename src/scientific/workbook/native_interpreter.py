from __future__ import annotations

from dataclasses import dataclass
from decimal import Decimal
from hashlib import sha256
import re

from .modification_intent import normalize_request, parse_modification_request


VERSION = "sic_native_task_v1"
INTERPRETER_REVISION = "native_controlled_semantic_ir_v2_dev"
QUERY_POLICY_VERSION = "native_queries_sequential_ir_v2_dev"
CELL = r"\$?[A-Z]{1,3}\$?[1-9]\d*"
RANGE = rf"{CELL}(?::{CELL})?"
QUOTED = r'''(?:"[^"]+"|'[^']+')'''
SHEET = rf"(?:on|in)\s+(?:the\s+)?(?:sheet|worksheet|tab)\s+(?P<sheet>{QUOTED})"


def clauses(text):
    start, quote = 0, None
    result = []
    for i, char in enumerate(text):
        if char in "\"'" and (quote or not (i and text[i - 1].isalnum())):
            if quote == char:
                quote = None
            elif quote is None and char in text[i + 1:]:
                quote = char
        if quote is None and (char == ";" or char in ".!?" and (i + 1 == len(text) or text[i + 1].isspace())):
            if text[start:i].strip():
                result.append((start, i + 1, text[start:i + 1]))
            start = i + 1
    if text[start:].strip():
        result.append((start, len(text), text[start:]))
    return result


def _intent(operation, sheet, **values):
    return {"parse_status": "parsed", "operation": operation, "target_sheet": sheet, **values}


def _atom(text, inherited=None):
    direct = parse_modification_request(text)
    if direct["parse_status"] == "parsed" and not direct.get("target_sheet") and inherited:
        direct["target_sheet"] = inherited
    if direct["parse_status"] == "parsed" and direct.get("target_sheet"):
        return direct
    body = normalize_request(text).rstrip(".!?;").strip()
    body = re.sub(r"^(?:please\s+|then\s+|and\s+then\s+)", "", body, flags=re.I)
    body = re.sub(r"^(?:how can I|how do I|I (?:want|need) to)\s+", "", body, flags=re.I)
    sheet = inherited
    prefix = re.match(rf"^{SHEET}\s*,\s*", body, re.I)
    suffix = re.search(rf"\s+{SHEET}$", body, re.I)
    if prefix:
        sheet, body = prefix["sheet"][1:-1], body[prefix.end():]
    elif suffix:
        sheet, body = suffix["sheet"][1:-1], body[:suffix.start()]
    copied = re.fullmatch(rf"(?:create\s+)?(?:a\s+)?copy\s+(?:of\s+)?(?:sheet|worksheet)\s+({QUOTED})\s+(?:named|as|to)\s+({QUOTED})", body, re.I)
    if copied:
        return _intent("copy_sheet", copied[1][1:-1], new_sheet_name=copied[2][1:-1])
    if not sheet:
        return None
    value = re.fullmatch(rf"(?:set|write|put)\s+({CELL})\s+(?:to|=)\s+({QUOTED}|-?\d+(?:\.\d+)?)", body, re.I)
    reverse_value = re.fullmatch(rf"(?:write|put)\s+({QUOTED}|-?\d+(?:\.\d+)?)\s+(?:in|into)\s+({CELL})", body, re.I)
    if value or reverse_value:
        match = value or reverse_value
        coordinate, literal = (match[1], match[2]) if value else (match[2], match[1])
        literal = literal[1:-1] if literal[:1] in "\"'" else float(Decimal(literal))
        return _intent("write_value", sheet, destination=coordinate.upper(), literal_value=literal)
    replace = re.fullmatch(rf"replace\s+({QUOTED})\s+(?:with|by)\s+({QUOTED})\s+in\s+({RANGE})", body, re.I)
    if replace:
        return _intent("replace_values", sheet, replacement_old=replace[1][1:-1], replacement_new=replace[2][1:-1], source_range=replace[3].upper())
    concat = re.fullmatch(rf"(?:concatenate|combine)\s+({CELL})\s+and\s+({CELL})\s+with\s+(a space|{QUOTED})\s+and\s+(?:write|put)\s+(?:the result|it)\s+(?:in|into)\s+({CELL})", body, re.I)
    if concat:
        return _intent("write_value", sheet, expression="concatenate", source_cells=[concat[1].upper(), concat[2].upper()], destination=concat[4].upper(), separator=" " if concat[3].lower() == "a space" else concat[3][1:-1])
    arithmetic = re.fullmatch(rf"(?:add|sum)\s+({CELL})\s+and\s+({CELL})\s+and\s+(?:write|store|put)\s+(?:the (?:numeric )?result|it)\s+(?:in|into)\s+({CELL})", body, re.I)
    if arithmetic:
        return _intent("write_value", sheet, expression="add", source_cells=[arithmetic[1].upper(), arithmetic[2].upper()], destination=arithmetic[3].upper())
    insert = re.fullmatch(r"insert\s+(?:a|one)\s+(?:blank\s+)?(row|column)\s+at\s+(?:row\s+|column\s+)?([A-Z]{1,3}|[1-9]\d*)", body, re.I)
    if insert:
        axis, position = insert[1].lower(), insert[2].upper()
        return _intent("insert_" + axis, sheet, insertion_axis=axis, insertion_position=int(position) if position.isdigit() else position)
    unmerge = re.fullmatch(rf"unmerge\s+(?:(?:the\s+)?(?:range|cells)\s+)?({RANGE})", body, re.I)
    if unmerge:
        return _intent("unmerge_range", sheet, source_range=unmerge[1].upper())
    return None


def _group_rank(instruction):
    relation = re.search(
        rf"(?:The\s+)?(?P<target_label>[\w ']+?)\s+in\s+Column\s+(?P<target>[A-Z]{{1,3}})\s+is\s+based\s+on\s+"
        rf"(?P<measure_label>[\w ']+?)\s+values?\s+in\s+Column\s+(?P<measure>[A-Z]{{1,3}})\s+within\s+each\s+"
        rf"(?P<group_label>[\w ']+?)\s*\(Column\s+(?P<group>[A-Z]{{1,3}})\)\s+in\s+(?P<sheet>{QUOTED})\s+sheet", instruction, re.I)
    if not relation or len(list(re.finditer(relation.re, instruction))) != 1:
        return None
    if not re.search(r"sequential\s+ranks?|ranks?\s+without\s+(?:gaps|duplicates)", instruction, re.I):
        return None
    if not re.search(r"(?:order\s+of\s+(?:the\s+)?(?:listing|appearance)|row\s+order)", instruction, re.I):
        return None
    descending = bool(re.search(r"top[- ](?:selling|scoring)|highest\s+(?:first|value)|descending", instruction, re.I))
    ascending = bool(re.search(r"lowest\s+(?:first|value)|ascending", instruction, re.I))
    if descending == ascending:
        return None
    if re.search(r"\b(?:if|unless|except|where|only|while|also)\b", instruction, re.I):
        return None
    roles = {name: relation[name].upper() for name in ("measure", "group", "target")}
    if any(column.upper() not in set(roles.values()) for column in re.findall(r"\bcolumn\s+([A-Z]{1,3})\b", instruction, re.I)):
        return None
    for label_name, role in (("measure_label", "measure"), ("group_label", "group"), ("target_label", "target")):
        label = relation[label_name].strip().removeprefix("The ").strip(" '")
        mentions = re.findall(re.escape(label) + r"['\"]?\s+(?:values?\s+)?in\s+column\s+([A-Z]{1,3})\b", instruction, re.I)
        if any(column.upper() != roles[role] for column in mentions):
            return None
    coverage = []
    for start, end, clause in clauses(instruction):
        lower = clause.lower()
        if not re.search(r"\brank(?:s|ing|ed)?\b", lower) and not (start <= relation.start() < end):
            return None
        if re.search(r"\b(?:delete|remove|insert|copy|clear|sort|format(?:ting)?|layout|colou?r(?:ing)?|highlight|email|send|transpose|filter|export)\b", lower):
            return None
        if re.search(r"\bmove\b", clause, re.I) and not re.search(r"\bmove up the ranking\b", clause, re.I):
            return None
        purpose = re.search(r"\bso that I can\b", clause, re.I)
        operational = clause[:purpose.start()] if purpose else clause
        if any(word.lower() not in {"within", "when", "for", "in", "the", "their"}
               for word in re.findall(r"\band\s+(\w+)", operational, re.I)):
            return None
        if any(word.lower() not in {"when", "they", "which", "with", "having", "there", "assign", "move"}
               for word in re.findall(r",\s*(\w+)", operational)):
            return None
        if re.search(r"\b(?:create|generate|write|replace|add)\b", operational, re.I):
            return None
        if not (re.match(r"\s*(?:modify|update|fix|currently|when|the\b|I (?:need|want)|specifically|for\b|subsequently)", clause, re.I)
                and not re.search(r"\b(?:also|and then|in addition)\b", operational, re.I)):
            return None
        coverage.append({"start": start, "end": end, "kind": "rank_constraint_or_context"})
    columns = [relation[name].upper() for name in ("measure", "group", "target")]
    if len(set(columns)) != 3:
        return None
    return {"kind": "stable_group_rank", "type": "NUMERICA", "sheet": relation["sheet"][1:-1],
            **{name: relation[name].upper() for name in ("measure", "group", "target")},
            **{name: relation[name].strip().removeprefix("The ").strip(" '") for name in ("measure_label", "group_label", "target_label")},
            "order": "descending" if descending else "ascending", "tie_break": "input_row_order", "coverage": coverage}


def _interpret_legacy_task(instruction):
    if not isinstance(instruction, str) or not instruction.strip():
        raise ValueError("Instruction must be nonempty text")
    result = {"schema_version": VERSION, "interpreter_revision": INTERPRETER_REVISION, "instruction": instruction,
              "instruction_sha256": sha256(instruction.encode("utf8")).hexdigest(),
              "status": "unsupported", "requirements": [], "uninterpreted_spans": [], "warnings": []}
    from .native_operations import interpret_operation
    operation = interpret_operation(instruction)
    if operation:
        result.update(status="interpreted", requirements=[{"id":"r1", "span":[0,len(instruction)], "depends_on":[], **operation}])
        return result
    atom = _atom(instruction)
    if atom:
        result.update(status="interpreted", requirements=[{"id": "r1", "kind": "atomic", "type": _operation_type(atom),
                    "span": [0, len(instruction)], "intent": atom, "depends_on": []}])
        return result
    rank = _group_rank(instruction)
    if rank:
        result.update(status="interpreted", requirements=[{"id": "r1", "span": [0, len(instruction)], "depends_on": [], **rank}])
        return result
    constant = re.fullmatch(r"\s*(?P<label>[\w ()%-]+?)\s+(?:are|is|should be)\s+(?P<value>-?\d+(?:\.\d+)?)%\s+in\s+Years?\s+(?P<first>\d+)\s*[-–]\s*(?P<last>\d+)\s*\.?\s*", instruction, re.I)
    if constant and 1 <= int(constant["first"]) <= int(constant["last"]) <= 50:
        result.update(status="interpreted", requirements=[{"id": "r1", "kind": "labelled_year_constant", "type": "NUMERICA",
            "span": [0, len(instruction)], "depends_on": [], "label": constant["label"], "value": float(Decimal(constant["value"]) / 100),
            "years": list(range(int(constant["first"]), int(constant["last"]) + 1))}])
        return result
    pieces = clauses(instruction)
    if len(pieces) == 1:
        protected = []
        for match in re.finditer(QUOTED, instruction):
            protected.append((match.start(), match.end()))
        cuts = [match for match in re.finditer(r"\s+(?:and then|and|e|then)\s+(?=(?:set|write|put|copy|insert|unmerge|replace|change|altere|mude)\b)", instruction, re.I)
                if not any(start <= match.start() < end for start, end in protected)]
        for cut in cuts:
            left = _atom(instruction[:cut.start()])
            if left:
                right = _atom(instruction[cut.end():], left["target_sheet"])
                if right:
                    pieces = [(0, cut.start(), instruction[:cut.start()]), (cut.end(), len(instruction), instruction[cut.end():])]
                    break
    if len(pieces) == 1:
        result["uninterpreted_spans"] = [[0, len(instruction)]]
        result["warnings"] = ["NO_COMPLETE_SUPPORTED_INTERPRETATION"]
        return result
    inherited = None
    for start, end, text in pieces:
        parsed = _atom(text.strip(), inherited)
        if parsed is None:
            result["uninterpreted_spans"].append([start, end])
            continue
        inherited = parsed["target_sheet"]
        number = len(result["requirements"]) + 1
        result["requirements"].append({"id": f"r{number}", "kind": "atomic", "type": _operation_type(parsed),
            "span": [start, end], "intent": parsed, "depends_on": [f"r{number - 1}"] if number > 1 else []})
    if result["uninterpreted_spans"] or not result["requirements"]:
        result["warnings"] = ["ENTIRE_TASK_REFUSED_UNINTERPRETED_REQUIREMENT"]
    else:
        result["status"] = "interpreted"
    return result


def interpret_native_task(instruction):
    result = _interpret_legacy_task(instruction)
    if result["status"] == "interpreted" and len(result["requirements"]) == 1:
        return result
    from .semantic_parser import parse_semantic_program
    from .semantic_compiler import native_requirements
    program = parse_semantic_program(instruction)
    if program is not None:
        result.update(status="interpreted", requirements=native_requirements(program), semantic_ir=program.to_dict(),
                      uninterpreted_spans=[], warnings=[])
    elif result["status"] == "interpreted":
        result.update(status="unsupported", requirements=[], uninterpreted_spans=[[0, len(instruction)]],
                      warnings=["UNQUALIFIED_LEGACY_COMPOSITION"])
    return result


def _operation_type(intent):
    operation = intent["operation"]
    if operation in {"copy_sheet", "insert_row", "insert_column", "sort_range", "unmerge_range", "delete_row"}:
        return "ESTRUTURAL"
    if operation == "write_formula" or intent.get("expression") == "add":
        return "NUMERICA"
    return "NUMERICA" if isinstance(intent.get("literal_value"), (float, int)) else "TEXTUAL"


@dataclass(frozen=True)
class NativeQueryPlan:
    queries: tuple
    subgoals: tuple
    requirements: tuple
    architecture: str

    def to_dict(self):
        return {"schema_version": "sic_native_queries_v1", "query_policy_version": QUERY_POLICY_VERSION, "architecture_id": self.architecture,
                "queries": list(self.queries), "subgoals": list(self.subgoals),
                "requirement_ids": [list(group) for group in self.requirements]}


def build_native_queries(task, architecture):
    queries = [normalize_request(task["instruction"])]
    groups = [tuple(r["id"] for r in task["requirements"])]
    if architecture == "C2" and task["status"] == "interpreted":
        descriptions = []
        for r in task["requirements"]:
            if r["kind"] == "typed_expression":
                from .semantic_ir import Goal
                goal = Goal.from_dict(r["ir_goal"])
                source = " ".join(f'{ref.sheet or goal.target.sheet or "workbook"}!{ref.address}' for ref in goal.references())
                descriptions.append((r["id"], f'{goal.target.sheet or "workbook"} {goal.operation.value} {goal.expression.operator} sources {source} target {goal.target.address} complete domain'))
            elif r["kind"] == "stable_group_rank":
                descriptions.extend([(r["id"], f'sheet "{r["sheet"]}" column {r["measure"]} {r["measure_label"]} data extent'),
                                     (r["id"], f'sheet "{r["sheet"]}" column {r["group"]} {r["group_label"]} grouping'),
                                     (r["id"], f'sheet "{r["sheet"]}" column {r["target"]} {r["target_label"]} sequential rank {r["tie_break"]}')])
            elif r["kind"] == "conditional_sum":
                from .native_sum import sheet_description
                target = sheet_description(r["target_sheet_reference"])
                source = sheet_description(r["source_sheet_reference"])
                descriptions.extend([(r["id"], f'{target} criterion {r["criterion"]} formula destination {r["destination"]}'),
                                     (r["id"], f'{source} sum values {r["sum_range"]}'),
                                     (r["id"], f'{source} equality criteria {r["criteria_range"]}')])
            elif r["kind"] == "conditional_existing_lookup":
                descriptions.extend([(r["id"], f'conditional formula {r["ranges"]["target"]} compare {r["ranges"]["left"]} {r["ranges"]["right"]}'),
                                     (r["id"], f'return {r["ranges"]["value"]} preserve {r["original_formula"]}')])
            elif r["kind"] == "labelled_year_constant":
                descriptions.append((r["id"], f'{r["label"]} Year ' + " ".join(map(str, r["years"]))))
            elif r["kind"] in {"normalize_column_spaces", "conditional_text_suffix", "timestamp_difference"}:
                columns = r.get("columns") or [r[k] for k in ("start_column", "end_column", "predicate_column", "target_column") if k in r]
                descriptions.append((r["id"], r["kind"] + " complete column domains " + " ".join(columns)))
            elif r["kind"] in {"text_contains_choice", "filter_delete_rows", "sum_ignoring_error"}:
                fields = [r["kind"], r.get("source_range", ""), r.get("target_column", ""), r.get("column", "")]
                descriptions.append((r["id"], " ".join(fields)))
            else:
                intent = r["intent"]
                references = [str(intent.get(name) or "") for name in ("source_range", "destination", "target_coordinate")]
                descriptions.append((r["id"], f'sheet "{intent["target_sheet"]}" {intent["operation"]} ' + " ".join(intent.get("source_cells") or []) + " " + " ".join(references)))
        buckets = [[] for _ in range(min(3, len(descriptions)))]
        for index, description in enumerate(descriptions):
            buckets[index % len(buckets)].append(description)
        for bucket in buckets:
            query = normalize_request(" ; ".join(text for _, text in bucket))
            if query not in queries:
                queries.append(query)
                groups.append(tuple(dict.fromkeys(identifier for identifier, _ in bucket)))
    return NativeQueryPlan(tuple(queries), tuple(["global"] + [f"requirements_{i}" for i in range(1, len(queries))]), tuple(groups), architecture)
