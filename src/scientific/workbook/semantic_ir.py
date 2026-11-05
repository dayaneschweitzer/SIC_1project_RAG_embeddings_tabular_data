from __future__ import annotations

from dataclasses import dataclass
from enum import Enum
from hashlib import sha256
from math import isfinite
import re

from .action_dsl import ActionSource


class IRValidationError(ValueError):
    pass


class ValueType(str, Enum):
    TEXT = "text"
    NUMBER = "number"
    BOOLEAN = "boolean"
    DATETIME = "datetime"
    TIME = "time"
    DURATION = "duration"
    BLANK = "blank"
    SCALAR = "scalar"
    TEMPORAL = "temporal"
    NUMBERS = "numbers"
    TEXTS = "texts"
    SCALARS = "scalars"


SERIES_TYPES = {ValueType.NUMBER: ValueType.NUMBERS, ValueType.TEXT: ValueType.TEXTS,
                ValueType.SCALAR: ValueType.SCALARS}
ELEMENT_TYPES = {value: key for key, value in SERIES_TYPES.items()}


class WriteOperation(str, Enum):
    VALUE = "write_value"
    FORMULA = "write_formula"
    DELETE_ROW = "delete_row"
    INSERT_ROW = "insert_row"
    INSERT_COLUMN = "insert_column"
    UNMERGE = "unmerge_range"
    COPY_SHEET = "copy_sheet"
    SORT = "sort_range"


@dataclass(frozen=True)
class Reference:
    address: str
    value_type: ValueType
    sheet: str | None = None
    start_row: int | None = None
    end_row: int | None = None

    def __post_init__(self):
        if not isinstance(self.value_type, ValueType):
            raise IRValidationError("INVALID_REFERENCE_TYPE")
        symbolic = isinstance(self.address, str) and self.address.startswith('@')
        if symbolic:
            if len(self.address) > 8192 or not re.fullmatch(r'@(?:header:[^\r\n]+|domain:[A-Z]{1,3}|formula:[^\r\n]+|formula_column:[^\r\n]+|text:(?:colon|iso_time|leading_space|compact_date|ymd_dot|sap_date)|same|adjacent|row|sheet)', self.address):
                raise IRValidationError('INVALID_CONTEXTUAL_REFERENCE')
        pattern = r"[A-Z]{1,3}[1-9]\d*:[A-Z]{1,3}[1-9]\d*" if self.value_type in ELEMENT_TYPES else r"[A-Z]{1,3}(?:[1-9]\d*)?"
        if not symbolic and (not isinstance(self.address, str) or not re.fullmatch(pattern, self.address)):
            raise IRValidationError("INVALID_REFERENCE")
        if symbolic:
            pass
        elif self.is_range:
            ActionSource(range=self.address)
        else:
            ActionSource(cells=(self.address if self.is_cell else self.address + "1",))
        if self.sheet is not None and (not isinstance(self.sheet, str) or not self.sheet.strip()):
            raise IRValidationError("INVALID_SHEET_REFERENCE")
        for row in (self.start_row, self.end_row):
            if row is not None and (type(row) is not int or not 1 <= row <= 1048576):
                raise IRValidationError("INVALID_REFERENCE_EXTENT")
        if (self.is_cell or self.is_range) and (self.start_row is not None or self.end_row is not None):
            raise IRValidationError("CELL_CANNOT_DECLARE_COLUMN_EXTENT")
        if self.end_row is not None and self.start_row is not None and self.end_row < self.start_row:
            raise IRValidationError("REVERSED_REFERENCE_EXTENT")

    @property
    def is_cell(self):
        return not self.is_symbolic and not self.is_range and bool(re.search(r"\d", self.address))

    @property
    def is_symbolic(self):
        return self.address.startswith('@')

    @property
    def is_range(self):
        return self.value_type in ELEMENT_TYPES

    def to_dict(self):
        return {"address": self.address, "value_type": self.value_type.value, "sheet": self.sheet,
                "start_row": self.start_row, "end_row": self.end_row}

    @classmethod
    def from_dict(cls, value):
        if not isinstance(value, dict) or set(value) != {"address", "value_type", "sheet", "start_row", "end_row"}:
            raise IRValidationError("INVALID_REFERENCE_FIELDS")
        return cls(**{**value, "value_type": ValueType(value["value_type"])})


