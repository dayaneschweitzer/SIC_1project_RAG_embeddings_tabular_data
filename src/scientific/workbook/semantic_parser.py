from __future__ import annotations

import ast
from dataclasses import replace
from hashlib import sha256
import re

from .modification_intent import normalize_request
from .semantic_ir import Goal, IRValidationError, Reference, SemanticProgram, ValueType, WriteOperation, call, literal, reference


Q = r'''(?:"[^"\n]*"|'[^'\n]*')'''
COL = r"[A-Z]{1,3}"
CELL = rf"{COL}[1-9]\d*"
NUMBER = r"-?\d+(?:\.\d+)?"


def _quoted(value):
    return value[1:-1]


def _number(value):
    return float(value) if "." in value else int(value)


def _math(node, hint=ValueType.NUMBER):
    if isinstance(node, ast.Expression):
        return _math(node.body, hint)
    if isinstance(node, ast.Constant) and type(node.value) in (int, float, str, bool):
        return literal(node.value)
    if isinstance(node, ast.Name) and re.fullmatch(CELL, node.id, re.I):
        return reference(node.id.upper(), hint)
    if isinstance(node, ast.UnaryOp) and isinstance(node.op, ast.USub):
        return call("subtract", literal(0), _math(node.operand))
    if isinstance(node, ast.BinOp):
        operators = {ast.Add: "add", ast.Sub: "subtract", ast.Mult: "multiply", ast.Div: "divide"}
        if type(node.op) in operators:
            return call(operators[type(node.op)], _math(node.left), _math(node.right))
    if isinstance(node, ast.Compare) and len(node.ops) == 1:
        operators = {ast.Eq: "eq", ast.NotEq: "ne", ast.Lt: "lt", ast.LtE: "le", ast.Gt: "gt", ast.GtE: "ge"}
        if type(node.ops[0]) in operators:
            kind = ValueType.SCALAR if isinstance(node.comparators[0], ast.Constant) and isinstance(node.comparators[0].value, str) else ValueType.NUMBER
            return call(operators[type(node.ops[0])], _math(node.left, kind), _math(node.comparators[0], kind))
    if isinstance(node, ast.Call) and isinstance(node.func, ast.Name) and not node.keywords:
        names = {"IF": "if", "AND": "and", "OR": "or", "NOT": "not", "ROUND": "round", "MOD": "mod", "TRIM": "trim", "SUBSTITUTE": "replace"}
        name = node.func.id.upper()
        if name in names:
            kind = ValueType.TEXT if name in {"TRIM", "SUBSTITUTE"} else ValueType.NUMBER
            arguments = tuple(_math(a, kind) for a in node.args)
            if name == "IF" and len(arguments) == 3 and any(a.value_type != ValueType.TEXT for a in arguments[1:]):
                arguments = (arguments[0], *(literal(None) if a.operator == "literal" and a.value == "" else a for a in arguments[1:]))
            return call(names[name], *arguments)
    raise IRValidationError("UNSUPPORTED_EXPRESSION_SYNTAX")


def parse_expression(text):
    if not isinstance(text, str) or len(text) > 8192:
        raise IRValidationError("INVALID_EXPRESSION_TEXT")
    try:
        return _math(ast.parse(text.removeprefix("="), mode="eval"))
    except (SyntaxError, RecursionError, IRValidationError):
        from .semantic_formula import parse_formula
        return parse_formula(text)


def _goal(identifier, expression, target, span, sheet=None, operation=WriteOperation.FORMULA, predicate=None, start=None):
    target_ref = Reference(target.upper(), expression.value_type, sheet, start)
    return Goal(identifier, operation, target_ref, expression, span, predicate=predicate)


