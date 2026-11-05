from __future__ import annotations

from openpyxl.utils.cell import coordinate_to_tuple, get_column_letter, range_boundaries

from .action_grounding import ground_action
from .semantic_ir import WriteOperation as W
from .semantic_operators import equal


def compile_structural(goal, sheet, resolver):
    from .semantic_compiler import SemanticCompilationError, evaluate_expression
    cells, complete = resolver.domain(sheet)
    if not complete:
        raise SemanticCompilationError('COMPLETE_STRUCTURAL_DOMAIN_REQUIRED')
    base = {'parse_status': 'parsed', 'operation': goal.operation.value, 'target_sheet': sheet}
    intents = []
    if goal.operation == W.DELETE_ROW:
        populated = [coordinate_to_tuple(k)[0] for k, c in cells.items() if c.get('value') not in (None, '', 'None')]
        if not populated:
            raise SemanticCompilationError('EMPTY_DELETION_DOMAIN')
        first = goal.target.start_row or min(populated)
        last = goal.target.end_row or max(populated)
        if last - first + 1 > 10000 or not any(top <= first and bottom >= last for _, top, _, bottom in complete):
            raise SemanticCompilationError('COMPLETE_STRUCTURAL_DOMAIN_REQUIRED')
        duplicate = goal.expression.operator in {'duplicate_first', 'duplicate_last', 'duplicate_unmarked'}
        ordered = list(range(first, last + 1))
        keys = {}
        chosen = {}
        groups = {}
        flags = {}
        if duplicate:
            for row in ordered:
                marked = goal.expression.operator == 'duplicate_unmarked'
                arguments = goal.expression.arguments[:-2] if marked else goal.expression.arguments
                values = [evaluate_expression(a, row, sheet, cells, resolver) for a in arguments]
                keys[row] = tuple(('blank', '') if v in (None, '') else ('text', v.casefold()) if isinstance(v, str) else ('number', v) if type(v) in (int, float) else (type(v).__name__, v) for v in values)
                groups.setdefault(keys[row], []).append(row)
                if marked:
                    flags[row] = evaluate_expression(goal.expression.arguments[-2], row, sheet, cells, resolver)
                    if flags[row] not in (None, '') and not equal(flags[row], goal.expression.arguments[-1].value):
                        raise SemanticCompilationError('UNSPECIFIED_DUPLICATE_FLAG')
                if goal.expression.operator == 'duplicate_last' or keys[row] not in chosen:
                    chosen[keys[row]] = row
            if flags and any(len(group) > 1 and not any(flags[r] not in (None, '') for r in group) for group in groups.values()):
                raise SemanticCompilationError('AMBIGUOUS_UNMARKED_DUPLICATE_GROUP')
        for row in ordered:
            resolver.begin()
            if goal.expression.operator == 'row_blank':
                selected = all(c.get('value') in (None, '', 'None') and not c.get('is_formula')
                               for k, c in cells.items() if coordinate_to_tuple(k)[0] == row)
            elif duplicate:
                selected = len(groups[keys[row]]) > 1 and flags[row] in (None, '') if flags else chosen[keys[row]] != row
            else:
                selected = evaluate_expression(goal.expression, row, sheet, cells, resolver)
            if type(selected) is not bool:
                raise SemanticCompilationError('BOOLEAN_DELETION_PREDICATE_REQUIRED')
            if selected:
                anchor = goal.target.address if not goal.target.is_symbolic else 'A'
                if goal.target.is_cell:
                    anchor = ''.join(c for c in anchor if c.isalpha())
                intents.append({**base, 'deletion_position': row, 'source_cells': [anchor + str(row)]})
        intents.reverse()
    elif goal.operation == W.COPY_SHEET:
        intents = [{**base, 'new_sheet_name': goal.expression.value}]
    elif goal.operation in {W.INSERT_ROW, W.INSERT_COLUMN}:
        row, col = coordinate_to_tuple(goal.target.address if goal.target.is_cell else goal.target.address + '1')
        intents = [{**base, 'insertion_axis': 'row' if goal.operation == W.INSERT_ROW else 'column',
                    'insertion_position': row if goal.operation == W.INSERT_ROW else col}]
    elif goal.operation in {W.UNMERGE, W.SORT}:
        if not goal.target.is_range or goal.target.is_symbolic:
            raise SemanticCompilationError('EXPLICIT_STRUCTURAL_RANGE_REQUIRED')
        intent = {**base, 'source_range': goal.target.address}
        if goal.operation == W.SORT:
            if goal.expression.value != 'ascending':
                raise SemanticCompilationError('EXECUTOR_SORT_ORDER_NOT_SUPPORTED')
            left, top, _, bottom = range_boundaries(goal.target.address)
            if any(resolver.cell(sheet, get_column_letter(left) + str(row)).get('value') is not None
                   and resolver.cell(sheet, get_column_letter(left) + str(row)).get('data_type') not in {'s','str','inlineStr'}
                   for row in range(top, bottom + 1)):
                raise SemanticCompilationError('EXECUTOR_NUMERIC_SORT_NOT_QUALIFIED')
            intent.update(sort_key='first_column', sort_order='ascending')
        intents = [intent]
    else:
        raise SemanticCompilationError('EXECUTOR_STRUCTURAL_OPERATION_UNAVAILABLE')
    if not intents:
        raise SemanticCompilationError('EMPTY_OPERATION_SCOPE_NOT_REPRESENTABLE')
    proofs = []
    for intent in intents:
        proof = ground_action(intent, resolver.evidences)
        if proof.status != 'grounded':
            raise SemanticCompilationError('STRUCTURAL_GROUNDING_FAILED:' + ','.join(proof.missing_requirements))
        proofs.append(proof)
    return proofs
