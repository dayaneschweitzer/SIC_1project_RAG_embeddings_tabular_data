from __future__ import annotations

from dataclasses import replace
import re

from openpyxl.utils.cell import coordinate_to_tuple, get_column_letter

from .semantic_ir import ELEMENT_TYPES, Reference, ValueType as T


PATTERNS = {
    'colon': r'.+:.+',
    'iso_time': r'\d{4}-\d{2}-\d{2}T\d{2}:\d{2}:\d{2}(?:\.\d+)?Z',
    'leading_space': r' +\S.*',
    'compact_date': r'\d{8}',
    'ymd_dot': r'\d{4}\.\d{2}\.\d{2}(?: .*)?',
    'sap_date': r'[A-Za-z]+, \d{1,2} [A-Za-z]+ \d{4}',
}


def header_label(value):
    labels = {'original','source','currently','date','dates','datum','timestamp','text','string','word','value','input','description','number','numbers','data','name','employee','assigned employee','office','client id','id','code','product','need','sold','budget','budget id','revenue','revenue type','amount','quantity','qty','hours','age','ageing','aging','days'}
    label = str(value).strip().casefold()
    return label in labels or bool(re.fullmatch(r'assigned employee \d{4}|office ?[1-9]\d?|fiscal year/period(?:[ .].*)?',label))


def _error(reason):
    from .semantic_compiler import SemanticCompilationError
    raise SemanticCompilationError(reason)


def _formula(value):
    from openpyxl.formula.tokenizer import Tokenizer
    try:
        value = str(value)
        if value.startswith('ArrayFormula(') and ', text=' in value:
            value = value.split(', text=', 1)[1][:-1]
        return tuple((t.type, t.subtype, re.sub(r'(?i)_xlfn\.|_xlws\.', '', t.value.casefold()) if t.subtype != 'TEXT' else t.value)
                     for t in Tokenizer('=' + value.removeprefix('=')).items if t.type != 'WHITE-SPACE')
    except Exception:
        return None


def _selector_matches(ref, cells):
    address = ref.address
    if address.startswith('@header:'):
        label = address.split(':', 1)[1].strip().casefold()
        return [(coordinate_to_tuple(k)[1], coordinate_to_tuple(k)[0] + 1)
                for k, c in cells.items() if c.get('data_type') in {'s', 'str', 'inlineStr'}
                and str(c.get('value', '')).strip().casefold() == label]
    if address.startswith(('@formula:', '@formula_column:')):
        formula = address.split(':', 1)[1]
        constrained = re.fullmatch(r'([A-Z]{1,3})\|(.+)',formula,re.S) if address.startswith('@formula_column:') else None
        column = constrained[1] if constrained else None
        formula = constrained[2] if constrained else formula
        wanted = _formula(formula)
        matches = [(k, coordinate_to_tuple(k)[0]) for k, c in cells.items()
                   if wanted and c.get('data_type') == 'f' and _formula(c.get('value')) == wanted
                   and (column is None or re.sub(r'\d','',k)==column)]
        if address.startswith('@formula_column:'):
            if not matches:
                from openpyxl.formula.translate import Translator
                rows = re.findall(r'\$?[A-Z]{1,3}\$?([1-9]\d*)', formula, re.I)
                if rows and len(set(rows)) == 1:
                    for k, c in cells.items():
                        if c.get('data_type') == 'f' and (column is None or re.sub(r'\d','',k)==column):
                            origin = re.sub(r'\d', '', k) + rows[0]
                            try:
                                translated = Translator('=' + formula.removeprefix('='), origin=origin).translate_formula(k)
                            except Exception:
                                continue
                            if _formula(translated) == _formula(c.get('value')):
                                matches.append((k, coordinate_to_tuple(k)[0]))
            groups = {}
            for coordinate, row in matches:
                groups.setdefault(re.sub(r'\d', '', coordinate), []).append(row)
            return [(col, min(rows)) for col, rows in groups.items()]
        return matches
    if address.startswith('@text:'):
        pattern = PATTERNS[address.split(':', 1)[1]]
        columns = {}
        for k, cell in cells.items():
            if cell.get('value') not in (None, '', 'None'):
                columns.setdefault(coordinate_to_tuple(k)[1], []).append((coordinate_to_tuple(k)[0], cell))
        result = []
        for col, items in columns.items():
            items.sort(key=lambda x: x[0])
            start = items[0][0]
            if items[0][0] == 1 and header_label(items[0][1].get('value', '')):
                items = items[1:]
                start = 2
            matching = [c for _, c in items if re.fullmatch(pattern, str(c.get('value', ''))) and not c.get('is_formula')]
            if matching:
                result.append((col, start))
        return result
    return []