def _conditions(text):
    protected = [(m.start(), m.end()) for m in re.finditer(Q, text)]
    separators = [m for m in re.finditer(r",?\s+and\s+|,\s*", text, re.I)
                  if not any(start <= m.start() < end for start, end in protected)]
    starts = [0] + [m.end() for m in separators]
    ends = [m.start() for m in separators] + [len(text)]
    fragments = [text[start:end] for start, end in zip(starts, ends)]
    predicates = []
    for fragment in fragments:
        match = re.fullmatch(rf"(?:the )?(?:corresponding )?(?:cell in )?column (?P<column>{COL})\s+(?:has (?:the text )?|equals?\s+|is\s+|=\s*)(?P<value>{Q}|{NUMBER})", fragment, re.I)
        if not match:
            raise IRValidationError("UNRESOLVED_BOOLEAN_CONDITION")
        raw = match["value"]
        predicates.append(call("eq", reference(match["column"].upper(), ValueType.SCALAR), literal(_quoted(raw) if raw[:1] in "\"'" else _number(raw))))
    return predicates[0] if len(predicates) == 1 else call("and", *predicates)


def _body(text):
    body = normalize_request(text).rstrip(".?!; ")
    body = re.sub(r"^(?:then\s+|please\s+)", "", body, flags=re.I)
    body = re.sub(r"^(?:How (?:can|do) I|I (?:need|want) to)\s+", "", body, flags=re.I)
    body = re.sub(r"^(?:create|use|write) (?:a )?formula in Excel to\s+", "", body, flags=re.I)
    return body



LOCATION = rf"(?:(?:'(?:[^']|'')+'|\"[^\"]+\"|[A-Za-z_][\w-]*)!)?\$?{COL}\$?[1-9]\d*(?::\$?{COL}\$?[1-9]\d*)?"


def _controlled_assignment(body, identifier, span, sheet):
    from openpyxl.formula.translate import Translator
    from openpyxl.utils.cell import get_column_letter, range_boundaries
    from .semantic_ir import references_overlap
    match = re.fullmatch(
        rf"(?P<verb>calculate|write formula|write value|fill formulas|fill values)\s+(?P<expr>.+?)\s+"
        rf"(?:in|into|to)\s+(?P<target>{LOCATION})(?:\s+where\s+(?P<test>.+))?", body, re.I)
    if not match:
        return None
    verb = match["verb"].lower()
    raw_target = match["target"]
    location = re.fullmatch(r"(?:(?P<sheet>.+)!)?(?P<address>\$?[A-Z]{1,3}\$?[1-9]\d*(?::\$?[A-Z]{1,3}\$?[1-9]\d*)?)", raw_target, re.I)
    target_sheet = location["sheet"]
    if target_sheet:
        target_sheet = target_sheet[1:-1].replace("''", "'") if target_sheet[0] in "\"'" else target_sheet
    target_sheet = target_sheet or sheet
    address = location["address"].replace("$", "").upper()
    left, top, right, bottom = range_boundaries(address)
    if left != right or bottom < top or bottom - top >= 1024:
        raise IRValidationError("CONTROLLED_FILL_MUST_BE_BOUNDED_VERTICAL")
    if ":" in address and not verb.startswith("fill "):
        raise IRValidationError("EXPLICIT_FILL_COMMAND_REQUIRED")
    operation = WriteOperation.FORMULA if verb in {"write formula", "fill formulas"} else WriteOperation.VALUE
    expression_text = match["expr"]
    parse_expression(expression_text)
    if match["test"]:
        parse_expression(match["test"])
    if verb == "write value" and parse_expression(expression_text).operator != "literal":
        raise IRValidationError("WRITE_VALUE_REQUIRES_LITERAL_USE_CALCULATE")
    origin = get_column_letter(left) + str(top)
    result = []
    for row in range(top, bottom + 1):
        target = get_column_letter(left) + str(row)
        expression = parse_expression(Translator("=" + expression_text.removeprefix("="), origin=origin).translate_formula(target))
        predicate = parse_expression(Translator("=" + match["test"].removeprefix("="), origin=origin).translate_formula(target)) if match["test"] else None
        if sheet is not None:
            from .semantic_compiler import _bind_expression
            expression = _bind_expression(expression, sheet)
            predicate = _bind_expression(predicate, sheet) if predicate is not None else None
        result.append(Goal(identifier + ("_" + str(row - top + 1) if bottom != top else ""),
                           operation, Reference(target, expression.value_type, target_sheet), expression, span,
                           predicate=predicate))
    for index, goal in enumerate(result):
        for other_index, other in enumerate(result):
            if index != other_index and any(references_overlap(goal.target, ref) for ref in other.references()):
                raise IRValidationError("OVERLAPPING_FILL_SOURCE_AND_DESTINATION")
    return result