@dataclass(frozen=True)
class Expression:
    operator: str
    value_type: ValueType
    arguments: tuple[Expression, ...] = ()
    value: str | int | float | bool | None = None
    reference: Reference | None = None

    def __post_init__(self):
        if not isinstance(self.arguments, tuple) or len(self.arguments) > 64 or not all(isinstance(a, Expression) for a in self.arguments):
            raise IRValidationError("INVALID_EXPRESSION_ARGUMENTS")
        if not isinstance(self.value_type, ValueType):
            raise IRValidationError("INVALID_EXPRESSION_TYPE")
        if self.operator == "literal":
            actual = (ValueType.BLANK if self.value is None else ValueType.BOOLEAN if type(self.value) is bool else
                      ValueType.NUMBER if type(self.value) in (int, float) else ValueType.TEXT if isinstance(self.value, str) else None)
            if actual != self.value_type or self.arguments or self.reference is not None:
                raise IRValidationError("INVALID_LITERAL")
            if isinstance(self.value, float) and not isfinite(self.value):
                raise IRValidationError("NONFINITE_LITERAL")
            if self.value_type == ValueType.NUMBER:
                try:
                    finite = isfinite(float(self.value))
                except OverflowError:
                    finite = False
                if not finite:
                    raise IRValidationError("NUMERIC_LITERAL_OUTSIDE_EXECUTOR_RANGE")
        elif self.operator == "reference":
            if not isinstance(self.reference, Reference) or self.reference.value_type != self.value_type or self.arguments or self.value is not None:
                raise IRValidationError("INVALID_REFERENCE_EXPRESSION")
        else:
            if self.value is not None or self.reference is not None:
                raise IRValidationError("UNEXPECTED_EXPRESSION_VALUE")
            if expression_type(self.operator, self.arguments) != self.value_type:
                raise IRValidationError("EXPRESSION_TYPE_MISMATCH")
        if self.depth > 32 or self.node_count > 512:
            raise IRValidationError("EXPRESSION_LIMIT_EXCEEDED")

    @property
    def depth(self):
        return 1 + max((a.depth for a in self.arguments), default=0)

    @property
    def node_count(self):
        return 1 + sum(a.node_count for a in self.arguments)

    def references(self):
        if self.reference is not None:
            return (self.reference,)
        return tuple(dict.fromkeys(r for a in self.arguments for r in a.references()))

    def to_dict(self):
        return {"operator": self.operator, "value_type": self.value_type.value,
                "arguments": [a.to_dict() for a in self.arguments], "value": self.value,
                "reference": self.reference.to_dict() if self.reference is not None else None}

    @classmethod
    def from_dict(cls, value, depth=0):
        if depth > 32 or not isinstance(value, dict) or set(value) != {"operator", "value_type", "arguments", "value", "reference"}:
            raise IRValidationError("INVALID_EXPRESSION_FIELDS")
        if not isinstance(value["arguments"], list) or len(value["arguments"]) > 64:
            raise IRValidationError("INVALID_EXPRESSION_ARGUMENTS")
        return cls(value["operator"], ValueType(value["value_type"]),
                   tuple(cls.from_dict(a, depth + 1) for a in value["arguments"]), value["value"],
                   Reference.from_dict(value["reference"]) if value["reference"] is not None else None)


