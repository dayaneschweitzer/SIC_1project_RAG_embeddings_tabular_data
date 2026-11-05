from __future__ import annotations

from dataclasses import replace
from datetime import datetime, time, timedelta
from decimal import Decimal, ROUND_HALF_UP
from math import isfinite
import re

from openpyxl.utils.cell import coordinate_to_tuple, get_column_letter, range_boundaries
from openpyxl.utils.datetime import to_excel

from scientific.contracts import oracle_free

from .action_grounding import ground_action
from .action_plan import ActionPlan, PlanStep, PreservedRequirement
from .semantic_ir import Expression, IRValidationError, Reference, SemanticProgram, ValueType, WriteOperation
from .spreadsheet_editor import executor_supports


class SemanticCompilationError(ValueError):
    pass


def _text_literal(value):
    return '"' + value.replace('"', '""') + '"'


def _coordinate(ref, row):
    if ref.sheet in (None, "*"):
        raise SemanticCompilationError("UNRESOLVED_SHEET_REFERENCE")
    if ref.is_cell or ref.is_range:
        return ref.address
    if row is None:
        raise SemanticCompilationError("UNRESOLVED_ROW_REFERENCE")
    if ref.start_row is not None and row < ref.start_row or ref.end_row is not None and row > ref.end_row:
        raise SemanticCompilationError("REFERENCE_ROW_OUTSIDE_DECLARED_EXTENT")
    return ref.address + str(row)


def formula_expression(expression, row, sheet):
    op, args = expression.operator, expression.arguments
    if op == "literal":
        value = expression.value
        return '""' if value is None else "TRUE()" if value is True else "FALSE()" if value is False else _text_literal(value) if isinstance(value, str) else str(value)
    if op == "reference":
        ref = expression.reference
        coordinate = _coordinate(ref, row)
        if ref.sheet != sheet:
            coordinate = "'" + ref.sheet.replace("'", "''") + "'!" + coordinate
        return "IF(ISNUMBER(" + coordinate + ")," + coordinate + ",TIMEVALUE(" + coordinate + "))" if ref.value_type in {ValueType.TIME, ValueType.TEMPORAL} else coordinate
    if op == "is_blank" and args[0].reference is not None:
        ref = args[0].reference
        coordinate = _coordinate(ref, row)
        if ref.sheet != sheet:
            coordinate = "'" + ref.sheet.replace("'", "''") + "'!" + coordinate
        return "(" + coordinate + '="")'
    values = [formula_expression(a, row, sheet) for a in args]
    from .semantic_operators import OPERATORS, formula as operator_formula
    if op in OPERATORS:
        return operator_formula(op, values)
    binary = {"add": "+", "subtract": "-", "multiply": "*", "divide": "/", "eq": "=", "ne": "<>", "lt": "<", "le": "<=", "gt": ">", "ge": ">="}
    if op in binary:
        return "(" + values[0] + binary[op] + values[1] + ")"
    if op in {"if", "and", "or", "not", "mod", "round", "trim", "sum", "min", "max"}:
        return op.upper() + "(" + ",".join(values) + ")"
    if op == "count_eq":
        return "SUMPRODUCT(--(" + values[0] + "=" + values[1] + "),--NOT(ISBLANK(" + values[0] + ")))"
    if op == "lookup_exact":
        key = 'IF(ISTEXT(' + values[0] + '),SUBSTITUTE(SUBSTITUTE(SUBSTITUTE(' + values[0] + ',"~","~~"),"*","~*"),"?","~?"),' + values[0] + ')'
        result = "INDEX(" + values[2] + ",MATCH(" + key + "," + values[1] + ",0))"
        return "IF(ISNA(MATCH(" + key + "," + values[1] + ",0))," + values[3] + "," + result + ")" if len(values) == 4 else result
    if op == "case":
        result = "NA()"
        for index in range(len(values) - 2, -1, -2):
            result = "IF(" + values[index] + "," + values[index + 1] + "," + result + ")"
        return result
    if op == 'if_error':
        return 'IFERROR(' + ','.join(values) + ')'
    if op == "is_blank":
        return "(" + values[0] + '="")'
    if op == "concat":
        return "(" + "&".join(values) + ")"
    if op == "remove_spaces":
        return "SUBSTITUTE(" + values[0] + '," ","")'
    if op == "replace":
        return "SUBSTITUTE(" + ",".join(values) + ")"
    if op == "contains":
        needle = values[1]
        if args[1].operator == "literal":
            needle = _text_literal(args[1].value.replace("~", "~~").replace("*", "~*").replace("?", "~?"))
        else:
            needle = 'SUBSTITUTE(SUBSTITUTE(SUBSTITUTE(' + needle + ',"~","~~"),"*","~*"),"?","~?")'
        return "ISNUMBER(SEARCH(" + needle + "," + values[0] + "))"
    if op == "parenthesized":
        start = 'FIND("(",' + values[0] + ")+1"
        return "MID(" + values[0] + "," + start + ',FIND(")",' + values[0] + "," + start + ")-(" + start + "))"
    if op == "between":
        if args[2].operator != "literal" or type(args[2].value) is not int or not 1 <= args[2].value <= 20:
            raise SemanticCompilationError("INVALID_DELIMITER_ORDINAL")
        previous = "0"
        for _ in range(args[2].value):
            previous = "FIND(" + values[1] + "," + values[0] + "," + previous + "+1)"
        start = "(" + previous + "+LEN(" + values[1] + "))"
        return "MID(" + values[0] + "," + start + ",FIND(" + values[3] + "," + values[0] + "," + start + ")-" + start + ")"
    if op == "hours":
        return "(" + values[0] + "*24)"
    if op == "time_shift":
        return "MOD(" + values[0] + "+(" + values[1] + "/1440),1)"
    raise SemanticCompilationError("UNSUPPORTED_COMPILER_OPERATOR:" + op)


