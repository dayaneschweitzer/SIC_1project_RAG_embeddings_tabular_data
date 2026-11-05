from __future__ import annotations

import re
from datetime import datetime, time

from openpyxl.utils.cell import coordinate_to_tuple, get_column_letter, range_boundaries

from .semantic_ir import ELEMENT_TYPES, Reference


class ReferenceResolver:
    def __init__(self, evidences, virtual):
        self.evidences = evidences
        self.virtual = virtual
        self.active = []
        self.reads = set()
        self.visits = 0
        self.planned_formulas = {}

    def begin(self):
        self.reads.clear()
        self.visits = 0

    def sheet(self, name):
        from .semantic_compiler import SemanticCompilationError
        names = {e.get('sheet_name') for e in self.evidences if e.get('sheet_name')}
        matches = [n for n in names if isinstance(name, str) and n.casefold() == name.casefold()]
        if len(matches) != 1:
            raise SemanticCompilationError('UNIQUE_NAMED_SHEET_EVIDENCE_REQUIRED')
        return matches[0]

    def domain(self, sheet):
        from .semantic_compiler import _sheet_cells
        sheet = self.sheet(sheet)
        if sheet not in self.virtual:
            self.virtual[sheet] = _sheet_cells(sheet, self.evidences)
        return self.virtual[sheet]

    def coordinates(self, ref, row):
        from .semantic_compiler import SemanticCompilationError, _coordinate
        if not ref.is_range:
            return (_coordinate(ref, row),)
        left, top, right, bottom = range_boundaries(ref.address)
        if (right - left + 1) * (bottom - top + 1) > 10000:
            raise SemanticCompilationError('SOURCE_DOMAIN_LIMIT_EXCEEDED')
        return tuple(get_column_letter(c) + str(r) for r in range(top, bottom + 1) for c in range(left, right + 1))

    def cell(self, sheet, coordinate):
        from .semantic_compiler import SemanticCompilationError
        self.visits += 1
        if self.visits > 50000:
            raise SemanticCompilationError('DEPENDENCY_EVALUATION_LIMIT_EXCEEDED')
        cells, complete = self.domain(sheet)
        if coordinate not in cells:
            row, col = coordinate_to_tuple(coordinate)
            if not any(left <= col <= right and top <= row <= bottom for left, top, right, bottom in complete):
                raise SemanticCompilationError('SOURCE_CELL_EVIDENCE_MISSING:' + coordinate)
            cells[coordinate] = {'coordinate': coordinate, 'value': None, 'data_type': 'n', 'is_formula': False}
        self.reads.add((sheet, coordinate))
        if len(self.reads) > 10000:
            raise SemanticCompilationError('DEPENDENCY_DOMAIN_LIMIT_EXCEEDED')
        return cells[coordinate]

    def structured_formula(self, formula, sheet, row):
        from .semantic_compiler import SemanticCompilationError
        pattern = r'(?P<table>[A-Za-z_][\w.]*)\[\[#This Row\],\[(?P<column>[^\[\]]+)\]\]'
        def resolve(match):
            tables = set()
            for e in self.evidences:
                if e.get('sheet_name') == sheet:
                    for table in e.get('structural_metadata', {}).get('tables', []):
                        if isinstance(table, dict) and str(table.get('name', '')).casefold() == match['table'].casefold():
                            tables.add(table.get('ref'))
            if len(tables) != 1:
                raise SemanticCompilationError('UNIQUE_STRUCTURED_TABLE_EVIDENCE_REQUIRED')
            left, top, right, bottom = range_boundaries(next(iter(tables)))
            if not top < row <= bottom:
                raise SemanticCompilationError('STRUCTURED_ROW_OUTSIDE_TABLE')
            matches = []
            for column in range(left, right + 1):
                header = self.cell(sheet, get_column_letter(column) + str(top))
                if header.get('data_type') in {'s', 'str', 'inlineStr'} and str(header.get('value', '')).casefold() == match['column'].casefold():
                    matches.append(column)
            if len(matches) != 1:
                raise SemanticCompilationError('UNIQUE_STRUCTURED_COLUMN_EVIDENCE_REQUIRED')
            return get_column_letter(matches[0]) + str(row)
        return re.sub(pattern, resolve, formula, flags=re.I)

    def value(self, ref, row):
        from .semantic_compiler import SemanticCompilationError, _bind_expression, _cell_value, evaluate_expression
        from .semantic_formula import parse_formula
        sheet = self.sheet(ref.sheet)
        epochs = {e.get('structural_metadata', {}).get('workbook_epoch') for e in self.evidences if e.get('sheet_name') == sheet and e.get('structural_metadata', {}).get('workbook_epoch') is not None}
        if len(epochs) > 1:
            raise SemanticCompilationError('CONFLICTING_WORKBOOK_EPOCH_EVIDENCE')
        epoch = next(iter(epochs), None)
        values = []
        for coordinate in self.coordinates(ref, row):
            cell = self.cell(sheet, coordinate)
            kind = ELEMENT_TYPES.get(ref.value_type, ref.value_type)
            if cell.get('is_formula') or cell.get('data_type') == 'f':
                key = sheet, coordinate
                if key in self.active:
                    raise SemanticCompilationError('CIRCULAR_SOURCE_DEPENDENCY')
                if len(self.active) >= 32:
                    raise SemanticCompilationError('DEPENDENCY_DEPTH_LIMIT_EXCEEDED')
                formula = cell.get('value')
                if not isinstance(formula, str) or not formula.startswith('='):
                    raise SemanticCompilationError('FORMULA_SOURCE_TYPE_NOT_QUALIFIED')
                self.active.append(key)
                try:
                    source_row = coordinate_to_tuple(coordinate)[0]
                    formula = self.structured_formula(formula, sheet, source_row)
                    try:
                        expr = self.planned_formulas.get(key)
                        if expr is None:
                            expr = _bind_expression(parse_formula(formula), sheet)
                    except ValueError as error:
                        raise SemanticCompilationError('FORMULA_SOURCE_TYPE_NOT_QUALIFIED:' + str(error)) from None
                    value = evaluate_expression(expr, source_row, sheet, self.domain(sheet)[0], self)
                finally:
                    self.active.pop()
                cell = {'value': value, 'data_type': 'b' if type(value) is bool else 's' if isinstance(value, str) else 'd' if isinstance(value, (datetime, time)) else 'n', 'is_formula': False}
            values.append(_cell_value(cell, kind, epoch))
        return values if ref.is_range else values[0]
