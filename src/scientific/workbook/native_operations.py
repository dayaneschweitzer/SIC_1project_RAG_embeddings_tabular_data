from __future__ import annotations

import re

from openpyxl.utils.cell import coordinate_to_tuple, get_column_letter, range_boundaries

from .modification_intent import normalize_request


VERSION = "sic_native_operations_v1"
Q = r'''(?:"[^"\n]+"|'[^'\n]+')'''
CELL = r"\$?[A-Z]{1,3}\$?[1-9]\d*"


def interpret_operation(instruction):
    from .native_columns import interpret_column_operation
    columns = interpret_column_operation(instruction)
    if columns:
        return columns
    from .native_sum import interpret_conditional_sum
    conditional_sum = interpret_conditional_sum(instruction)
    if conditional_sum:
        return conditional_sum
    from .native_conditional import interpret_conditional_lookup
    conditional_lookup = interpret_conditional_lookup(instruction)
    if conditional_lookup:
        return conditional_lookup
    text = normalize_request(instruction)
    conditional = re.fullmatch(
        rf"(?:How can I |I (?:need|want) to )?create a formula for column (?P<target>[A-Z]{{1,3}}) in Excel that will display "
        rf"(?P<first>{Q}) in each cell if the corresponding cell in the range (?P<start>{CELL}) (?:to|:) (?P<end>{CELL}) "
        rf"contains (?:the word |the text )?(?P<term1>{Q}), and (?P<second>{Q}) if it contains (?:the word |the text )?(?P<term2>{Q})[?.]?",
        text, re.I)
    if conditional:
        source = conditional['start'].replace('$', '').upper() + ':' + conditional['end'].replace('$', '').upper()
        left, top, right, bottom = range_boundaries(source)
        target = conditional['target'].upper()
        if left != right or top > bottom or bottom - top >= 10000 or target == get_column_letter(left):
            return None
        return {'kind':'text_contains_choice', 'type':'TEXTUAL', 'sheet':None, 'source_range':source,
                'target_column':target, 'choices':[
                    {'text':conditional['term1'][1:-1], 'result':conditional['first'][1:-1]},
                    {'text':conditional['term2'][1:-1], 'result':conditional['second'][1:-1]}],
                'unmatched_or_overlapping':'refuse', 'case_sensitive':False}
    filtering = re.fullmatch(
        rf"(?:In (?:my|the) Excel spreadsheet, (?:which has (?:over )?[\d,]+ rows, )?)?I (?:need|want) to filter column "
        rf"(?P<column>[A-Z]{{1,3}}) for (?:the )?(?:groups|values) labeled (?P<values>{Q}(?:,\s*{Q})*(?:,?\s+and\s+{Q})?), "
        rf"and then delete these rows\.?(?: The (?:attached )?(?:example|sample) (?:only )?(?:shows|contains) [\d,]+ rows\.)?"
        rf"(?: Can the provided code handle deleting the rows without taking excessive amounts of time\?)?",
        text, re.I)
    if filtering:
        return {'kind':'filter_delete_rows', 'type':'ESTRUTURAL', 'sheet':None,
                'column':filtering['column'].upper(), 'values':[m[0][1:-1] for m in re.finditer(Q, filtering['values'])],
                'comparison':'exact_case_sensitive', 'scope':'existing_input_rows'}
    total = re.fullmatch(
        rf"(?:How can I |How do I )?Create a formula in Excel to sum a range of cells that contains the (?P<error>#[A-Z0-9/]+!?) error value, "
        rf"as illustrated in the attached {Q} spreadsheet\? My current formula is: SUM\((?P<range>{CELL}:{CELL})\)", text, re.I)
    if total:
        return {'kind':'sum_ignoring_error', 'type':'NUMERICA', 'sheet':None,
                'source_range':total['range'].replace('$','').upper(), 'error':total['error'].upper() if total['error'].upper() in {'#N/A','#GETTING_DATA'} or total['error'].endswith('!') else total['error'].upper()+'!',
                'current_formula':'=SUM('+total['range'].replace('$','').upper()+')'}
    return None


def sheet_for(goal, evidences):
    if goal.get('sheet'):
        return goal['sheet']
    inventories = {tuple(e.get('structural_metadata',{}).get('workbook_content_sheets',[]))
                   for e in evidences if e.get('structural_metadata',{}).get('workbook_content_sheets')}
    if len(inventories) != 1 or len(next(iter(inventories))) != 1:
        raise ValueError('UNIQUE_CONTENT_SHEET_EVIDENCE_REQUIRED')
    return next(iter(inventories))[0]


def _cells(evidences, sheet):
    cells = {}
    for e in evidences:
        if e.get('sheet_name') != sheet:
            continue
        for cell in e.get('cells',[]):
            if not isinstance(cell,dict) or not cell.get('coordinate') or 'value' not in cell:
                continue
            key = cell['coordinate'].replace('$','').upper()
            if key in cells and (cells[key].get('value'),cells[key].get('data_type')) != (cell['value'],cell.get('data_type')):
                raise ValueError('CONFLICTING_SOURCE_EVIDENCE')
            cells[key] = cell
    return cells