def expression_type(operator, arguments):
    types = tuple(a.value_type for a in arguments)
    text, number, boolean = ValueType.TEXT, ValueType.NUMBER, ValueType.BOOLEAN
    temporal = {ValueType.DATETIME, ValueType.TIME, ValueType.TEMPORAL, ValueType.DURATION}
    def shape(argument):
        if argument.reference is None or argument.reference.is_symbolic:
            return None
        from openpyxl.utils.cell import range_boundaries
        left, top, right, bottom = range_boundaries(argument.reference.address)
        return bottom - top + 1, right - left + 1
    if operator in {'is_number', 'is_text'} and len(types) == 1 and types[0] == ValueType.SCALAR:
        return boolean
    if operator in {'ltrim', 'after_colon', 'strip_iso_time', 'first_last_name'} and types == (text,):
        return text
    if operator == 'to_text' and types == (ValueType.SCALAR,):
        return text
    if operator == 'date_text' and types == (text, text, text):
        return text
    if operator == 'coalesce_unique' and len(types) >= 2 and set(types) == {ValueType.SCALAR}:
        return ValueType.SCALAR
    scalar_types = {ValueType.SCALAR, text, number, boolean, ValueType.BLANK, ValueType.DATETIME, ValueType.TIME}
    if operator == 'count_matches' and len(types) >= 2 and len(types) % 2 == 0 and all(types[i] == ValueType.SCALARS and types[i + 1] in scalar_types for i in range(0, len(types), 2)):
        shapes = {shape(a) for a in arguments[::2]} - {None}
        if len(shapes) > 1:
            raise IRValidationError('CRITERIA_RANGES_NOT_ALIGNED')
        return number
    if operator == 'sum_matches' and len(types) >= 3 and len(types) % 2 == 1 and types[0] == ValueType.NUMBERS and all(types[i] == ValueType.SCALARS and types[i + 1] in scalar_types for i in range(1, len(types), 2)):
        shapes = {shape(a) for a in (arguments[0], *arguments[1::2])} - {None}
        if len(shapes) > 1:
            raise IRValidationError('CRITERIA_RANGES_NOT_ALIGNED')
        return number
    if operator == 'max_product' and types == (ValueType.NUMBERS, ValueType.NUMBERS):
        if None not in (shape(arguments[0]),shape(arguments[1])) and shape(arguments[0]) != shape(arguments[1]):
            raise IRValidationError('PRODUCT_RANGES_NOT_ALIGNED')
        return number
    if operator == 'sum_month' and types == (ValueType.NUMBERS, ValueType.SCALARS, ValueType.SCALAR):
        return number
    if operator == 'matrix_sum' and types == (ValueType.NUMBERS, ValueType.SCALARS, ValueType.SCALAR, ValueType.SCALARS, ValueType.SCALAR, ValueType.SCALARS, ValueType.SCALAR):
        sizes = [shape(arguments[i]) for i in (0,1,3,5)]
        if all(s is not None for s in sizes) and (sizes[1] != sizes[2] or sizes[1][1] != 1 or sizes[3][0] != 1 or sizes[0] != (sizes[1][0],sizes[3][1])):
            raise IRValidationError('MATRIX_CRITERIA_NOT_ALIGNED')
        return number
    if operator == 'date_format' and types == (ValueType.SCALARS,):
        return text
    if operator == 'join_dates' and types == (ValueType.SCALAR, ValueType.SCALARS, ValueType.SCALARS, text, text):
        sizes = shape(arguments[1]),shape(arguments[2])
        if all(s is not None for s in sizes) and (sizes[0] != sizes[1] or sizes[0][1] != 1):
            raise IRValidationError('DATE_LOOKUP_RANGES_NOT_ALIGNED')
        return text
    if operator == 'length' and types == (text,) or operator in {'year','month','day'} and types in {(ValueType.SCALAR,), (ValueType.DATETIME,)}:
        return number
    if operator in {'left', 'right'} and types == (text, number) or operator == 'mid' and types == (text, number, number):
        return text
    if operator in {'find', 'search'} and types in {(text, text), (text, text, number)}:
        return number
    if operator in {'roundup'} and types == (number, number):
        return number
    if operator == 'date' and types == (number, number, number) or operator == 'eomonth' and types in {(ValueType.SCALAR, number), (ValueType.DATETIME, number)}:
        return ValueType.DATETIME
    if operator == 'if_error' and len(types) == 2:
        branches = set(types) - {ValueType.BLANK}
        if len(branches) == 1:
            return next(iter(branches))
    if operator == 'row_blank' and not types:
        return boolean
    if operator in {'duplicate_first', 'duplicate_last'} and types and set(types) == {ValueType.SCALAR}:
        return boolean
    if operator == 'duplicate_unmarked' and len(types) >= 3 and set(types[:-1]) == {ValueType.SCALAR} and types[-1] == text:
        return boolean
    if operator == 'urn_next' and types == (ValueType.SCALAR, ValueType.SCALAR):
        return text
    if operator == 'group_running_max_product' and types == (ValueType.SCALAR, number, number):
        return number
    if operator in {"sum", "min", "max"} and types and set(types) <= {number, ValueType.NUMBERS}:
        return number
    if operator == "count_eq" and len(types) == 2 and types[0] in ELEMENT_TYPES and types[1] in {ELEMENT_TYPES[types[0]], ValueType.SCALAR, ValueType.BLANK}:
        return number
    if operator == "lookup_exact" and len(types) in (3, 4) and types[0] in {text, number, ValueType.SCALAR} and types[1] == ValueType.SCALARS and types[2] in ELEMENT_TYPES:
        from openpyxl.utils.cell import range_boundaries
        if any(a.reference is None or not a.reference.is_range for a in arguments[1:3]):
            raise IRValidationError("EXPLICIT_LOOKUP_RANGES_REQUIRED")
        if not any(a.reference.is_symbolic for a in arguments[1:3]):
            keys, values = (range_boundaries(a.reference.address) for a in arguments[1:3])
            if keys[0] != keys[2] or values[0] != values[2] or keys[3] - keys[1] != values[3] - values[1]:
                raise IRValidationError("LOOKUP_RANGES_NOT_ALIGNED")
        result = ELEMENT_TYPES[types[2]]
        if len(types) == 3 or types[3] in {result, ValueType.BLANK}:
            return result
    if operator in {"and", "or"} and len(types) >= 2 and set(types) == {boolean}:
        return boolean
    if operator == "not" and types == (boolean,):
        return boolean
    if operator == "is_blank" and len(types) == 1:
        return boolean
    if operator in {"eq", "ne", "lt", "le", "gt", "ge"} and len(types) == 2 and types[0] == types[1]:
        if operator in {"eq", "ne"} or types[0] == number or types[0] in temporal:
            return boolean
    if operator in {"eq", "ne"} and len(types) == 2 and ValueType.SCALAR in types:
        return boolean
    if operator == "if" and len(types) == 3 and types[0] == boolean:
        branches = set(types[1:]) - {ValueType.BLANK}
        if len(branches) == 1:
            return next(iter(branches))
    if operator == "case" and len(types) >= 4 and len(types) % 2 == 0 and set(types[::2]) == {boolean}:
        branches = set(types[1::2]) - {ValueType.BLANK}
        if len(branches) == 1:
            return next(iter(branches))
    if operator in {"trim", "remove_spaces", "parenthesized"} and types == (text,):
        return text
    if operator == "replace" and types == (text, text, text):
        return text
    if operator == "concat" and len(types) >= 2 and set(types) == {text}:
        return text
    if operator == "contains" and types == (text, text):
        return boolean
    if operator == "between" and types == (text, text, number, text):
        return text
    if operator in {"add", "subtract", "multiply", "divide", "mod", "round"} and types == (number, number):
        return number
    if operator == "subtract" and len(types) == 2 and types[0] == types[1] and types[0] in {ValueType.DATETIME, ValueType.TIME, ValueType.TEMPORAL}:
        return ValueType.DURATION
    if operator == "hours" and types == (ValueType.DURATION,):
        return number
    if operator == "time_shift" and types == (ValueType.TIME, number):
        return ValueType.TIME
    raise IRValidationError("UNSUPPORTED_OR_ILL_TYPED_OPERATOR:" + str(operator))


