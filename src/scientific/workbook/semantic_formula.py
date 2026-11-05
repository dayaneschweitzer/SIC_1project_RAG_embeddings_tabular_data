from __future__ import annotations

import ast
import re

from openpyxl.formula.tokenizer import Tokenizer, TokenizerError

from .semantic_ir import IRValidationError, SERIES_TYPES, ValueType as T, call, literal, reference


def _reference(text, kind):
    match = re.fullmatch(r"(?:(?:'(?P<quoted>(?:[^']|'')+)'|(?P<sheet>[\w .-]+))!)?(?P<address>\$?[A-Z]{1,3}\$?[1-9]\d*(?::\$?[A-Z]{1,3}\$?[1-9]\d*)?)", text, re.I)
    if not match:
        raise IRValidationError("UNSUPPORTED_FORMULA_REFERENCE")
    address = match['address'].replace('$', '').upper()
    if ':' in address:
        if kind not in SERIES_TYPES:
            raise IRValidationError("UNSUPPORTED_SERIES_ELEMENT_TYPE")
        kind = SERIES_TYPES[kind]
    sheet = match['quoted'].replace("''", "'") if match['quoted'] else match['sheet']
    return reference(address, kind, sheet)


def parse_formula(text):
    if not isinstance(text, str) or len(text) > 8192:
        raise IRValidationError("INVALID_EXPRESSION_TEXT")
    refs, fragments = {}, []
    try:
        tokens = Tokenizer('=' + text.removeprefix('=')).items
        for token in tokens:
            if token.type == 'WHITE-SPACE':
                fragments.append(' ')
            elif token.type == 'OPERAND' and token.subtype == 'RANGE':
                name = 'ref_' + str(len(refs))
                refs[name] = token.value
                fragments.append(name)
            elif token.type == 'OPERAND' and token.subtype == 'TEXT':
                fragments.append(repr(token.value[1:-1].replace('""', '"')))
            elif token.type == 'OPERAND' and token.subtype == 'LOGICAL':
                fragments.append('True' if token.value.upper() == 'TRUE' else 'False')
            elif token.type == 'OPERAND' and token.subtype == 'NUMBER':
                fragments.append(token.value)
            elif token.type == 'OPERATOR-INFIX' and token.value in {'+', '-', '*', '/', '&', '=', '<>', '<', '<=', '>', '>='}:
                fragments.append({'&': '@', '=': '==', '<>': '!='}.get(token.value, token.value))
            elif token.type == 'OPERATOR-PREFIX' and token.value in {'+', '-'}:
                fragments.append(token.value)
            elif token.type in {'FUNC', 'PAREN'} and token.subtype in {'OPEN', 'CLOSE'}:
                fragments.append(token.value)
            elif token.type == 'SEP' and token.subtype == 'ARG':
                fragments.append(',')
            else:
                raise IRValidationError("UNSUPPORTED_FORMULA_TOKEN")
        node = ast.parse(''.join(fragments), mode='eval')
    except (SyntaxError, RecursionError, IndexError, ValueError, TokenizerError):
        raise IRValidationError("INVALID_FORMULA_SYNTAX") from None

    def walk(node, kind=T.NUMBER):
        if isinstance(node, ast.Expression):
            return walk(node.body, kind)
        if isinstance(node, ast.Constant) and type(node.value) in (int, float, str, bool):
            return literal(node.value)
        if isinstance(node, ast.Name) and node.id in refs:
            return _reference(refs[node.id], kind)
        if isinstance(node, ast.UnaryOp) and type(node.op) in (ast.USub, ast.UAdd):
            return call('subtract', literal(0), walk(node.operand)) if isinstance(node.op, ast.USub) else walk(node.operand)
        if isinstance(node, ast.BinOp):
            names = {ast.Add: 'add', ast.Sub: 'subtract', ast.Mult: 'multiply', ast.Div: 'divide', ast.MatMult: 'concat'}
            if type(node.op) in names:
                hint = T.TEXT if isinstance(node.op, ast.MatMult) else T.NUMBER
                return call(names[type(node.op)], walk(node.left, hint), walk(node.right, hint))
        if isinstance(node, ast.Compare) and len(node.ops) == 1:
            names = {ast.Eq: 'eq', ast.NotEq: 'ne', ast.Lt: 'lt', ast.LtE: 'le', ast.Gt: 'gt', ast.GtE: 'ge'}
            if type(node.ops[0]) in names:
                hint = T.SCALAR if any(isinstance(n, ast.Constant) and isinstance(n.value, str) for n in (node.left, node.comparators[0])) else T.NUMBER
                return call(names[type(node.ops[0])], walk(node.left, hint), walk(node.comparators[0], hint))
        if isinstance(node, ast.Call) and isinstance(node.func, ast.Name) and not node.keywords:
            name = node.func.id.upper()
            names = {'IF': 'if', 'AND': 'and', 'OR': 'or', 'NOT': 'not', 'ROUND': 'round', 'MOD': 'mod',
                     'TRIM': 'trim', 'SUBSTITUTE': 'replace', 'SUM': 'sum', 'MAX': 'max', 'MIN': 'min',
                     'COUNTIF': 'count_eq', 'COUNT_EQ': 'count_eq', 'LOOKUP_EXACT': 'lookup_exact',
                     'LEFT':'left','RIGHT':'right','MID':'mid','LEN':'length','FIND':'find','SEARCH':'search',
                     'ISNUMBER':'is_number','ISTEXT':'is_text','IFERROR':'if_error','ROUNDUP':'roundup',
                     'YEAR':'year','MONTH':'month','DAY':'day','DATE':'date','EOMONTH':'eomonth',
                     'SUMIFS':'sum_matches','COUNTIFS':'count_matches','SUMIF':'sum_matches',
                     'VLOOKUP':'lookup_exact','XLOOKUP':'lookup_exact'}
            if name not in names:
                raise IRValidationError("UNSUPPORTED_FORMULA_FUNCTION:" + name)
            if name in {'COUNTIF', 'COUNT_EQ'}:
                if len(node.args) != 2:
                    raise IRValidationError("INVALID_COUNT_ARGUMENTS")
                criterion = walk(node.args[1], T.SCALAR)
                if name == 'COUNTIF' and criterion.operator == 'literal' and isinstance(criterion.value, str) and re.search(r'[<>*=~?]', criterion.value):
                    raise IRValidationError("COUNTIF_PATTERN_NOT_QUALIFIED")
                element = criterion.value_type if criterion.value_type in SERIES_TYPES else T.SCALAR
                return call('count_eq', walk(node.args[0], element), criterion)
            if name in {'LOOKUP_EXACT', 'XLOOKUP'}:
                if len(node.args) not in (3, 4):
                    raise IRValidationError("INVALID_LOOKUP_ARGUMENTS")
                return call('lookup_exact', walk(node.args[0], T.SCALAR), walk(node.args[1], T.SCALAR),
                            walk(node.args[2], kind), *(walk(a, kind) for a in node.args[3:]))
            if name == 'VLOOKUP':
                if len(node.args) != 4 or not isinstance(node.args[2], ast.Constant) or type(node.args[2].value) is not int or not isinstance(node.args[3], ast.Constant) or node.args[3].value not in (0, False):
                    raise IRValidationError('ONLY_EXPLICIT_EXACT_VLOOKUP_QUALIFIED')
                table = walk(node.args[1], T.SCALAR)
                if table.reference is None or not table.reference.is_range or table.reference.is_symbolic:
                    raise IRValidationError('EXPLICIT_LOOKUP_TABLE_REQUIRED')
                from openpyxl.utils.cell import get_column_letter, range_boundaries
                left, top, right, bottom = range_boundaries(table.reference.address)
                index = node.args[2].value
                if kind not in SERIES_TYPES or not 1 <= index <= right - left + 1:
                    raise IRValidationError('LOOKUP_COLUMN_OUTSIDE_TABLE')
                keys = reference(get_column_letter(left) + str(top) + ':' + get_column_letter(left) + str(bottom), T.SCALARS, table.reference.sheet)
                values = reference(get_column_letter(left + index - 1) + str(top) + ':' + get_column_letter(left + index - 1) + str(bottom), SERIES_TYPES[kind], table.reference.sheet)
                return call('lookup_exact', walk(node.args[0], T.SCALAR), keys, values)
            if name in {'SUMIFS', 'COUNTIFS', 'SUMIF'}:
                if name == 'SUMIF':
                    if len(node.args) not in (2, 3):
                        raise IRValidationError('INVALID_SUMIF_ARGUMENTS')
                    args = (walk(node.args[2] if len(node.args) == 3 else node.args[0], T.NUMBER), walk(node.args[0], T.SCALAR), walk(node.args[1], T.SCALAR))
                else:
                    first = 1 if name == 'SUMIFS' else 0
                    if len(node.args) < first + 2 or (len(node.args) - first) % 2:
                        raise IRValidationError('INVALID_CRITERIA_ARGUMENTS')
                    args = tuple([walk(node.args[0], T.NUMBER)] if first else []) + tuple(walk(a, T.SCALAR) for a in node.args[first:])
                criteria = args[2::2] if name != 'COUNTIFS' else args[1::2]
                if any(a.operator == 'literal' and isinstance(a.value, str) and (re.search(r'[<>*=~?]', a.value) or re.fullmatch(r'[+-]?(?:\d+(?:\.\d*)?|\.\d+)(?:[eE][+-]?\d+)?|\d+[-/]\d+[-/]\d+', a.value.strip())) for a in criteria):
                    raise IRValidationError('CRITERIA_PATTERN_NOT_QUALIFIED')
                return call(names[name], *args)
            if name == 'IFERROR':
                if len(node.args) != 2:
                    raise IRValidationError('INVALID_ERROR_FALLBACK_ARGUMENTS')
                args = tuple(walk(a, kind) for a in node.args)
                if any(a.value_type != T.TEXT for a in args):
                    args = tuple(literal(None) if a.operator == 'literal' and a.value == '' else a for a in args)
                return call('if_error', *args)
            if name in {'LEFT','RIGHT','MID','LEN','FIND','SEARCH','ISNUMBER','ISTEXT','YEAR','MONTH','DAY','EOMONTH'}:
                if name in {'ISNUMBER','ISTEXT','YEAR','MONTH','DAY','EOMONTH'}:
                    args = tuple(walk(a, T.SCALAR if i == 0 else T.NUMBER) for i, a in enumerate(node.args))
                elif name in {'FIND','SEARCH'}:
                    args = tuple(walk(a, T.TEXT if i < 2 else T.NUMBER) for i, a in enumerate(node.args))
                else:
                    args = tuple(walk(a, T.TEXT if i == 0 else T.NUMBER) for i, a in enumerate(node.args))
                    if name in {'LEFT','RIGHT'} and len(args) == 1:
                        args += (literal(1),)
                return call(names[name], *args)
            if name == 'IF':
                if len(node.args) != 3:
                    raise IRValidationError("INVALID_IF_ARGUMENTS")
                condition = walk(node.args[0], T.BOOLEAN)
                branches = tuple(walk(a, kind) for a in node.args[1:])
                if any(a.value_type != T.TEXT for a in branches):
                    branches = tuple(literal(None) if a.operator == 'literal' and a.value == '' else a for a in branches)
                return call('if', condition, *branches)
            hint = T.TEXT if name in {'TRIM', 'SUBSTITUTE'} else T.BOOLEAN if name in {'AND', 'OR', 'NOT'} else T.NUMBER
            return call(names[name], *(walk(a, hint) for a in node.args))
        raise IRValidationError("UNSUPPORTED_FORMULA_SYNTAX")

    try:
        return walk(node)
    except RecursionError:
        raise IRValidationError("EXPRESSION_LIMIT_EXCEEDED") from None
