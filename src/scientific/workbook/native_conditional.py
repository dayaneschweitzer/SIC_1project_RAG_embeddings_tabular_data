from __future__ import annotations

import re

from openpyxl.formula.tokenizer import Tokenizer, TokenizerError
from openpyxl.formula.translate import Translator
from openpyxl.utils.cell import coordinate_to_tuple, get_column_letter, range_boundaries

from .modification_intent import normalize_request


RANGE = r'\$?[A-Z]{1,3}\$?[1-9]\d*:\$?[A-Z]{1,3}\$?[1-9]\d*'
CELL = r'\$?[A-Z]{1,3}\$?[1-9]\d*'


def formula_signature(formula):
    try:
        return tuple((t.type, t.subtype, t.value if t.subtype == 'TEXT' else t.value.casefold())
                     for t in Tokenizer(formula).items if t.type != 'WHITE-SPACE')
    except TokenizerError as error:
        raise ValueError('MALFORMED_EXISTING_FORMULA') from error


def interpret_conditional_lookup(instruction):
    text = normalize_request(instruction)
    match = re.fullmatch(
        rf'(?:How can I |I (?:need|want) to )?modify (?:the|my) VLOOKUP formula in (?:range )?(?P<target>{RANGE}) '
        rf'to return a (?P<value_type>time|numeric|text) value from the corresponding row in (?:range )?(?P<value>{RANGE}) '
        rf'if the (?:name|value) in a cell within (?:range )?(?P<left>{RANGE}) is the same as in the corresponding cell in (?:range )?(?P<right>{RANGE})\? '
        rf'(?:(?:An example being|For example,) if (?P<example_left>{CELL}) has been manually changed to match (?P<example_right>{CELL}), '
        rf'the formula in (?P<example_target>{CELL}) should return the (?P<example_type>time|numeric|text) value from (?P<example_value>{CELL})\. )?'
        r'My current formula is:\s*(?P<formula>=?IFERROR\(VLOOKUP\(.+\),""\))\s*', text, re.I)
    if not match:
        return None
    ranges = {name: match[name].replace('$', '').upper() for name in ('target', 'value', 'left', 'right')}
    bounds = {name: range_boundaries(value) for name, value in ranges.items()}
    if any(b[0] != b[2] or b[1] > b[3] for b in bounds.values()):
        return None
    first, last = bounds['target'][1], bounds['target'][3]
    if last - first >= 1024 or any((b[1], b[3]) != (first, last) for b in bounds.values()):
        return None
    if bounds['target'][0] in {bounds[k][0] for k in ('value', 'left', 'right')} or bounds['left'][0] == bounds['right'][0]:
        return None
    if match['example_left']:
        examples = {name: coordinate_to_tuple(match['example_' + name].replace('$', '').upper()) for name in ranges}
        if len({v[0] for v in examples.values()}) != 1 or any(v[1] != bounds[k][0] or not first <= v[0] <= last for k, v in examples.items()):
            return None
        if match['example_type'].lower() != match['value_type'].lower():
            return None
    formula = '=' + match['formula'].lstrip('=')
    lookup = re.fullmatch(
        rf'=IFERROR\(VLOOKUP\((?P<key>{CELL}),(?P<sheet>\'(?:[^\']|\'\')+\'|[A-Z_][A-Z0-9_. ]*)!'
        r'\$?(?P<first>[A-Z]{1,3}):\$?(?P<last>[A-Z]{1,3}),(?P<index>[1-9]\d*),(?:0|FALSE)\),""\)', formula, re.I)
    if not lookup or coordinate_to_tuple(lookup['key'].replace('$', '').upper())[0] != first:
        return None
    key_column = re.sub(r'[$0-9]', '', lookup['key']).upper()
    if key_column == get_column_letter(bounds['target'][0]):
        return None
    from openpyxl.utils.cell import column_index_from_string
    width = column_index_from_string(lookup['last'].upper()) - column_index_from_string(lookup['first'].upper()) + 1
    if not 1 <= int(lookup['index']) <= width:
        return None
    sheet = lookup['sheet']
    sheet = sheet[1:-1].replace("''", "'") if sheet.startswith("'") else sheet
    return {'kind': 'conditional_existing_lookup', 'type': 'NUMERICA' if match['value_type'].lower() != 'text' else 'TEXTUAL',
            'sheet': None, 'ranges': ranges, 'original_formula': formula, 'lookup_sheet': sheet,
            'lookup_key_range': f'{key_column}{first}:{key_column}{last}', 'comparison': 'excel_equal',
            'fallback': 'preserve_original_formula', 'value_type': match['value_type'].lower()}