def _cell_value(cell, value_type, epoch=None):
    if cell.get("is_formula") or cell.get("data_type") == "f":
        raise SemanticCompilationError("FORMULA_SOURCE_TYPE_NOT_QUALIFIED")
    value, kind = cell.get("value"), cell.get("data_type")
    if value is None or value == "None" and kind == "n":
        return None
    if value_type == ValueType.SCALAR:
        if kind in {"s", "str", "inlineStr"} and isinstance(value, str):
            return value
        if kind == "b":
            return _cell_value(cell, ValueType.BOOLEAN)
        if kind == 'd':
            try:
                parsed = value if isinstance(value, (datetime, time)) else time.fromisoformat(value) if re.fullmatch(r'\d{2}:\d{2}(?::\d{2}(?:\.\d+)?)?', value) else datetime.fromisoformat(value)
            except (TypeError, ValueError):
                raise SemanticCompilationError('INVALID_TEMPORAL_SOURCE') from None
            if getattr(parsed, 'tzinfo', None) is not None:
                raise SemanticCompilationError('TIMEZONE_NOT_QUALIFIED')
            return parsed
        return _cell_value(cell, ValueType.NUMBER)
    if value_type == ValueType.TEXT:
        if kind not in {"s", "str", "inlineStr"} or not isinstance(value, str):
            raise SemanticCompilationError("TYPED_TEXT_SOURCE_REQUIRED")
        return value
    if value_type == ValueType.BOOLEAN:
        if kind != "b" or not (type(value) is bool or isinstance(value, str) and value in {"True", "False"}):
            raise SemanticCompilationError("TYPED_BOOLEAN_SOURCE_REQUIRED")
        return value is True or value == "True"
    if value_type == ValueType.NUMBER or value_type == ValueType.DURATION:
        if kind != "n" or type(value) is bool:
            raise SemanticCompilationError("TYPED_NUMERIC_SOURCE_REQUIRED")
        try:
            numeric = float(value)
        except (TypeError, ValueError):
            raise SemanticCompilationError("TYPED_NUMERIC_SOURCE_REQUIRED") from None
        if not isfinite(numeric):
            raise SemanticCompilationError("FINITE_NUMERIC_SOURCE_REQUIRED")
        return numeric
    if value_type in {ValueType.TIME, ValueType.DATETIME, ValueType.TEMPORAL}:
        text_time = value_type in {ValueType.TIME, ValueType.TEMPORAL} and kind in {"s", "str", "inlineStr"} and isinstance(value, str) and re.fullmatch(r"\d{2}:\d{2}(?::\d{2})?", value) and re.fullmatch(r"h{1,2}:mm(?::ss)?", str(cell.get("number_format", "")), re.I)
        if kind != "d" and not text_time:
            raise SemanticCompilationError("TYPED_TEMPORAL_SOURCE_REQUIRED")
        try:
            parsed = value if isinstance(value, (datetime, time, timedelta)) else time.fromisoformat(value) if value_type == ValueType.TIME or value_type == ValueType.TEMPORAL and re.fullmatch(r'\d{2}:\d{2}(?::\d{2}(?:\.\d+)?)?', value) else datetime.fromisoformat(value)
        except (TypeError, ValueError):
            raise SemanticCompilationError("INVALID_TEMPORAL_SOURCE") from None
        if value_type == ValueType.TIME and not isinstance(parsed, time) or value_type == ValueType.DATETIME and not isinstance(parsed, datetime):
            raise SemanticCompilationError("TEMPORAL_KIND_MISMATCH")
        if not isinstance(parsed, (time, datetime)):
            raise SemanticCompilationError("TEMPORAL_KIND_MISMATCH")
        if getattr(parsed, "tzinfo", None) is not None:
            raise SemanticCompilationError("TIMEZONE_NOT_QUALIFIED")
        if value_type == ValueType.TEMPORAL and isinstance(parsed, datetime):
            if epoch not in {'1899-12-30 00:00:00', '1904-01-01 00:00:00'}:
                raise SemanticCompilationError('WORKBOOK_EPOCH_EVIDENCE_REQUIRED')
            return to_excel(parsed, datetime.fromisoformat(epoch))
        return to_excel(parsed)
    raise SemanticCompilationError("UNSUPPORTED_SOURCE_TYPE")