def sheets_for_goal(goal, evidences):
    from .semantic_compiler import _sheet_names, _sheet_cells
    try:
        return _sheet_names(goal.target, evidences)
    except ValueError:
        if goal.target.sheet is not None:
            raise
        refs = [r for r in (goal.target, *goal.references()) if r.is_symbolic and r.address.startswith(('@header:', '@formula:', '@formula_column:', '@text:')) and r.sheet is None]
        if not refs:
            raise
        names = sorted({e.get('sheet_name') for e in evidences if e.get('sheet_name')})
        candidates = [name for name in names if all(len(_selector_matches(r, _sheet_cells(name, evidences)[0])) == 1 for r in refs)]
        if len(candidates) != 1:
            _error('AMBIGUOUS_CONTEXTUAL_SHEET_REFERENCE')
        return tuple(candidates)


def bind_goal(goal, sheet, resolver):
    sources = []
    from .semantic_operators import OPERATORS
    def extended(expr):
        return expr is not None and (expr.operator in OPERATORS or expr.operator in {'group_running_max_product','urn_next'} or expr.reference is not None and expr.reference.is_symbolic or any(extended(a) for a in expr.arguments))
    infer_header = extended(goal.expression) or extended(goal.predicate) or goal.target.is_symbolic or goal.operation.value not in {'write_value','write_formula'}
    raw_columns = [r for r in goal.references() if r.sheet in (None,'*',sheet) and not r.is_cell and not r.is_range and not r.is_symbolic]
    current_cells = resolver.domain(sheet)[0]
    shared_headers = bool(raw_columns) and all(header_label(current_cells.get(r.address + '1',{}).get('value')) for r in raw_columns)
    def bind(ref):
        name = resolver.sheet(sheet if ref.sheet in (None, '*') else ref.sheet)
        cells, complete = resolver.domain(name)
        if not ref.is_symbolic:
            result = replace(ref, sheet=name)
            if infer_header and not ref.is_cell and not ref.is_range and ref.start_row is None:
                top = cells.get(ref.address + '1', {})
                body = [c for k, c in cells.items() if re.sub(r'\d', '', k) == ref.address and coordinate_to_tuple(k)[0] > 1 and c.get('value') not in (None, '', 'None')]
                if top.get('data_type') in {'s', 'str', 'inlineStr'} and body and header_label(top.get('value')) and (all(c.get('data_type') in {'n', 'd'} for c in body) or shared_headers):
                    result = replace(result, start_row=2)
        elif ref.address.startswith('@domain:'):
            col = ref.address.split(':')[1]
            rows = sorted(coordinate_to_tuple(k)[0] for k, c in cells.items()
                          if re.sub(r'\d', '', k) == col and c.get('value') not in (None, '', 'None'))
            if not rows:
                _error('EMPTY_CONTEXTUAL_COLUMN_DOMAIN')
            first, last = rows[0], rows[-1]
            numeric_bodies = [c for k, c in cells.items() if coordinate_to_tuple(k)[0] > first and c.get('value') not in (None, '', 'None') and c.get('data_type') in {'n', 'd'}]
            first_values = [c for k, c in cells.items() if coordinate_to_tuple(k)[0] == first and c.get('value') not in (None, '', 'None')]
            if first == 1 and first_values and all(c.get('data_type') in {'s', 'str', 'inlineStr'} for c in first_values) and numeric_bodies:
                first += 1
            first = ref.start_row or first
            last = ref.end_row or last
            result = Reference(col + str(first) + ':' + col + str(last), ref.value_type, name)
        elif ref.address.startswith(('@header:', '@text:', '@formula:', '@formula_column:')):
            matches = _selector_matches(ref, cells)
            if len(matches) != 1:
                _error('UNIQUE_CONTEXTUAL_REFERENCE_EVIDENCE_REQUIRED:' + ref.address.split(':')[0])
            value, start = matches[0]
            if ref.address.startswith('@formula:'):
                result = Reference(value, ref.value_type, name)
            else:
                col = re.sub(r'\d', '', value) if isinstance(value, str) else get_column_letter(value)
                if ref.is_range:
                    rows = [coordinate_to_tuple(k)[0] for k, c in cells.items() if re.sub(r'\d', '', k) == col and coordinate_to_tuple(k)[0] >= start and c.get('value') not in (None, '', 'None')]
                    if not rows:
                        _error('EMPTY_CONTEXTUAL_COLUMN_DOMAIN')
                    result = Reference(col + str(start) + ':' + col + str(max(rows)), ref.value_type, name)
                else:
                    result = Reference(col, ref.value_type, name, ref.start_row or start, ref.end_row)
        elif ref.address in {'@row', '@sheet'}:
            result = replace(ref, sheet=name)
        else:
            _error('CONTEXTUAL_TARGET_REQUIRES_BOUND_SOURCES')
        return result
    def expression(expr):
        if expr.reference is not None:
            result = bind(expr.reference)
            sources.append(result)
            return replace(expr, reference=result)
        result = replace(expr, arguments=tuple(expression(a) for a in expr.arguments))
        if result.operator == 'date_format':
            from .semantic_ir import literal
            ref = result.arguments[0].reference
            if ref is None or not ref.is_range:
                _error('EXPLICIT_DATE_FORMAT_DOMAIN_REQUIRED')
            formats = {resolver.cell(ref.sheet, c).get('number_format') for c in resolver.coordinates(ref, None) if resolver.cell(ref.sheet, c).get('data_type') == 'd'}
            if len(formats) != 1 or next(iter(formats)) not in {'mm-dd-yy','dd/mm/yyyy','dd-mm-yyyy','yyyy-mm-dd','m/d/yyyy','m/d/yy'}:
                _error('UNIQUE_SOURCE_DATE_FORMAT_REQUIRED')
            return literal(next(iter(formats)))
        return result
    expr = expression(goal.expression)
    predicate = expression(goal.predicate) if goal.predicate is not None else None
    if goal.target.address in {'@same', '@adjacent'}:
        columns = {(r.address, r.sheet, r.start_row, r.end_row) for r in sources if not r.is_range and not r.is_cell and not r.is_symbolic}
        if len(columns) != 1:
            _error('UNIQUE_CONTEXTUAL_SOURCE_COLUMN_REQUIRED')
        col, name, start, end = next(iter(columns))
        index = coordinate_to_tuple(col + '1')[1] + (goal.target.address == '@adjacent')
        target = Reference(get_column_letter(index), goal.target.value_type, name, start, end)
    else:
        target = bind(goal.target)
    return replace(goal, target=target, expression=expr, predicate=predicate)