def literal(value):
    kind = (ValueType.BLANK if value is None else ValueType.BOOLEAN if type(value) is bool else
            ValueType.NUMBER if type(value) in (int, float) else ValueType.TEXT)
    return Expression("literal", kind, value=value)


def reference(address, value_type, sheet=None, start_row=None, end_row=None):
    ref = Reference(address, value_type, sheet, start_row, end_row)
    return Expression("reference", value_type, reference=ref)


def call(operator, *arguments):
    return Expression(operator, expression_type(operator, arguments), tuple(arguments))


def references_overlap(left, right):
    if left.sheet not in (None, "*") and right.sheet not in (None, "*") and left.sheet.casefold() != right.sheet.casefold():
        return False
    if left.is_symbolic or right.is_symbolic:
        return False
    from openpyxl.utils.cell import range_boundaries
    def extent(ref):
        if ref.is_range:
            return range_boundaries(ref.address)
        if ref.is_cell:
            return range_boundaries(ref.address)
        first, _, last, _ = range_boundaries(ref.address + "1")
        return first, ref.start_row or 1, last, ref.end_row or 1048576
    a, b = extent(left), extent(right)
    return max(a[0], b[0]) <= min(a[2], b[2]) and max(a[1], b[1]) <= min(a[3], b[3])