def evaluate_expression(expression, row, sheet, cells, resolver=None):
    op, args = expression.operator, expression.arguments
    if op == "literal":
        return expression.value
    if op == "reference":
        ref = expression.reference
        if resolver is not None:
            return resolver.value(ref, row)
        if ref.sheet != sheet:
            raise SemanticCompilationError("CROSS_SHEET_EXPRESSION_NOT_QUALIFIED")
        key = _coordinate(ref, row)
        if key not in cells:
            raise SemanticCompilationError("SOURCE_CELL_EVIDENCE_MISSING:" + key)
        return _cell_value(cells[key], ref.value_type)
    if op == "is_blank" and args[0].reference is not None:
        if resolver is not None:
            return resolver.value(args[0].reference, row) in (None, '')
        key = _coordinate(args[0].reference, row)
        if key not in cells or cells[key].get("is_formula") or cells[key].get("data_type") == "f":
            raise SemanticCompilationError("BLANK_PREDICATE_SOURCE_NOT_QUALIFIED")
        cell = cells[key]
        return cell.get("value") in (None, "") or cell.get("value") == "None" and cell.get("data_type") == "n"
    evaluate = lambda a: evaluate_expression(a, row, sheet, cells, resolver)
    if op == "if":
        condition = evaluate(args[0])
        if type(condition) is not bool:
            raise SemanticCompilationError("BOOLEAN_CONDITION_REQUIRED")
        return evaluate(args[1] if condition else args[2])
    if op in {"and", "or"}:
        result = [evaluate(a) for a in args]
        if any(type(v) is not bool for v in result):
            raise SemanticCompilationError("BOOLEAN_CONDITION_REQUIRED")
        return all(result) if op == "and" else any(result)
    if op == "case":
        guards = [evaluate(args[index]) for index in range(0, len(args), 2)]
        if any(type(v) is not bool for v in guards):
            raise SemanticCompilationError("BOOLEAN_CONDITION_REQUIRED")
        selected = [index * 2 for index, value in enumerate(guards) if value]
        if len(selected) != 1:
            raise SemanticCompilationError("UNSPECIFIED_OR_OVERLAPPING_CASE_BRANCH")
        return evaluate(args[selected[0] + 1])
    if op == 'if_error':
        try:
            return evaluate(args[0])
        except SemanticCompilationError as error:
            if str(error) not in {'DELIMITER_NOT_FOUND', 'UNQUALIFIED_ARITHMETIC_DOMAIN', 'UNQUALIFIED_ROUND_DOMAIN', 'NONFINITE_EXPRESSION_RESULT'}:
                raise
            return evaluate(args[1])
    if op == "lookup_exact":
        key, keys = evaluate(args[0]), evaluate(args[1])
        values = evaluate(args[2])
        if not isinstance(keys, list) or not isinstance(values, list) or len(keys) != len(values):
            raise SemanticCompilationError('LOOKUP_RANGES_NOT_ALIGNED')
        if key is None:
            raise SemanticCompilationError('UNSPECIFIED_BLANK_LOOKUP_KEY')
        equal = lambda a, b: a.casefold() == b.casefold() if isinstance(a, str) and isinstance(b, str) else type(a) is not bool and type(b) is not bool and a == b
        matches = [i for i, candidate in enumerate(keys) if candidate is not None and equal(candidate, key)]
        if len(matches) > 1:
            raise SemanticCompilationError('AMBIGUOUS_LOOKUP_KEY')
        if not matches:
            if len(args) == 4:
                return evaluate(args[3])
            raise SemanticCompilationError('UNRESOLVED_LOOKUP_KEY')
        if values[matches[0]] is None:
            raise SemanticCompilationError('UNSPECIFIED_BLANK_LOOKUP_RESULT')
        return values[matches[0]]
    values = [evaluate(a) for a in args]
    from .semantic_operators import OPERATORS, evaluate as evaluate_operator
    if op in OPERATORS:
        result = evaluate_operator(op, values)
        if type(result) in (int, float) and not isfinite(result):
            raise SemanticCompilationError('NONFINITE_EXPRESSION_RESULT')
        return result
    if op in {'sum', 'min', 'max'}:
        items = [item for value in values for item in (value if isinstance(value, list) else [value]) if item is not None]
        if not items or any(type(v) not in (int, float) or not isfinite(v) for v in items):
            raise SemanticCompilationError('TYPED_AGGREGATE_DOMAIN_REQUIRED')
        result = {'sum': sum, 'min': min, 'max': max}[op](items)
        if not isfinite(result):
            raise SemanticCompilationError('NONFINITE_EXPRESSION_RESULT')
        return result
    if op == 'count_eq':
        items, criterion = values
        if not isinstance(items, list) or criterion is None:
            raise SemanticCompilationError('TYPED_COUNT_ARGUMENTS_REQUIRED')
        return sum((v.casefold() == criterion.casefold() if isinstance(v, str) and isinstance(criterion, str) else type(v) is not bool and type(criterion) is not bool and v == criterion) for v in items if v is not None)
    if op == "is_blank":
        return values[0] in (None, "")
    if op == "not":
        if type(values[0]) is not bool:
            raise SemanticCompilationError("BOOLEAN_CONDITION_REQUIRED")
        return not values[0]
    if op in {"eq", "ne"}:
        left, right = values
        if left is None or right is None:
            other = right if left is None else left
            if type(other) in (int, float) and other == 0:
                raise SemanticCompilationError("UNSPECIFIED_BLANK_NUMERIC_COMPARISON")
            equal = other in (None, "")
            return equal if op == "eq" else not equal
        equal = type(left) is type(right) and left.casefold() == right.casefold() if isinstance(left, str) else type(left) is not bool and type(right) is not bool and left == right or type(left) is bool and type(right) is bool and left == right
        return equal if op == "eq" else not equal
    if op == "contains" and values[0] is None:
        if not values[1]:
            raise SemanticCompilationError("EMPTY_CONTAINS_PATTERN")
        return False
    if any(v is None for v in values):
        raise SemanticCompilationError("UNSPECIFIED_BLANK_SOURCE")
    if op in {"trim", "remove_spaces"}:
        if any(c.isspace() and c != " " for c in values[0]):
            raise SemanticCompilationError("NON_ASCII_WHITESPACE_NOT_QUALIFIED")
        return " ".join(s for s in values[0].split(" ") if s) if op == "trim" else values[0].replace(" ", "")
    if op == "replace":
        if values[1] == "":
            raise SemanticCompilationError("EMPTY_REPLACEMENT_PATTERN")
        return values[0].replace(values[1], values[2])
    if op == "concat":
        return "".join(values)
    if op == "contains":
        if not values[1]:
            raise SemanticCompilationError("EMPTY_CONTAINS_PATTERN")
        return values[1].lower() in values[0].lower()
    if op == "parenthesized":
        start = values[0].find("(")
        end = values[0].find(")", start + 1)
        if start < 0 or end < 0:
            raise SemanticCompilationError("DELIMITER_NOT_FOUND")
        return values[0][start + 1:end]
    if op == "between":
        source, delimiter, ordinal, end_delimiter = values
        if not delimiter or not end_delimiter or type(ordinal) is not int or not 1 <= ordinal <= 20:
            raise SemanticCompilationError("INVALID_DELIMITER_ARGUMENT")
        start = -1
        for _ in range(ordinal):
            start = source.find(delimiter, start + 1)
            if start < 0:
                raise SemanticCompilationError("DELIMITER_NOT_FOUND")
        start += len(delimiter)
        end = source.find(end_delimiter, start)
        if end < 0:
            raise SemanticCompilationError("DELIMITER_NOT_FOUND")
        return source[start:end]
    if op == "hours":
        if values[0] < 0:
            raise SemanticCompilationError("UNSPECIFIED_OVERNIGHT_INTERVAL")
        return values[0] * 24
    if op == "time_shift":
        return (values[0] + values[1] / 1440) % 1
    if op == "round":
        if type(values[1]) is not int or not -15 <= values[1] <= 15:
            raise SemanticCompilationError("INVALID_ROUND_PRECISION")
        try:
            return float(Decimal(str(values[0])).quantize(Decimal(1).scaleb(-values[1]), rounding=ROUND_HALF_UP))
        except ArithmeticError:
            raise SemanticCompilationError("UNQUALIFIED_ROUND_DOMAIN") from None
    binary = {"add": lambda a, b: a + b, "subtract": lambda a, b: a - b, "multiply": lambda a, b: a * b,
              "divide": lambda a, b: a / b, "mod": lambda a, b: a % b, "lt": lambda a, b: a < b,
              "le": lambda a, b: a <= b, "gt": lambda a, b: a > b, "ge": lambda a, b: a >= b}
    if op in binary:
        try:
            result = (values[0] - values[1]).total_seconds() / 86400 if op == 'subtract' and all(isinstance(v, datetime) for v in values) else binary[op](*values)
        except (ZeroDivisionError, ArithmeticError):
            raise SemanticCompilationError("UNQUALIFIED_ARITHMETIC_DOMAIN") from None
        if type(result) is not bool:
            try:
                finite = isfinite(result)
            except OverflowError:
                finite = False
            if not finite:
                raise SemanticCompilationError("NONFINITE_EXPRESSION_RESULT")
        return result
    raise SemanticCompilationError("UNSUPPORTED_EVALUATOR_OPERATOR:" + op)


