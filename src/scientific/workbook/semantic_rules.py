from __future__ import annotations

import re

from openpyxl.utils.cell import get_column_letter, range_boundaries

from .semantic_ir import Goal, IRValidationError, Reference, ValueType as T, WriteOperation as W, call, literal, reference


Q = r'''(?:"[^"\n]*"|'[^'\n]*')'''
COL = r'[A-Z]{1,3}'
CELL = rf'{COL}[1-9]\d*'


def _score(identifier, span, sheet, points, source, target):
    a, b = range_boundaries(source), range_boundaries(target)
    if a[0] != a[2] or b[0] != b[2] or a[3] - a[1] != b[3] - b[1] or not 1 <= a[3] - a[1] < 100:
        raise IRValidationError('ALIGNED_VERTICAL_SCORE_RANGES_REQUIRED')
    values = reference(source, T.NUMBERS, sheet)
    highest = call('max', values)
    count = call('count_eq', values, highest)
    goals = []
    for offset in range(a[3] - a[1] + 1):
        cell = reference(get_column_letter(a[0]) + str(a[1] + offset), T.NUMBER, sheet)
        expr = call('if', call('eq', cell, highest), call('divide', literal(points), count), literal(0))
        goals.append(Goal(identifier + '_' + str(offset + 1), W.FORMULA,
                          Reference(get_column_letter(b[0]) + str(b[1] + offset), T.NUMBER, sheet), expr, span))
    return goals


def parse_rules(body, identifier, span, sheet):
    lookup = re.fullmatch(rf'Look up (?P<key>{CELL}) exactly in (?P<keys>{CELL}:{CELL}) and return the corresponding (?P<kind>number|text) from (?P<values>{CELL}:{CELL}) in (?P<target>{CELL})(?:, or (?P<default>{Q}|-?\d+(?:\.\d+)?) if missing)?', body, re.I)
    if lookup:
        kind = T.TEXT if lookup['kind'].lower() == 'text' else T.NUMBER
        args = [reference(lookup['key'].upper(), T.SCALAR, sheet),
                reference(lookup['keys'].upper(), T.SCALARS, sheet),
                reference(lookup['values'].upper(), T.TEXTS if kind == T.TEXT else T.NUMBERS, sheet)]
        if lookup['default'] is not None:
            raw = lookup['default']
            args.append(literal(raw[1:-1] if raw[:1] in "\"'" else float(raw)))
        expr = call('lookup_exact', *args)
        return [Goal(identifier, W.FORMULA, Reference(lookup['target'].upper(), kind, sheet), expr, span)]
    aggregate = re.fullmatch(rf'(?:write|put) (?:the )?(?P<operation>sum|maximum|minimum) of (?P<source>{CELL}:{CELL}) (?:in|into) (?P<target>{CELL})', body, re.I)
    if aggregate:
        op = {'maximum': 'max', 'minimum': 'min'}.get(aggregate['operation'].lower(), 'sum')
        expr = call(op, reference(aggregate['source'].upper(), T.NUMBERS, sheet))
        return [Goal(identifier, W.FORMULA, Reference(aggregate['target'].upper(), T.NUMBER, sheet), expr, span)]
    score = re.fullmatch(rf'Distribute (?P<points>\d+) points equally among the highest values in (?P<source>{CELL}:{CELL}) into (?P<target>{CELL}:{CELL}), others receive 0', body, re.I)
    if score:
        return _score(identifier, span, sheet, int(score['points']), score['source'].upper(), score['target'].upper())
    scores = re.fullmatch(
        rf'I need a formula for an Excel sheet that assigns a score of (?P<points>\d+), (?P<two>\d+), (?P<three>\d+), or 0 in rows (?P<rows>\d+(?:, \d+)*, and \d+) across columns (?P<cols>{COL}(?:, {COL})*, and {COL})\. '
        r'The score should be based on the highest total scores from the top three rows\. Specifically, if one person has the highest score alone, they receive the full (?P=points) points\. '
        r'If the highest score is tied between two people, they both receive (?P=two) points each\. If all three share the highest score, they each get (?P=three) points\. '
        r'Anyone without the highest score gets 0 points\. The columns each represent (?P=points) points', body, re.I)
    if scores:
        rows = [int(r) for r in re.findall(r'\d+', scores['rows'])]
        cols = re.findall(COL, scores['cols'].upper().replace('AND', ''))
        points = int(scores['points'])
        if len(rows) != 3 or rows != list(range(rows[0], rows[0] + 3)) or points != int(scores['two']) * 2 or points != int(scores['three']) * 3:
            raise IRValidationError('CONFLICTING_SCORE_ALLOCATION')
        goals = []
        for index, col in enumerate(cols):
            goals.extend(_score(identifier + '_' + str(index + 1), span, sheet, points, col + '1:' + col + '3', col + str(rows[0]) + ':' + col + str(rows[-1])))
        return goals
    table = re.fullmatch(
        rf'Is there a way to create a formula in Excel that acts like an {Q} condition\? Specifically, I have a spreadsheet with two columns: Column (?P<a>{COL}), which contains the values (?P<x>{Q}) or (?P<y>{Q}), and Column (?P<b>{COL}), which is either empty or contains the word (?P<z>{Q})\. '
        rf'The requirement is to have a third column, Column (?P<target>{COL}), display (?P<first>{Q}) if Column (?P=a) is (?P=x) and Column (?P=b) is empty, (?P<second>{Q}) if Column (?P=a) is (?P=y) and Column (?P=b) is empty, and (?P=second) if Column (?P=a) is (?P=x) and Column (?P=b) contains (?P=z)\. '
        rf'I am finding it challenging to construct a formula when there is a value in Column (?P=b)\. I want cell (?P<example>{CELL}) to show (?P<example_value>{Q}) when sub-dept = (?P<example_z>{Q}) and name is (?P<example_x>{Q}) '
        rf'My current formula is:IF\((?P<anchor>{CELL})="","",IF\(ISERROR\(SEARCH\("-",(?P=anchor),1\)\),"(?P<old_first>[^"]*)","(?P<old_second>[^"]*)"\)\)', body, re.I)
    if table:
        value = lambda key: table[key][1:-1]
        anchor = re.fullmatch(rf'({COL})([1-9]\d*)', table['anchor'], re.I)
        if anchor[1].upper() != table['a'].upper() or not table['example'].upper().startswith(table['target'].upper()) or any(value(left) != value(right) for left, right in (('example_value', 'second'), ('example_z', 'z'), ('example_x', 'x'))) or table['old_first'] != value('first') or table['old_second'] != value('second'):
            raise IRValidationError('CONFLICTING_DECISION_RULES')
        start = int(anchor[2])
        a = reference(table['a'].upper(), T.SCALAR, sheet, start)
        b = reference(table['b'].upper(), T.SCALAR, sheet, start)
        expr = call('case', call('is_blank', a), literal(None),
                    call('and', call('eq', a, literal(value('x'))), call('is_blank', b)), literal(value('first')),
                    call('and', call('eq', a, literal(value('y'))), call('is_blank', b)), literal(value('second')),
                    call('and', call('eq', a, literal(value('x'))), call('eq', b, literal(value('z')))), literal(value('second')))
        return [Goal(identifier, W.FORMULA, Reference(table['target'].upper(), T.TEXT, sheet, start), expr, span)]
    return None