@dataclass(frozen=True)
class Goal:
    identifier: str
    operation: WriteOperation
    target: Reference
    expression: Expression
    span: tuple[int, int]
    depends_on: tuple[str, ...] = ()
    predicate: Expression | None = None

    def __post_init__(self):
        if not isinstance(self.identifier, str) or not self.identifier or not isinstance(self.operation, WriteOperation):
            raise IRValidationError("INVALID_GOAL_OPERATION")
        if not isinstance(self.target, Reference) or not isinstance(self.expression, Expression):
            raise IRValidationError("INVALID_GOAL_TYPE")
        writing = self.operation in {WriteOperation.VALUE, WriteOperation.FORMULA}
        if not writing and self.predicate is not None:
            raise IRValidationError('STRUCTURAL_PREDICATE_MUST_BE_EXPLICIT_EXPRESSION')
        if writing and self.target.value_type != self.expression.value_type:
            raise IRValidationError("INVALID_GOAL_TYPE")
        if self.operation == WriteOperation.DELETE_ROW and self.expression.value_type != ValueType.BOOLEAN:
            raise IRValidationError('BOOLEAN_DELETION_PREDICATE_REQUIRED')
        if self.operation == WriteOperation.COPY_SHEET and (self.expression.operator != 'literal' or self.expression.value_type != ValueType.TEXT):
            raise IRValidationError('LITERAL_DESTINATION_SHEET_REQUIRED')
        if self.operation in {WriteOperation.INSERT_ROW, WriteOperation.INSERT_COLUMN, WriteOperation.UNMERGE} and (self.expression.operator != 'literal' or self.expression.value_type != ValueType.BLANK):
            raise IRValidationError('UNEXPECTED_STRUCTURAL_ARGUMENT')
        if self.operation == WriteOperation.SORT and (self.expression.operator != 'literal' or self.expression.value != 'ascending'):
            raise IRValidationError('EXECUTOR_SORT_ORDER_NOT_SUPPORTED')
        if self.operation == WriteOperation.COPY_SHEET and self.target.address != '@sheet':
            raise IRValidationError('SHEET_TARGET_REQUIRED')
        if self.operation in {WriteOperation.INSERT_ROW, WriteOperation.INSERT_COLUMN} and not self.target.is_cell:
            raise IRValidationError('POSITION_REFERENCE_REQUIRED')
        if self.operation in {WriteOperation.UNMERGE, WriteOperation.SORT} and (not self.target.is_range or self.target.is_symbolic):
            raise IRValidationError('EXPLICIT_STRUCTURAL_RANGE_REQUIRED')
        if self.target.is_range and writing:
            raise IRValidationError("SCALAR_WRITE_TARGET_REQUIRED")
        if not isinstance(self.span, tuple) or len(self.span) != 2 or any(type(v) is not int for v in self.span) or not 0 <= self.span[0] < self.span[1]:
            raise IRValidationError("INVALID_GOAL_SPAN")
        if not isinstance(self.depends_on, tuple) or len(set(self.depends_on)) != len(self.depends_on):
            raise IRValidationError("INVALID_GOAL_DEPENDENCIES")
        if self.predicate is not None and (not isinstance(self.predicate, Expression) or self.predicate.value_type != ValueType.BOOLEAN):
            raise IRValidationError("INVALID_GOAL_PREDICATE")
        if self.operation == WriteOperation.FORMULA and any(references_overlap(self.target, ref) for ref in self.expression.references()):
            raise IRValidationError("CIRCULAR_FORMULA_TARGET")

    def references(self):
        return tuple(dict.fromkeys(self.expression.references() + (() if self.predicate is None else self.predicate.references())))

    def to_dict(self):
        return {"identifier": self.identifier, "operation": self.operation.value, "target": self.target.to_dict(),
                "expression": self.expression.to_dict(), "span": list(self.span), "depends_on": list(self.depends_on),
                "predicate": self.predicate.to_dict() if self.predicate is not None else None}

    @classmethod
    def from_dict(cls, value):
        if not isinstance(value, dict) or set(value) != {"identifier", "operation", "target", "expression", "span", "depends_on", "predicate"}:
            raise IRValidationError("INVALID_GOAL_FIELDS")
        return cls(value["identifier"], WriteOperation(value["operation"]), Reference.from_dict(value["target"]),
                   Expression.from_dict(value["expression"]), tuple(value["span"]), tuple(value["depends_on"]),
                   Expression.from_dict(value["predicate"]) if value["predicate"] is not None else None)