def materialized_equal(cell, value):
    if cell.get('is_formula') or cell.get('data_type') == 'f':
        return False
    kind = cell.get('data_type')
    if isinstance(value,str):
        return kind in {'s','str','inlineStr'} and cell.get('value') == value
    if type(value) is bool:
        return kind == 'b' and _cell_value(cell,ValueType.BOOLEAN) == value
    if type(value) in (int,float):
        return kind == 'n' and _cell_value(cell,ValueType.NUMBER) == value
    return False


def _bind_expression(expression, sheet):
    if expression.reference is not None:
        ref = expression.reference
        return replace(expression, reference=replace(ref, sheet=sheet if ref.sheet in (None, '*') else ref.sheet))
    return replace(expression, arguments=tuple(_bind_expression(a, sheet) for a in expression.arguments))


def _sheet_names(target, evidences):
    inventories = {tuple(e.get("structural_metadata", {}).get("workbook_content_sheets", [])) for e in evidences
                   if e.get("structural_metadata", {}).get("workbook_content_sheets")}
    names = {e.get("sheet_name") for e in evidences if e.get("sheet_name")}
    if target.sheet == "*":
        if len(inventories) != 1:
            raise SemanticCompilationError("COMPLETE_WORKBOOK_SHEET_INVENTORY_REQUIRED")
        result = next(iter(inventories))
        if not result or not set(result) <= names:
            raise SemanticCompilationError("ALL_CONTENT_SHEETS_MUST_BE_RETRIEVED")
        return result
    if target.sheet is None:
        if len(inventories) != 1 or len(next(iter(inventories))) != 1:
            raise SemanticCompilationError("UNIQUE_CONTENT_SHEET_EVIDENCE_REQUIRED")
        return next(iter(inventories))
    matches = [name for name in names if name.casefold() == target.sheet.casefold()]
    if len(matches) != 1:
        raise SemanticCompilationError("UNIQUE_NAMED_SHEET_EVIDENCE_REQUIRED")
    return tuple(matches)


