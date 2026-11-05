from datetime import datetime
from decimal import Decimal, InvalidOperation
import re

from openpyxl.utils.cell import column_index_from_string, coordinate_to_tuple

from .modification_intent import normalize_request


Q = r'''(?:"[^"\n]+"|'[^'\n]+')'''
COL = r"[A-Z]{1,3}"


def interpret_column_operation(instruction):
    text = normalize_request(instruction)
    spaces = re.fullmatch(
        rf"I (?:need|want) a (?:VBA )?macro that can remove leading and trailing spaces for each item in cells "
        rf"and reduce any instances of multiple consecutive spaces between items within cells to just one space\. "
        rf"This macro should be applied to columns (?P<columns>{COL}(?:,\s*{COL})*(?:,? and {COL})?) "
        rf"across all sheets in a workbook\. Please ensure that each data location remains unchanged\.", text, re.I)
    compact_spaces = re.fullmatch(
        rf"(?:Please )?normalize leading, trailing and repeated spaces in columns "
        rf"(?P<columns>{COL}(?:,\s*{COL})*(?:,? and {COL})?) across all sheets without moving cells[.!]?", text, re.I)
    if spaces or compact_spaces:
        columns = re.split(r"\s*,\s*(?:and\s+)?|\s+and\s+", (spaces or compact_spaces)['columns'], flags=re.I)
        columns = [c.upper() for c in columns]
        columns = [c.strip() for c in columns if c.strip()]
        if len(set(columns)) != len(columns) or any(column_index_from_string(c) > 16384 for c in columns):
            return None
        return {'kind': 'normalize_column_spaces', 'type': 'TEXTUAL', 'columns': columns,
                'sheet_scope': 'all_content_sheets', 'space_character': ' ', 'preserve_locations': True}
    append = re.fullmatch(
        rf"(?:I (?:need|want) a macro that can |Please )?add (?P<suffix>{Q}) at the end of the text "
        rf"(?:present )?in column (?P<target>{COL}) for each row where the corresponding cell in column "
        rf"(?P<predicate>{COL}) contains the number (?P<value>-?\d+(?:\.\d+)?)[.!]?"
        rf"(?: I've provided a sample data sheet to show the desired outcome, specifically referencing cell "
        rf"(?P<example>{COL}[1-9]\d*) as an example\.)?", text, re.I)
    if append:
        target, predicate = append['target'].upper(), append['predicate'].upper()
        if target == predicate or any(column_index_from_string(c) > 16384 for c in (target, predicate)):
            return None
        if append['example'] and re.sub(r'\d', '', append['example']).upper() != target:
            return None
        return {'kind': 'conditional_text_suffix', 'type': 'TEXTUAL', 'sheet': None,
                'target_column': target, 'predicate_column': predicate, 'predicate_value': append['value'],
                'suffix': append['suffix'][1:-1], 'comparison': 'numeric_equal'}
    elapsed = re.fullmatch(
        rf"(?:How can I (?:devise|create) an? Excel formula to |Please )calculate the amount of time that has "
        rf"elapsed between two timestamps listed in Column (?P<start>{COL}) and Column (?P<end>{COL})\? "
        rf"Please fill in the answer in column (?P<target>{COL})\.", text, re.I)
    compact_elapsed = re.fullmatch(
        rf"(?:Please )?calculate elapsed time from timestamps in column (?P<start>{COL}) to timestamps in column "
        rf"(?P<end>{COL}) and write the formula in column (?P<target>{COL})[.!]?", text, re.I)
    if elapsed or compact_elapsed:
        match = elapsed or compact_elapsed
        roles = {name + '_column': match[name].upper() for name in ('start', 'end', 'target')}
        if len(set(roles.values())) != 3 or any(column_index_from_string(c) > 16384 for c in roles.values()):
            return None
        return {'kind': 'timestamp_difference', 'type': 'NUMERICA', 'sheet': None,
                **roles, 'unit': 'excel_days', 'negative_or_time_only': 'refuse'}
    return None


def operation_sheets(goal, evidences):
    inventories = {tuple(e.get('structural_metadata', {}).get('workbook_content_sheets', []))
                   for e in evidences if e.get('structural_metadata', {}).get('workbook_content_sheets')}
    if len(inventories) != 1:
        raise ValueError('UNIQUE_WORKBOOK_SHEET_INVENTORY_REQUIRED')
    sheets = next(iter(inventories))
    if not sheets or len(set(sheets)) != len(sheets):
        raise ValueError('VALID_WORKBOOK_SHEET_INVENTORY_REQUIRED')
    if goal['kind'] != 'normalize_column_spaces' and len(sheets) != 1:
        raise ValueError('UNIQUE_CONTENT_SHEET_EVIDENCE_REQUIRED')
    return sheets


