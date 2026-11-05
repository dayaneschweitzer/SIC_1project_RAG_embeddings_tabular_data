from __future__ import annotations

from decimal import Decimal, InvalidOperation
import re

from openpyxl.utils.cell import coordinate_to_tuple, range_boundaries, quote_sheetname

from .action_dsl import ActionSource, ActionDSLValidationError
from .modification_intent import normalize_request


CELL = r"\$?[A-Z]{1,3}\$?[1-9]\d*"
RANGE = rf"{CELL}:{CELL}"
TITLE = r"(?:\"[^\"\n]+\"|'(?:[^'\n]|'')+'|[1-9]\d*)"
NOUN = r"(?:products|items|amounts|sales|units|quantities|orders)"


def sheet_reference(text):
    if text.isdigit():
        return {"kind": "numbered_label", "number": int(text)}
    return {"kind": "name", "name": text[1:-1].replace("''", "'") if text.startswith("'") else text[1:-1]}


def sheet_description(reference):
    return f"sheet {reference['number']}" if reference['kind'] == 'numbered_label' else f'sheet "{reference["name"]}"'


def resolve_sheet(reference, evidences):
    names = {e['sheet_name'] for e in evidences if e.get('sheet_name')}
    for e in evidences:
        names.update(e.get('structural_metadata', {}).get('workbook_content_sheets', []))
    if reference['kind'] == 'name':
        matches = [name for name in names if name.casefold() == reference['name'].casefold()]
    else:
        matches = [name for name in names if re.fullmatch(rf"sheet ?{reference['number']}", name, re.I)]
    if len(matches) != 1:
        raise ValueError('UNIQUE_NAMED_SUM_SHEET_EVIDENCE_REQUIRED')
    return matches[0]


def interpret_conditional_sum(instruction):
    text = normalize_request(instruction)
    detailed = re.fullmatch(
        rf"(?:I (?:need|want) (?:assistance|help) (?:writing|creating)|(?:Please )?(?:write|create)) a formula "
        rf"in (?:cell )?(?P<destination>{CELL}) of (?:sheet|worksheet) (?P<target>{TITLE}) "
        rf"that sums(?: up)? (?:the )?(?:total (?:number|amount) of )?(?P<noun>{NOUN}) "
        rf"from (?:sheet|worksheet) (?P<source>{TITLE}), (?:range )?(?P<sum>{RANGE})\. "
        rf"This sum should only include (?P=noun) that match the value in (?:cell )?(?P<key>{CELL}) "
        rf"of (?:sheet|worksheet) (?P<key_sheet>{TITLE}), which corresponds to the values in column "
        rf"(?P<column>[A-Z]{{1,3}}) of (?:sheet|worksheet) (?P<criteria_sheet>{TITLE})\."
        rf"(?: For example, if the (?:product|item|amount|sale|unit|quantity|order) in (?:cell )?(?P<example_key>{CELL}) "
        rf"of (?:sheet|worksheet) (?P<example_sheet>{TITLE}) is (?P<label>[A-Za-z][A-Za-z0-9 _-]*), "
        rf"I expect to get a result of (?P<result>-?\d+(?:\.\d+)?)\.)?", text, re.I)
    compact = None if detailed else re.fullmatch(
        rf"(?:In|On) (?:sheet|worksheet) (?P<target>{TITLE}), (?:write|put) in (?P<destination>{CELL}) "
        rf"a formula (?:summing|to sum) (?:range )?(?P<sum>{RANGE}) (?:on|from) (?:sheet|worksheet) (?P<source>{TITLE}) "
        rf"where column (?P<column>[A-Z]{{1,3}}) matches (?:cell )?(?P<key>{CELL}) "
        rf"(?:on|of) (?:sheet|worksheet) (?P<key_sheet>{TITLE})\.?", text, re.I)
    match = detailed or compact
    if not match:
        return None
    target, source = sheet_reference(match['target']), sheet_reference(match['source'])
    if sheet_reference(match['key_sheet']) != target or detailed and sheet_reference(match['criteria_sheet']) != source:
        return None
    reference = lambda name: match[name].replace('$', '').upper()
    destination, criterion, sum_range = reference('destination'), reference('key'), reference('sum')
    left, first, right, last = range_boundaries(sum_range)
    if left != right or first > last or destination == criterion:
        return None
    criteria_range = f"{match['column'].upper()}{first}:{match['column'].upper()}{last}"
    try:
        ActionSource(cells=(destination, criterion), ranges=(sum_range, criteria_range))
    except ActionDSLValidationError:
        return None
    example = None
    if detailed and match['example_key']:
        if (reference('example_key') != criterion or sheet_reference(match['example_sheet']) != target
                or re.search(r"\b(?:and|then|also|unless|except)\b", match['label'], re.I)):
            return None
        example = {'role': 'illustrative_instruction_example', 'criterion_text': match['label'],
                   'result': match['result'], 'used_as_output_constant': False}
    return {'kind': 'conditional_sum', 'type': 'NUMERICA', 'target_sheet_reference': target,
            'source_sheet_reference': source, 'destination': destination, 'criterion': criterion,
            'sum_range': sum_range, 'criteria_range': criteria_range, 'comparison': 'excel_equal',
            'wildcards': 'literal', 'example': example}