def _sheet_cells(sheet, evidences):
    cells = {}
    complete = []
    for e in evidences:
        if e.get("sheet_name") != sheet:
            continue
        if not isinstance(e.get("block_id"), str) or not e["block_id"]:
            raise SemanticCompilationError("STABLE_EVIDENCE_ID_REQUIRED")
        if e.get("structural_metadata", {}).get("source_block_complete") is True and e.get("block_type") in {"sheet_overview", "used_range", "column_block"}:
            try:
                complete.append(range_boundaries(e["range"]))
            except (KeyError, ValueError, TypeError):
                raise SemanticCompilationError("INVALID_COMPLETE_EVIDENCE_RANGE") from None
        for cell in e.get("cells", []):
            if not isinstance(cell, dict) or not cell.get("coordinate") or "value" not in cell:
                continue
            coordinate = cell["coordinate"].replace("$", "").upper()
            if coordinate in cells and any(cells[coordinate].get(k) != cell.get(k) for k in ("value", "data_type", "is_formula")):
                raise SemanticCompilationError("CONFLICTING_SOURCE_EVIDENCE")
            cells[coordinate] = cell
    return cells, complete


def _rows(goal, cells, complete, limit=10000):
    if goal.target.is_cell:
        return [coordinate_to_tuple(goal.target.address)[0]]
    refs = goal.references()
    columns = [r for r in refs if not r.is_cell and not r.is_range and r.sheet == goal.target.sheet]
    if not columns:
        raise SemanticCompilationError("COLUMN_TARGET_REQUIRES_SOURCE_DOMAIN")
    for ref in columns:
        col = coordinate_to_tuple(ref.address + "1")[1]
        if not any(left <= col <= right for left, top, right, bottom in complete):
            raise SemanticCompilationError("COMPLETE_SOURCE_DOMAIN_REQUIRED:" + ref.address)
    starts = {r.start_row for r in (*columns, goal.target) if r.start_row is not None}
    ends = {r.end_row for r in (*columns, goal.target) if r.end_row is not None}
    if len(starts) > 1 or len(ends) > 1:
        raise SemanticCompilationError("INCONSISTENT_ROW_DOMAINS")
    first, last = next(iter(starts), 1), next(iter(ends), None)
    rows = sorted({coordinate_to_tuple(k)[0] for k, c in cells.items() if c.get("value") not in (None, "None", "")
                   and re.sub(r"\d", "", k) in {r.address for r in columns}
                   and coordinate_to_tuple(k)[0] >= first and (last is None or coordinate_to_tuple(k)[0] <= last)})
    if not rows:
        raise SemanticCompilationError("EMPTY_SOURCE_DOMAIN")
    if last is not None or starts:
        if (last or rows[-1]) - first + 1 > limit:
            raise SemanticCompilationError("SOURCE_DOMAIN_LIMIT_EXCEEDED")
        rows = list(range(first, (last or rows[-1]) + 1))
    for ref in columns:
        col = coordinate_to_tuple(ref.address + "1")[1]
        if not any(left <= col <= right and top <= rows[0] and bottom >= (last or rows[-1]) for left, top, right, bottom in complete):
            raise SemanticCompilationError("SOURCE_EXTENT_NOT_PROVED:" + ref.address)
    if rows[0] == 1 and not starts and any(r.value_type in {ValueType.DATETIME, ValueType.TIME, ValueType.TEMPORAL} for r in columns):
        headers = [cells.get(r.address + "1", {}) for r in columns]
        if all(c.get("data_type") in {"s", "str", "inlineStr"} and re.search(r"\b(?:date|time|timestamp|in|out|start|end|finish|std|proposal|converted|active)\b", str(c.get("value", "")), re.I) for c in headers):
            rows = rows[1:]
    if not rows or len(rows) > limit:
        raise SemanticCompilationError("SOURCE_DOMAIN_LIMIT_EXCEEDED")
    return rows