def _complete_cells(evidences, sheet):
    from .native_operations import _cells
    complete = [e for e in evidences if e.get('sheet_name') == sheet
                and e.get('block_type') in {'used_range', 'sheet_overview'}
                and e.get('structural_metadata', {}).get('source_block_complete') is True]
    if not complete:
        raise ValueError('COMPLETE_SHEET_EVIDENCE_REQUIRED_FOR_COLUMN_OPERATION')
    cells = _cells(evidences, sheet)
    closed = _cells(complete, sheet)
    if any(k not in closed for k in cells):
        raise ValueError('CONTRADICTORY_COMPLETE_SHEET_EVIDENCE')
    return cells


def _text_value(cell):
    if cell.get('is_formula') or cell.get('data_type') not in {'s', 'str', 'inlineStr'} or not isinstance(cell['value'], str):
        raise ValueError('LITERAL_TEXT_SOURCE_REQUIRED')
    return cell['value']


def _write_text(sheet, coordinate, value, sources):
    if value.startswith('='):
        raise ValueError('FORMULA_LIKE_TEXT_OUTPUT_NOT_QUALIFIED')
    return {'parse_status': 'parsed', 'operation': 'write_value', 'target_sheet': sheet,
            'destination': coordinate, 'literal_value': value, 'source_cells': sources}


def resolve_column_operation(goal, evidences):
    intents = []
    for sheet in operation_sheets(goal, evidences):
        cells = _complete_cells(evidences, sheet)
        if goal['kind'] == 'normalize_column_spaces':
            for coordinate, cell in sorted(cells.items(), key=lambda pair: coordinate_to_tuple(pair[0])):
                if re.sub(r'\d', '', coordinate) not in goal['columns'] or cell['value'] is None:
                    continue
                if cell.get('data_type') in {'n', 'b', 'd'} and not cell.get('is_formula'):
                    continue
                value = _text_value(cell)
                normalized = re.sub(' +', ' ', value.strip(' '))
                if normalized != value:
                    intents.append(_write_text(sheet, coordinate, normalized, [coordinate]))
        elif goal['kind'] == 'conditional_text_suffix':
            for coordinate, cell in sorted(cells.items(), key=lambda pair: coordinate_to_tuple(pair[0])):
                if re.sub(r'\d', '', coordinate) != goal['predicate_column']:
                    continue
                if cell.get('is_formula') or cell.get('data_type') == 'e':
                    raise ValueError('LITERAL_NUMERIC_PREDICATE_REQUIRED')
                if cell.get('data_type') != 'n' or cell['value'] is None:
                    continue
                try:
                    number = Decimal(str(cell['value']))
                except InvalidOperation as error:
                    raise ValueError('INVALID_NUMERIC_PREDICATE') from error
                if not number.is_finite():
                    raise ValueError('NONFINITE_NUMERIC_PREDICATE')
                if number != Decimal(goal['predicate_value']):
                    continue
                target = goal['target_column'] + str(coordinate_to_tuple(coordinate)[0])
                if target not in cells:
                    raise ValueError('MATCHED_TEXT_TARGET_REQUIRED')
                intents.append(_write_text(sheet, target, _text_value(cells[target]) + goal['suffix'], [coordinate, target]))
        elif goal['kind'] == 'timestamp_difference':
            columns = (goal['start_column'], goal['end_column'])
            rows = sorted({coordinate_to_tuple(k)[0] for k in cells if re.sub(r'\d', '', k) in columns})
            for row in rows:
                source = [c + str(row) for c in columns]
                pair = [cells.get(c, {}) for c in source]
                if row == rows[0] and all(c.get('data_type') in {'s', 'str', 'inlineStr'} and not c.get('is_formula') for c in pair):
                    continue
                if any(c.get('data_type') != 'd' or c.get('is_formula') for c in pair):
                    raise ValueError('COMPLETE_TYPED_TIMESTAMP_PAIRS_REQUIRED')
                dates = []
                for c in pair:
                    value = str(c['value'])
                    if not re.match(r'\d{4}-\d{2}-\d{2}[ T]', value):
                        raise ValueError('FULL_TIMESTAMP_REQUIRED')
                    dates.append(datetime.fromisoformat(value))
                if any(d.tzinfo is not None for d in dates) or dates[1] < dates[0]:
                    raise ValueError('TIMESTAMP_ORDER_OR_TIMEZONE_NOT_QUALIFIED')
                intents.append({'parse_status': 'parsed', 'operation': 'write_formula', 'target_sheet': sheet,
                                'destination': goal['target_column'] + str(row), 'source_cells': source,
                                'formula': f'={source[1]}-{source[0]}'})
        else:
            raise ValueError('UNSUPPORTED_COLUMN_OPERATION')
    if not intents:
        raise ValueError('EMPTY_COLUMN_OPERATION_NOT_REPRESENTABLE')
    return intents