def resolve_conditional_sum(goal, evidences):
    from .native_operations import _cells
    target = resolve_sheet(goal['target_sheet_reference'], evidences)
    source = resolve_sheet(goal['source_sheet_reference'], evidences)
    key = goal['criterion']
    target_row, target_col = coordinate_to_tuple(goal['destination'])
    for evidence in evidences:
        if evidence.get('sheet_name') != target:
            continue
        metadata = evidence.get('structural_metadata', {})
        for name in ('merged_ranges', 'merged_cells'):
            for region in metadata.get(name, []):
                left, top, right, bottom = range_boundaries(region)
                if left <= target_col <= right and top <= target_row <= bottom:
                    raise ValueError('MERGED_SUM_DESTINATION_NOT_QUALIFIED')
    cell = _cells(evidences, target).get(key, {})
    value = cell.get('value')
    if cell.get('is_formula') or cell.get('data_type') not in {'s', 'str', 'inlineStr', 'n'} or value in (None, '') or isinstance(value, bool):
        raise ValueError('TYPED_SUM_CRITERION_EVIDENCE_REQUIRED')
    if cell['data_type'] == 'n':
        try:
            if not Decimal(str(value)).is_finite():
                raise ValueError('FINITE_SUM_CRITERION_REQUIRED')
        except InvalidOperation as error:
            raise ValueError('FINITE_SUM_CRITERION_REQUIRED') from error
    elif not isinstance(value, str) or len(value) + sum(value.count(c) for c in '~*?') + 1 > 255:
        raise ValueError('SUM_CRITERION_STRING_NOT_QUALIFIED')
    if source == target:
        row, col = coordinate_to_tuple(goal['destination'])
        if any((b := range_boundaries(r))[0] <= col <= b[2] and b[1] <= row <= b[3]
               for r in (goal['sum_range'], goal['criteria_range'])):
            raise ValueError('CIRCULAR_SUM_DESTINATION')
    criterion = f'"="&SUBSTITUTE(SUBSTITUTE(SUBSTITUTE({key},"~","~~"),"*","~*"),"?","~?")'
    formula = f"=SUMIF({quote_sheetname(source)}!{goal['criteria_range']},{criterion},{quote_sheetname(source)}!{goal['sum_range']})"
    return [{'parse_status': 'parsed', 'operation': 'write_formula', 'target_sheet': target,
             'destination': goal['destination'], 'source_cells': [key], 'source_ranges_sheet': source,
             'source_ranges': [goal['criteria_range'], goal['sum_range']], 'formula': formula,
             'resolved_constraints': {'comparison': goal['comparison'], 'wildcards': goal['wildcards'],
                                      'criteria_rows_from_explicit_sum_range': True, 'example': goal['example']}}]