def compile_program(program, evidences):
    if not isinstance(program, SemanticProgram):
        raise IRValidationError("TYPED_PROGRAM_REQUIRED")
    oracle_free(evidences)
    steps, traces, virtual, written, preserved = [], [], {}, [], []
    from .semantic_resolution import ReferenceResolver
    resolver = ReferenceResolver(evidences, virtual)
    dependencies = {}
    for goal in program.goals:
        dependencies[goal.identifier] = set(goal.depends_on)
        for producer in goal.depends_on:
            dependencies[goal.identifier].update(dependencies[producer])
    from .semantic_context import bind_goal, sheets_for_goal, specialize_expression
    for goal_index, original in enumerate(program.goals):
        if not executor_supports(original.operation.value):
            raise SemanticCompilationError("EXECUTOR_OPERATION_UNAVAILABLE")
        for sheet in sheets_for_goal(original, evidences):
            goal = bind_goal(original, sheet, resolver)
            if sheet not in virtual:
                virtual[sheet] = _sheet_cells(sheet, evidences)
            cells, complete = virtual[sheet]
            if goal.operation not in {WriteOperation.VALUE, WriteOperation.FORMULA}:
                if any(producer not in dependencies[goal.identifier] for producer, _, _ in written):
                    raise SemanticCompilationError('UNDECLARED_STRUCTURAL_DEPENDENCY')
                if goal_index != len(program.goals) - 1:
                    raise SemanticCompilationError('STRUCTURAL_REBASING_NOT_QUALIFIED')
                from .semantic_structural import compile_structural
                for proof in compile_structural(goal, sheet, resolver):
                    step_id = 's' + str(len(steps) + 1)
                    steps.append(PlanStep(step_id, proof.action, (goal.identifier,), proof.evidence_block_ids,
                                          (steps[-1].step_id,) if steps else ()))
                    traces.append({'requirement_id': goal.identifier, **proof.to_dict()})
                if len(steps) > 1024:
                    raise SemanticCompilationError('ACTION_PLAN_STEP_LIMIT_EXCEEDED')
                continue
            from .semantic_batch import batch_eligible, compile_batch
            batching = goal_index == len(program.goals) - 1 and batch_eligible(goal)
            rows = _rows(goal, cells, complete, 100000 if batching else 10000)
            if batching:
                domain = {goal.target.address + str(row) for row in rows}
                if any(producer not in dependencies[goal.identifier] and old_sheet == sheet and old_target in domain
                       for producer, old_sheet, old_target in written):
                    raise SemanticCompilationError('UNDECLARED_DATA_DEPENDENCY')
                proofs = compile_batch(goal, rows, sheet, resolver)
                if proofs is not None:
                    for proof in proofs:
                        step_id = 's' + str(len(steps) + 1)
                        steps.append(PlanStep(step_id, proof.action, (goal.identifier,), proof.evidence_block_ids,
                                              (steps[-1].step_id,) if steps else ()))
                        traces.append({'requirement_id': goal.identifier, **proof.to_dict()})
                    if len(steps) > 1024:
                        raise SemanticCompilationError('ACTION_PLAN_STEP_LIMIT_EXCEEDED')
                    continue
            local = []
            for row in rows:
                resolver.begin()
                if goal.predicate is not None and not evaluate_expression(goal.predicate, row, sheet, cells, resolver):
                    if any(producer not in dependencies[goal.identifier] and (old_sheet, old_target) in resolver.reads
                           for producer, old_sheet, old_target in written):
                        raise SemanticCompilationError("UNDECLARED_DATA_DEPENDENCY")
                    if goal.target.is_cell:
                        destination = _coordinate(goal.target, row)
                        resolver.cell(sheet, destination)
                        proof_ids = tuple(sorted({e["block_id"] for e in evidences
                                                  if e.get("sheet_name") in {s for s, _ in resolver.reads}}))
                        proof = PreservedRequirement(goal.identifier, sheet, destination, goal.predicate.to_dict(), proof_ids)
                        preserved.append(proof)
                        traces.append({"requirement_id": goal.identifier, "status": "preserved", **proof.to_dict()})
                    continue
                row_expression = specialize_expression(goal.expression, row, sheet, resolver)
                for ref in row_expression.references():
                    resolver.value(ref, row)
                value = evaluate_expression(row_expression, row, sheet, cells, resolver)
                sources = tuple(sorted(coordinate for source_sheet, coordinate in resolver.reads if source_sheet == sheet))
                foreign = {source_sheet for source_sheet, coordinate in resolver.reads if source_sheet != sheet}
                if len(foreign) > 1 or foreign and goal.operation != WriteOperation.FORMULA:
                    raise SemanticCompilationError('EXECUTOR_CROSS_SHEET_SOURCE_NOT_REPRESENTABLE')
                destination = _coordinate(goal.target, row)
                for producer, old_sheet, old_target in written:
                    if ((old_sheet, old_target) in resolver.reads or old_sheet == sheet and old_target == destination) and producer != goal.identifier and producer not in dependencies[goal.identifier]:
                        raise SemanticCompilationError("UNDECLARED_DATA_DEPENDENCY")
                if goal.operation == WriteOperation.VALUE and (type(value) not in (str,int,float,bool) or value == "" or isinstance(value, str) and value.startswith("=")):
                    raise SemanticCompilationError("EXECUTOR_LITERAL_NOT_MATERIALIZABLE")
                if goal.operation == WriteOperation.VALUE and destination in cells and materialized_equal(cells[destination],value):
                    continue
                if goal.operation == WriteOperation.FORMULA and destination in sources:
                    raise SemanticCompilationError("CIRCULAR_FORMULA_TARGET")
                for e in evidences:
                    if e.get("sheet_name") == sheet:
                        for merged in e.get("structural_metadata", {}).get("merged_cells", []):
                            bounds = range_boundaries(merged)
                            dr, dc = coordinate_to_tuple(destination)
                            if bounds[0] <= dc <= bounds[2] and bounds[1] <= dr <= bounds[3] and destination != get_column_letter(bounds[0]) + str(bounds[1]):
                                raise SemanticCompilationError("MERGED_TARGET_INTERIOR")
                intent = {"parse_status": "parsed", "operation": goal.operation.value, "target_sheet": sheet,
                          "destination": destination, "source_cells": list(sources)}
                if foreign:
                    source_sheet = next(iter(foreign))
                    intent['source_ranges_sheet'] = source_sheet
                    intent['source_ranges'] = [coordinate + ':' + coordinate for s, coordinate in sorted(resolver.reads) if s == source_sheet]
                if goal.operation == WriteOperation.VALUE:
                    intent["literal_value"] = value
                else:
                    intent["formula"] = "=" + formula_expression(row_expression, row, sheet)
                proof = ground_action(intent, evidences)
                if proof.status != "grounded":
                    raise SemanticCompilationError("SOURCE_REFERENCE_GROUNDING_FAILED:" + ",".join(proof.missing_requirements))
                local.append((intent, proof, value, destination, row_expression))
            if not local:
                if any(p.requirement_id == goal.identifier for p in preserved):
                    continue
                raise SemanticCompilationError("EMPTY_OPERATION_SCOPE_NOT_REPRESENTABLE")
            for intent, proof, value, destination, row_expression in local:
                step_id = "s" + str(len(steps) + 1)
                steps.append(PlanStep(step_id, proof.action, (goal.identifier,), proof.evidence_block_ids,
                                      (steps[-1].step_id,) if steps else ()))
                traces.append({"requirement_id": goal.identifier, "resolution": {"semantic_ir": "semantic_ir_v1",
                               "depends_on": list(goal.depends_on)}, **proof.to_dict()})
                is_formula = goal.operation == WriteOperation.FORMULA
                if is_formula:
                    resolver.planned_formulas[sheet, destination] = row_expression
                else:
                    resolver.planned_formulas.pop((sheet, destination), None)
                cells[destination] = {"coordinate": destination, "value": intent["formula"] if is_formula else value,
                                      "data_type": "f" if is_formula else "b" if type(value) is bool else "s" if isinstance(value, str) else "d" if isinstance(value, (datetime, time)) else "n",
                                      "is_formula": is_formula}
                written.append((goal.identifier, sheet, destination))
            if len(steps) > 1024:
                raise SemanticCompilationError("ACTION_PLAN_STEP_LIMIT_EXCEEDED")
    for _, sheet, coordinate in written:
        cell = virtual[sheet][0][coordinate]
        if cell.get("is_formula"):
            resolver.begin()
            resolver.value(Reference(coordinate, ValueType.SCALAR, sheet), coordinate_to_tuple(coordinate)[0])
    return ActionPlan(program.instruction_sha256, tuple(g.identifier for g in program.goals), tuple(steps), tuple(preserved)), traces