def specialize_expression(expr, row, sheet, resolver):
    from .semantic_ir import call, reference, literal
    from .semantic_operators import equal
    if expr.operator == 'group_running_max_product':
        group, left, right = (a.reference for a in expr.arguments)
        key = resolver.value(group, row)
        if key in (None, ''):
            _error('UNSPECIFIED_BLANK_GROUP')
        first = row
        while first > (group.start_row or left.start_row or right.start_row or 1):
            previous = resolver.value(group, first - 1)
            if not equal(previous, key):
                break
            first -= 1
        return call('max_product', reference(left.address + str(first) + ':' + left.address + str(row), T.NUMBERS, sheet),
                    reference(right.address + str(first) + ':' + right.address + str(row), T.NUMBERS, sheet))
    if expr.operator == 'urn_next':
        key, data = (a.reference for a in expr.arguments)
        anchor = row - 1
        first = max(key.start_row or 1, data.start_row or 1)
        while anchor >= first and resolver.value(key, anchor) in (None, ''):
            anchor -= 1
        if anchor < first:
            _error('URN_BASE_EVIDENCE_REQUIRED')
        value = resolver.value(key, anchor)
        from .semantic_operators import evaluate
        base = evaluate('to_text', [value])
        match = re.fullmatch(r'(\d+)([a-z]*)', base, re.I)
        if not match:
            _error('URN_BASE_TYPE_NOT_QUALIFIED')
        ordinal = 0
        for char in match[2].upper():
            ordinal = ordinal * 26 + ord(char) - 64
        ordinal += sum(resolver.value(key, r) in (None, '') and resolver.value(data, r) not in (None, '') for r in range(anchor + 1, row + 1))
        suffix = ''
        while ordinal:
            ordinal, digit = divmod(ordinal - 1, 26)
            suffix = chr(97 + digit) + suffix
        result = match[1] + suffix
        cells = resolver.domain(sheet)[0]
        existing = [resolver.cell(sheet, k).get('value') for k, c in list(cells.items()) if re.sub(r'\d', '', k) == key.address and coordinate_to_tuple(k)[0] >= first and c.get('value') not in (None, '', 'None')]
        normalized = [evaluate('to_text',[v]).casefold() for v in existing]
        if len(normalized) != len(set(normalized)):
            _error('AMBIGUOUS_URN_BASE_GROUP')
        if any(str(v).casefold() == result.casefold() for v in existing):
            _error('DUPLICATE_GENERATED_URN')
        return literal(result)
    return replace(expr, arguments=tuple(specialize_expression(a, row, sheet, resolver) for a in expr.arguments))