def resolve_operation(goal, evidences):
    if goal['kind'] in {'normalize_column_spaces', 'conditional_text_suffix', 'timestamp_difference'}:
        from .native_columns import resolve_column_operation
        return resolve_column_operation(goal, evidences)
    if goal["kind"] == "conditional_sum":
        from .native_sum import resolve_conditional_sum
        return resolve_conditional_sum(goal, evidences)
    if goal["kind"] == "conditional_existing_lookup":
        from .native_conditional import resolve_conditional_lookup
        return resolve_conditional_lookup(goal, evidences)
    sheet = sheet_for(goal,evidences)
    cells = _cells(evidences,sheet)
    if goal['kind'] == 'text_contains_choice':
        left,top,right,bottom = range_boundaries(goal['source_range'])
        for row in range(top,bottom+1):
            cell = cells.get(f'{get_column_letter(left)}{row}',{})
            value = cell.get('value')
            if cell.get('data_type') not in {'s','inlineStr','str'} or not isinstance(value,str) or cell.get('is_formula'):
                raise ValueError('TYPED_TEXT_SOURCE_EVIDENCE_REQUIRED')
            matches = [c for c in goal['choices'] if c['text'].casefold() in value.casefold()]
            if len(matches) != 1:
                raise ValueError('UNSPECIFIED_OR_OVERLAPPING_TEXT_BRANCH')
        def literal(value):
            return '"'+value.replace('"','""')+'"'
        a,b=goal['choices']
        # SEARCH criteria are escaped so wildcard characters remain literal.
        def search_text(value):
            return literal(value.replace('~','~~').replace('*','~*').replace('?','~?'))
        source=f'{get_column_letter(left)}{top}'
        formula=f'=IF(ISNUMBER(SEARCH({search_text(a["text"])},{source})),{literal(a["result"])},IF(ISNUMBER(SEARCH({search_text(b["text"])},{source})),{literal(b["result"])},NA()))'
        return [{'parse_status':'parsed','operation':'write_formula','target_sheet':sheet,
                 'source_ranges':[goal['source_range']], 'destination':f'{goal["target_column"]}{top}',
                 'target_range':f'{goal["target_column"]}{top}:{goal["target_column"]}{bottom}',
                 'formula':formula,'fill_mode':'relative_rows',
                 'resolved_constraints':{'complete_text_domain':goal['source_range'],'unmatched_or_overlapping':'refused'}}]
    if goal['kind'] == 'sum_ignoring_error':
        left,top,right,bottom=range_boundaries(goal['source_range'])
        for row in range(top,bottom+1):
            for column in range(left,right+1):
                c=cells.get(f'{get_column_letter(column)}{row}',{})
                if not c or c.get('is_formula') or c.get('data_type') not in {'n','e'}:
                    raise ValueError('COMPLETE_TYPED_SUM_DOMAIN_REQUIRED')
                if c.get('data_type')=='e' and str(c['value']).upper()!=goal['error']:
                    raise ValueError('UNSPECIFIED_ERROR_KIND')
        targets=[coordinate for coordinate,c in cells.items() if str(c['value']).upper()==goal['current_formula'].upper()]
        if len(targets)!=1:
            raise ValueError('UNIQUE_EXISTING_SUM_FORMULA_REQUIRED')
        return [{'parse_status':'parsed','operation':'write_formula','target_sheet':sheet,
                 'source_ranges':[goal['source_range']], 'source_cells':targets,'destination':targets[0],
                 'formula':f'=SUMPRODUCT(IFERROR({goal["source_range"]},0))'}]
    if goal['kind'] != 'filter_delete_rows':
        raise ValueError('UNSUPPORTED_NATIVE_OPERATION')
    complete = [e for e in evidences if e.get('sheet_name')==sheet
                and e.get('block_type') in {'used_range','sheet_overview'}
                and e.get('structural_metadata',{}).get('source_block_complete') is True]
    if not complete:
        raise ValueError('COMPLETE_SHEET_EVIDENCE_REQUIRED_FOR_ROW_DELETION')
    row_values={coordinate_to_tuple(k)[0]:c for k,c in cells.items() if re.fullmatch(goal['column']+r'[1-9]\d*',k)}
    if any(c.get('is_formula') for c in row_values.values()):
        raise ValueError('FORMULA_FILTER_PREDICATE_NOT_QUALIFIED')
    if any(isinstance(c['value'],str) and c['value'] not in goal['values']
           and any(c['value'].casefold()==v.casefold() for v in goal['values']) for c in row_values.values()):
        raise ValueError('FILTER_CASE_SEMANTICS_AMBIGUOUS')
    rows=sorted((row for row,c in row_values.items() if c['value'] in goal['values']),reverse=True)
    if not rows:
        raise ValueError('EMPTY_DELETION_NOOP_NOT_REPRESENTABLE')
    return [{'parse_status':'parsed','operation':'delete_row','target_sheet':sheet,
             'deletion_position':row,'source_cells':[f'{goal["column"]}{row}'],
             'resolved_constraints':{'match_values':goal['values'],'comparison':goal['comparison'],
                                     'input_row':row,'order':'descending_original_rows'}} for row in rows]