def conditional_sheet(goal, evidences):
    anchor = goal['ranges']['target'].split(':')[0]
    expected = formula_signature(goal['original_formula'])
    sheets = {e['sheet_name'] for e in evidences if e.get('sheet_name')
              for c in e.get('cells', []) if isinstance(c, dict) and c.get('coordinate') == anchor
              and c.get('data_type') == 'f' and isinstance(c.get('value'), str)
              and c['value'].startswith('=') and formula_signature(c['value']) == expected}
    if len(sheets) != 1:
        raise ValueError('UNIQUE_EXISTING_LOOKUP_SHEET_REQUIRED')
    return next(iter(sheets))


def resolve_conditional_lookup(goal, evidences):
    from .native_operations import _cells
    sheet = conditional_sheet(goal, evidences)
    cells = _cells(evidences, sheet)
    ranges = goal['ranges']
    first, last = (int(re.search(r'\d+', cell)[0]) for cell in ranges['target'].split(':'))
    anchor = ranges['target'].split(':')[0]
    source_ranges = [ranges[k] for k in ('left', 'right', 'value')] + [goal['lookup_key_range']]
    inventories = {tuple(e.get('structural_metadata', {}).get('workbook_content_sheets', [])) for e in evidences
                   if e.get('structural_metadata', {}).get('workbook_content_sheets')}
    if len(inventories) != 1 or goal['lookup_sheet'] not in next(iter(inventories)):
        raise ValueError('ORIGINAL_LOOKUP_SHEET_EVIDENCE_REQUIRED')
    blanks = []
    for reference in source_ranges:
        col, start, _, end = range_boundaries(reference)
        for row in range(start, end + 1):
            coord = f'{get_column_letter(col)}{row}'
            if coord in cells:
                if cells[coord].get('data_type') not in {'s', 'str', 'inlineStr', 'n', 'd', 'b', 'f'}:
                    raise ValueError('TYPED_CONDITIONAL_SOURCE_REQUIRED')
                continue
            complete = False
            for e in evidences:
                if e.get('sheet_name') != sheet or e.get('structural_metadata', {}).get('source_block_complete') is not True:
                    continue
                if e.get('block_type') not in {'column_block', 'used_range', 'sheet_overview'} or not e.get('range'):
                    continue
                left, top, right, bottom = range_boundaries(e['range'])
                if None not in (left, top, right, bottom) and left <= col <= right and top <= row <= bottom:
                    complete = True
            if not complete:
                raise ValueError('COMPLETE_CONDITIONAL_SOURCE_EVIDENCE_REQUIRED')
            blanks.append(coord)
    target_col = get_column_letter(range_boundaries(ranges['target'])[0])
    for row in range(first, last + 1):
        coord = f'{target_col}{row}'
        c = cells.get(coord, {})
        expected = Translator(goal['original_formula'], origin=anchor).translate_formula(coord)
        if c.get('data_type') != 'f' or not isinstance(c.get('value'), str) or not c['value'].startswith('=') or formula_signature(c['value']) != formula_signature(expected):
            raise ValueError('EXISTING_LOOKUP_FORMULA_FAMILY_REQUIRED')
    original = cells[anchor]['value']
    left, right, value = (ranges[k].split(':')[0] for k in ('left', 'right', 'value'))
    formula = f'=IF({left}={right},{value},{original[1:]})'
    return [{'parse_status': 'parsed', 'operation': 'write_formula', 'target_sheet': sheet,
             'destination': f'{target_col}{row}', 'source_ranges': source_ranges + [ranges['target']],
             'formula': Translator(formula, origin=anchor).translate_formula(f'{target_col}{row}'),
             'resolved_constraints': {'complete_domain': ranges['target'], 'fallback': 'original_formula_verified_each_row',
                                      'blank_cells_proved_by_complete_blocks': sorted(set(blanks)),
                                      'comparison': 'excel_equal', 'lookup_sheet': goal['lookup_sheet']}}
            for row in range(first, last + 1)]