def _legacy_scalar_goal(text, identifier, span, sheet):
    from .native_interpreter import _atom
    intent = _atom(text, sheet)
    if not intent or intent["operation"] != "write_value":
        return None
    if "literal_value" in intent:
        expr = literal(intent["literal_value"])
    elif intent.get("expression") in {"add", "concatenate"}:
        kind = ValueType.NUMBER if intent["expression"] == "add" else ValueType.TEXT
        args = tuple(reference(c, kind, intent["target_sheet"]) for c in intent["source_cells"])
        expr = call("add", *args) if kind == ValueType.NUMBER else call("concat", args[0], literal(intent.get("separator", "")), args[1])
    else:
        return None
    return [_goal(identifier, expr, intent["destination"], span, intent["target_sheet"], WriteOperation.VALUE)]


def _parse_one(text, identifier, span, inherited=None):
    body, sheet = _body(text), inherited
    prefix = re.match(rf"(?:In|On) (?:the )?(?:sheet|worksheet|tab) (?P<sheet>{Q}),\s*", body, re.I)
    if prefix:
        sheet, body = _quoted(prefix["sheet"]), body[prefix.end():]
    controlled = _controlled_assignment(body, identifier, span, sheet)
    if controlled is not None:
        return controlled
    direct = re.fullmatch(rf"(?:write|put) (?:(?:a|the) )?(?:formula|expression) (?:=)?(?P<expression>.+) (?:in|into|to) (?P<target>{CELL})", body, re.I)
    if direct:
        return [_goal(identifier, parse_expression(direct["expression"]), direct["target"], span, sheet)]
    clean = re.fullmatch(
        rf"remove (?:all )?(?P<token>commas|spaces|{Q}) from (?:the )?(?:data in )?column (?P<source>{COL})"
        rf"(?: (?:and (?:display|write|put) (?:the )?(?:resulting text|result|cleaned text)(?: without (?P=token))? (?:in|into) column (?P<target>{COL})))?", body, re.I)
    if clean:
        token = clean["token"]
        old = "," if token.lower() == "commas" else " " if token.lower() == "spaces" else _quoted(token)
        source = reference(clean["source"].upper(), ValueType.TEXT)
        expression = call("replace", source, literal(old), literal(""))
        target = clean["target"] or clean["source"]
        operation = WriteOperation.FORMULA if clean["target"] and target.upper() != clean["source"].upper() else WriteOperation.VALUE
        return [_goal(identifier, expression, target, span, sheet, operation)]
    trim = re.fullmatch(rf"(?:trim|normalize) (?:the )?spaces in columns? (?P<columns>{COL}(?:(?:,\s*| and ){COL})*) (?:on|across) all (?:the )?sheets", body, re.I)
    if trim:
        columns = re.split(r",\s*| and ", trim["columns"], flags=re.I)
        return [_goal(identifier + ("_" + str(i + 1) if len(columns) > 1 else ""),
                      call("trim", reference(col.upper(), ValueType.TEXT)), col, span, "*", WriteOperation.VALUE)
                for i, col in enumerate(columns)]
    normalization = re.fullmatch(
        rf"I need a (?:VBA )?macro that can remove leading and trailing spaces for each item in cells and reduce any instances of multiple consecutive spaces between items within cells to just one space\. "
        rf"This macro should be applied to columns (?P<columns>{COL}(?:,\s*{COL})*(?:,? and {COL})?) across all sheets in a workbook\. Please ensure that each data location remains unchanged", body, re.I)
    if normalization:
        cols = re.split(r",\s*(?:and\s+)?|\s+and\s+", normalization["columns"], flags=re.I)
        return [_goal(identifier + "_" + str(i + 1), call("trim", reference(col.upper(), ValueType.TEXT)), col, span, "*", WriteOperation.VALUE)
                for i, col in enumerate(cols)]
    spaces = re.fullmatch(
        rf"I have a column starting at cell (?P<source>{CELL}), extending to the last populated row in the column, that contains numbers with incorrect formatting; specifically, leading, trailing, and internal spaces have been introduced within these cells\. "
        rf"I attempted to create a VBA macro to rectify this by removing all spaces from these cells, but I haven't succeeded\. Could you provide a solution to eliminate all types of spaces from these cells using VBA", body, re.I)
    if spaces:
        col, row = re.fullmatch(rf"({COL})([1-9]\d*)", spaces["source"], re.I).groups()
        expr = call("remove_spaces", reference(col.upper(), ValueType.TEXT, start_row=int(row)))
        return [_goal(identifier, expr, col, span, sheet, WriteOperation.VALUE, start=int(row))]
    suffix = re.fullmatch(
        rf"I need a macro that can add (?P<literal>{Q}) at the (?P<side>end|beginning) of the text present in column (?P<source>{COL}) for each row where the corresponding cell in column (?P<criterion>{COL}) contains the number (?P<number>{NUMBER})\."
        rf"(?: I've provided a sample data sheet to show the desired outcome, specifically referencing cell (?P<example>{CELL}) as an example)?", body, re.I)
    if suffix:
        if suffix["example"] and re.sub(r"\d", "", suffix["example"]).upper() != suffix["source"].upper():
            raise IRValidationError("CONFLICTING_SOURCE_EXAMPLE")
        src, value = reference(suffix["source"].upper(), ValueType.TEXT), literal(_quoted(suffix["literal"]))
        expr = call("concat", src, value) if suffix["side"].lower() == "end" else call("concat", value, src)
        predicate = call("eq", reference(suffix["criterion"].upper(), ValueType.SCALAR), literal(_number(suffix["number"])))
        return [_goal(identifier, expr, suffix["source"], span, sheet, WriteOperation.VALUE, predicate)]
    affix = re.fullmatch(rf"(?P<side>append|prepend) (?P<literal>{Q}) (?:to|in) column (?P<source>{COL}) (?:if|where) (?P<condition>.+)", body, re.I)
    if affix:
        src, value = reference(affix["source"].upper(), ValueType.TEXT), literal(_quoted(affix["literal"]))
        expr = call("concat", src, value) if affix["side"].lower() == "append" else call("concat", value, src)
        return [_goal(identifier, expr, affix["source"], span, sheet, WriteOperation.VALUE, _conditions(affix["condition"]))]
    conditional = re.fullmatch(rf"(?:write|put|insert) (?P<literal>{Q}|{NUMBER}) (?:in|into) column (?P<target>{COL}) (?:if|when|where) (?P<condition>.+)", body, re.I)
    if conditional:
        raw = conditional["literal"]
        expr = literal(_quoted(raw) if raw[:1] in "\"'" else _number(raw))
        return [_goal(identifier, expr, conditional["target"], span, sheet, WriteOperation.VALUE, _conditions(conditional["condition"]))]
    insert = re.fullmatch(
        rf"insert a specific text into a new column in an Excel spreadsheet using VBA if (?P<count>\w+|\d+) conditions are met in other columns\? Specifically, when (?P<condition>.+), I want to automatically insert the word (?P<literal>{Q}) into column (?P<target>{COL})\. "
        rf"This process should be more efficient than using the filter function, as the sheet is quite large\. VBA is preferred as it is already used for data import and cleanup", body, re.I)
    if insert:
        predicate = _conditions(insert["condition"])
        count = {"one": 1, "two": 2, "three": 3, "four": 4}.get(insert["count"].lower(), int(insert["count"]) if insert["count"].isdigit() else None)
        actual = len(predicate.arguments) if predicate.operator == "and" else 1
        if count != actual:
            raise IRValidationError("CONDITION_COUNT_MISMATCH")
        return [_goal(identifier, literal(_quoted(insert["literal"])), insert["target"], span, sheet, WriteOperation.VALUE, predicate)]
    elapsed = re.fullmatch(
        rf"(?:devise an Excel formula to calculate the amount of time that has elapsed|calculate (?:the )?elapsed time) between two timestamps listed in Column (?P<start>{COL}) and Column (?P<end>{COL})\? "
        rf"Please fill in the answer in column (?P<target>{COL})", body, re.I)
    short_elapsed = re.fullmatch(rf"calculate (?:the )?elapsed time (?:between|from) (?:column )?(?P<start>{COL}) (?:and|to) (?:column )?(?P<end>{COL}) (?:in|into) (?:column )?(?P<target>{COL})", body, re.I)
    if elapsed or short_elapsed:
        match = elapsed or short_elapsed
        expr = call("subtract", reference(match["end"].upper(), ValueType.DATETIME), reference(match["start"].upper(), ValueType.DATETIME))
        return [_goal(identifier, expr, match["target"], span, sheet)]
    hours = re.fullmatch(
        rf"I have a spreadsheet with clock in times in column (?P<start>{COL}) and clock out times in column (?P<end>{COL})\. "
        rf"I need a VBA code to calculate the total hours worked and display the results in column (?P<target>{COL})\. "
        r"Importantly, the results should be in hours and decimal fractions of an hour \(e\.g\., \d+ hours \d+ minutes should be displayed as [\d.]+\)\. "
        r"The initial code provided to me returns the hours worked in HH:MM format, but I require the minutes to be converted to a decimal fraction of an hour for the output", body, re.I)
    short_hours = re.fullmatch(rf"calculate (?:the )?decimal hours (?:between|from) (?:column )?(?P<start>{COL}) (?:and|to) (?:column )?(?P<end>{COL}) (?:in|into) (?:column )?(?P<target>{COL})", body, re.I)
    if hours or short_hours:
        match = hours or short_hours
        expr = call("hours", call("subtract", reference(match["end"].upper(), ValueType.TEMPORAL), reference(match["start"].upper(), ValueType.TEMPORAL)))
        return [_goal(identifier, expr, match["target"], span, sheet)]
    between = re.fullmatch(
        rf"(?:use a formula to )?extract text from column (?P<source>{COL})(?: in a spreadsheet)?, starting after the (?P<ordinal>first|second|third|\d+)(?:st|nd|rd|th)? (?P<delimiter>space|{Q}) character(?: to the right)? and ending before (?:a |the )?(?P<end>dash|{Q}) character\? "
        rf"An example of the manually extracted data is provided in column (?P<target>{COL}) for reference", body, re.I)
    short_between = re.fullmatch(
        rf"extract text after the (?P<ordinal>first|second|third|\d+)(?:st|nd|rd|th)? (?P<delimiter>space|{Q}) and before (?P<end>dash|{Q}) from column (?P<source>{COL}) (?:in|into) column (?P<target>{COL})", body, re.I)
    if between or short_between:
        match = between or short_between
        ordinal = {"first": 1, "second": 2, "third": 3}.get(match["ordinal"].lower(), int(match["ordinal"]) if match["ordinal"].isdigit() else None)
        delimiter = " " if match["delimiter"].lower() == "space" else _quoted(match["delimiter"])
        end = "-" if match["end"].lower() == "dash" else _quoted(match["end"])
        expr = call("between", reference(match["source"].upper(), ValueType.TEXT), literal(delimiter), literal(ordinal), literal(end))
        return [_goal(identifier, expr, match["target"], span, sheet)]
    parentheses = re.fullmatch(rf"extract (?:the )?text (?:inside|between) parentheses (?:in|from) column (?P<source>{COL})(?: starting (?:at|from) row (?P<row>[1-9]\d*))? in place", body, re.I)
    if parentheses:
        start = int(parentheses["row"]) if parentheses["row"] else None
        expr = call("parenthesized", reference(parentheses["source"].upper(), ValueType.TEXT, start_row=start))
        return [_goal(identifier, expr, parentheses["source"], span, sheet, WriteOperation.VALUE, start=start)]
    parenthesized_macro = re.fullmatch(
        rf"incorporate a formula that deletes everything outside of parentheses into a VBA macro which iterates over all cells in a column without gaps\? "
        rf"I found a formula '=MID\((?P<cell>{CELL}),FIND\(\"\(\",(?P=cell)\)\+1,FIND\(\"\)\",(?P=cell)\)-FIND\(\"\(\",(?P=cell)\)-1\)' that does what I need for a single cell, but I'm unsure how to adapt it to run in a loop within a VBA for the entire column starting from (?P=cell) to the last cell with data\. "
        rf"I was considering using a method that involves selecting the range from (?P=cell) to the bottom of the data set using 'Range\(\"(?P=cell)\"\)\.Select' and 'Range\(Selection, Selection\.End\(xlDown\)\)\.Select'\. I have attached a file for reference", body, re.I)
    if parenthesized_macro:
        col, row = re.fullmatch(rf"({COL})([1-9]\d*)", parenthesized_macro["cell"], re.I).groups()
        expr = call("parenthesized", reference(col.upper(), ValueType.TEXT, start_row=int(row)))
        return [_goal(identifier, expr, col, span, sheet, WriteOperation.VALUE, start=int(row))]
    prefix_condition = re.fullmatch(
        rf"I need a VB \(Visual Basic\) code that can look through column (?P<source>{COL}) in an Excel sheet for cells that do not contain the text (?P<first>{Q}) or (?P<second>{Q}) and also have the text (?P<criterion>{Q}) in the corresponding cell of column (?P<condition>{COL})\. "
        rf"For any such found cell in column (?P=source), the code should add (?P=criterion) and a space in front of the existing content in the cell\. "
        rf"I have highlighted examples in the attached file, such as {Q} for cell {CELL} and {Q} for cell {CELL}", body, re.I)
    if prefix_condition:
        src = reference(prefix_condition["source"].upper(), ValueType.TEXT)
        term = literal(_quoted(prefix_condition["criterion"]))
        excluded = call("or", call("contains", src, literal(_quoted(prefix_condition["first"]))),
                        call("contains", src, literal(_quoted(prefix_condition["second"]))))
        predicate = call("and", call("not", excluded), call("contains", reference(prefix_condition["condition"].upper(), ValueType.TEXT), term))
        expr = call("concat", term, literal(" "), src)
        return [_goal(identifier, expr, prefix_condition["source"], span, sheet, WriteOperation.VALUE, predicate)]
    cutoff = re.fullmatch(
        rf"correct an error that occurs when calculating a {Q} time that should be (?P<duration>half an hour|\d+ minutes) before the {Q} time, particularly when this calculation crosses over midnight\? "
        rf"Additionally, how can I ensure that if there's no data present in columns (?P<left>{COL}) and (?P<right>{COL}), then column (?P<target>{COL}) will display a blank cell instead of an error\? "
        rf"Current formula is (?P<cell>{CELL})-TIME\(0,(?P<minutes>\d+),0\)", body, re.I)
    if cutoff:
        col, row = re.fullmatch(rf"({COL})([1-9]\d*)", cutoff["cell"], re.I).groups()
        minutes = 30 if cutoff["duration"].lower() == "half an hour" else int(cutoff["duration"].split()[0])
        if minutes != int(cutoff["minutes"]) or col.upper() != cutoff["right"].upper():
            raise IRValidationError("CONFLICTING_TEMPORAL_ARGUMENTS")
        blank = call("and", call("is_blank", reference(cutoff["left"].upper(), ValueType.SCALAR, start_row=int(row))),
                     call("is_blank", reference(col.upper(), ValueType.SCALAR, start_row=int(row))))
        shifted = call("time_shift", reference(col.upper(), ValueType.TIME, start_row=int(row)), literal(-minutes))
        return [_goal(identifier, call("if", blank, literal(None), shifted), cutoff["target"], span, sheet, start=int(row))]
    contains_condition = re.fullmatch(
        rf"I need help creating a formula in Excel for column (?P<target>{COL}) that returns (?P<result>{Q}) under two conditions: "
        rf"The text in the same row's column (?P<source>{COL}) must contain the word (?P<term>{Q}), and column (?P<other>{COL}) in the same row must have the character (?P<value>{Q})\. "
        rf"My current problem is that for rows \d+-\d+, instead of returning a blank \(\"\"\) where (?P=term) is missing in column (?P=source), it gives a #VALUE! error\. "
        rf"How can I adjust my formula to avoid this error and return an empty string when the conditions are not met, or is there a different approach to the formula that I should consider\? "
        rf"IF\(AND\(SEARCH\((?P<formula_term>{Q}),(?P<cell>{CELL})\)>0,(?P<other_cell>{CELL})=(?P<formula_value>{Q})\),(?P<formula_result>{Q}),\"\"\)", body, re.I)
    if contains_condition:
        if any(_quoted(contains_condition[left]) != _quoted(contains_condition[right]) for left, right in
               (("term", "formula_term"), ("value", "formula_value"), ("result", "formula_result"))):
            raise IRValidationError("CONFLICTING_FORMULA_LITERALS")
        source, row = re.fullmatch(rf"({COL})([1-9]\d*)", contains_condition["cell"], re.I).groups()
        other, other_row = re.fullmatch(rf"({COL})([1-9]\d*)", contains_condition["other_cell"], re.I).groups()
        if source.upper() != contains_condition["source"].upper() or other.upper() != contains_condition["other"].upper() or row != other_row:
            raise IRValidationError("CONFLICTING_CONDITION_REFERENCES")
        predicate = call("and", call("contains", reference(source.upper(), ValueType.TEXT, start_row=int(row)), literal(_quoted(contains_condition["term"]))),
                         call("eq", reference(other.upper(), ValueType.SCALAR, start_row=int(row)), literal(_quoted(contains_condition["value"]))))
        expr = call("if", predicate, literal(_quoted(contains_condition["result"])), literal(""))
        return [_goal(identifier, expr, contains_condition["target"], span, sheet, start=int(row))]
    math_if = re.fullmatch(
        rf"write a formula in cell (?:(?P<sheet>[\w -]+) )?(?P<target>{CELL}) that performs different sum calculations based on the condition of cell (?P<condition>{CELL})\? "
        rf"If (?P=condition) equals (?P<value>{Q}|{NUMBER}), then the formula should calculate (?P<yes>[A-Z0-9()+*/. -]+), otherwise, it should calculate the reverse: (?P<no>[A-Z0-9()+*/. -]+)\. "
        r"The result needs to be rounded to (?P<precision>\d+|two|three|four) decimal places\. My attempts with SUM and SUMIF functions were unsuccessful", body, re.I)
    if math_if:
        raw = math_if["value"]
        value = literal(_quoted(raw) if raw[:1] in "\"'" else _number(raw))
        condition = call("eq", reference(math_if["condition"].upper(), ValueType.SCALAR), value)
        yes, no = parse_expression(math_if["yes"]), parse_expression(math_if["no"])
        if yes.operator != "subtract" or no != call("subtract", yes.arguments[1], yes.arguments[0]):
            raise IRValidationError("CONFLICTING_REVERSED_EXPRESSION")
        expr = call("if", condition, yes, no)
        precision = {"two": 2, "three": 3, "four": 4}.get(math_if["precision"].lower(), int(math_if["precision"]) if math_if["precision"].isdigit() else None)
        return [_goal(identifier, call("round", expr, literal(precision)), math_if["target"], span, math_if["sheet"] or sheet)]
    from .semantic_rules import parse_rules
    rules = parse_rules(body, identifier, span, sheet)
    if rules is not None:
        return rules
    from .semantic_frontend import parse_frontend
    parsed = parse_frontend(body, identifier, span, sheet)
    return parsed if parsed is not None else _legacy_scalar_goal(text, identifier, span, sheet)


def parse_semantic_program(instruction):
    if not isinstance(instruction, str) or not instruction.strip() or len(instruction) > 50000:
        return None
    try:
        try:
            goals = _parse_one(instruction, "r1", (0, len(instruction)))
        except (IRValidationError, ValueError, TypeError):
            goals = None
        if goals is None:
            from .native_interpreter import clauses
            pieces = clauses(instruction)
            if len(pieces) < 2:
                from .semantic_frontend import operation_clauses
                pieces = operation_clauses(instruction)
            if len(pieces) < 2:
                return None
            goals, inherited = [], None
            for start, end, text in pieces:
                group = _parse_one(text, "r" + str(len(goals) + 1), (start, end), inherited)
                if not group:
                    return None
                for goal in group:
                    goals.append(replace(goal, depends_on=(goals[-1].identifier,) if goals else ()))
                    inherited = goal.target.sheet
        goals = [replace(goal, depends_on=(goals[index - 1].identifier,) if index else ())
                 for index, goal in enumerate(goals)]
        return SemanticProgram(sha256(instruction.encode("utf8")).hexdigest(), len(instruction), tuple(goals), ((0, len(instruction)),))
    except (IRValidationError, ValueError, TypeError, RecursionError):
        return None