@dataclass(frozen=True)
class SemanticProgram:
    instruction_sha256: str
    instruction_length: int
    goals: tuple[Goal, ...]
    covered_spans: tuple[tuple[int, int], ...]

    def __post_init__(self):
        if not isinstance(self.instruction_sha256, str) or not re.fullmatch(r"[a-f0-9]{64}", self.instruction_sha256):
            raise IRValidationError("INVALID_INSTRUCTION_HASH")
        if type(self.instruction_length) is not int or self.instruction_length <= 0:
            raise IRValidationError("INVALID_INSTRUCTION_LENGTH")
        if not isinstance(self.goals, tuple) or not 1 <= len(self.goals) <= 1024 or not all(isinstance(g, Goal) for g in self.goals):
            raise IRValidationError("INVALID_PROGRAM_GOALS")
        if not isinstance(self.covered_spans, tuple):
            raise IRValidationError("INVALID_COVERAGE_TYPE")
        cursor = 0
        for span in self.covered_spans:
            if not isinstance(span, tuple) or len(span) != 2 or any(type(v) is not int for v in span) or span[0] != cursor or not span[0] < span[1] <= self.instruction_length:
                raise IRValidationError("INCOMPLETE_INSTRUCTION_COVERAGE")
            cursor = span[1]
        if cursor != self.instruction_length:
            raise IRValidationError("INCOMPLETE_INSTRUCTION_COVERAGE")
        seen = set()
        previous_start = 0
        for goal in self.goals:
            if goal.identifier in seen or not set(goal.depends_on) <= seen or goal.span[1] > self.instruction_length or goal.span[0] < previous_start:
                raise IRValidationError("INVALID_PROGRAM_COMPOSITION")
            seen.add(goal.identifier)
            previous_start = goal.span[0]

    def bind_instruction(self, instruction):
        if len(instruction) != self.instruction_length or sha256(instruction.encode("utf8")).hexdigest() != self.instruction_sha256:
            raise IRValidationError("INSTRUCTION_BINDING_MISMATCH")

    def to_dict(self):
        return {"schema_version": "semantic_ir_v1", "instruction_sha256": self.instruction_sha256,
                "instruction_length": self.instruction_length, "goals": [g.to_dict() for g in self.goals],
                "covered_spans": [list(s) for s in self.covered_spans]}

    @classmethod
    def from_dict(cls, value):
        if not isinstance(value, dict) or set(value) != {"schema_version", "instruction_sha256", "instruction_length", "goals", "covered_spans"} or value["schema_version"] != "semantic_ir_v1":
            raise IRValidationError("INVALID_PROGRAM_FIELDS")
        return cls(value["instruction_sha256"], value["instruction_length"], tuple(Goal.from_dict(g) for g in value["goals"]),
                   tuple(tuple(s) for s in value["covered_spans"]))