def compile_native_program(task, evidences):
    try:
        program = SemanticProgram.from_dict(task["semantic_ir"])
        program.bind_instruction(task["instruction"])
        from .semantic_parser import parse_semantic_program
        if parse_semantic_program(task["instruction"]) != program:
            raise IRValidationError("IR_DIFFERS_FROM_COMPLETE_INSTRUCTION")
        if task["instruction_sha256"] != program.instruction_sha256 or task["requirements"] != native_requirements(program):
            raise IRValidationError("PARTIAL_OR_FORGED_NATIVE_REQUIREMENTS")
        plan, traces = compile_program(program, evidences)
    except (IRValidationError, KeyError, TypeError, ValueError) as error:
        status = "invalid_intent" if isinstance(error, (IRValidationError, KeyError, TypeError)) else "insufficient_context"
        return {"status": status, "action_dsl": None, "change_plan": None,
                "grounding": {"status": status, "missing_requirements": [str(error)], "evidence_block_ids": [], "steps": []}}
    payload = plan.to_dict()
    return {"status": "grounded", "action_dsl": payload, "change_plan": plan.to_change_plan(),
            "grounding": {"status": "grounded", "action": payload, "steps": traces, "missing_requirements": [],
                          "evidence_block_ids": sorted({b for s in (*plan.steps, *plan.preserved_requirements) for b in s.evidence_block_ids})}}


def native_requirements(program):
    return [{"id": g.identifier, "kind": "typed_expression", "type": "ESTRUTURAL" if g.operation not in {WriteOperation.VALUE, WriteOperation.FORMULA} else "TEXTUAL" if g.expression.value_type == ValueType.TEXT else "NUMERICA",
             "span": list(g.span), "depends_on": list(g.depends_on), "ir_goal": g.to_dict()} for g in program.goals]
