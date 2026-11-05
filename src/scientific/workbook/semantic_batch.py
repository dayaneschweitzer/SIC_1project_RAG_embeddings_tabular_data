from __future__ import annotations

from .action_grounding import ground_action
from .semantic_ir import WriteOperation


def batch_eligible(goal):
    return (goal.operation == WriteOperation.VALUE and not goal.target.is_cell and not goal.target.is_range
            and goal.expression.operator in {'date_text', 'to_text', 'after_colon', 'strip_iso_time'}
            and all(r.address == goal.target.address and r.sheet == goal.target.sheet for r in goal.references()))


def compile_batch(goal, rows, sheet, resolver):
    from .semantic_compiler import SemanticCompilationError, evaluate_expression, materialized_equal
    cells, complete = resolver.domain(sheet)
    if any(cells.get(goal.target.address + str(row), {}).get('is_formula') for row in rows):
        if len(rows) > 10000:
            raise SemanticCompilationError('SOURCE_DOMAIN_LIMIT_EXCEEDED')
        return None
    mapping, expected = {}, {}
    for row in rows:
        resolver.begin()
        coordinate = goal.target.address + str(row)
        cell = resolver.cell(sheet, coordinate)
        old = cell.get('value')
        if goal.predicate is not None and not evaluate_expression(goal.predicate, row, sheet, cells, resolver):
            expected[coordinate] = old
            continue
        new = evaluate_expression(goal.expression, row, sheet, cells, resolver)
        expected[coordinate] = new
        if new is None or new == '' or isinstance(new, str) and new.startswith('='):
            raise SemanticCompilationError('EXECUTOR_LITERAL_NOT_MATERIALIZABLE')
        if materialized_equal(cell,new):
            continue
        key = str(old)
        if key in mapping and (type(mapping[key][1]) is not type(new) or mapping[key][1] != new):
            raise SemanticCompilationError('AMBIGUOUS_BATCH_REPLACEMENT')
        mapping[key] = old, new
    if not mapping:
        raise SemanticCompilationError('EMPTY_OPERATION_SCOPE_NOT_REPRESENTABLE')
    if len(mapping) > 1024:
        raise SemanticCompilationError('ACTION_PLAN_STEP_LIMIT_EXCEEDED')
    for coordinate, new in expected.items():
        old = cells.get(coordinate, {}).get('value')
        simulated = old
        for before, after in mapping.values():
            if str(simulated) == str(before):
                simulated = after
        if type(simulated) is not type(new) or simulated != new:
            if len(rows) <= 10000:
                return None
            raise SemanticCompilationError('BATCH_REPLACEMENT_COMPOSITION_NOT_QUALIFIED')
    domain = goal.target.address + str(rows[0]) + ':' + goal.target.address + str(rows[-1])
    proofs = []
    for before, after in mapping.values():
        intent = {'parse_status':'parsed', 'operation':'replace_values', 'target_sheet':sheet,
                  'source_range':domain, 'replacement_old':before, 'replacement_new':after}
        proof = ground_action(intent, resolver.evidences)
        if proof.status != 'grounded':
            raise SemanticCompilationError('BATCH_GROUNDING_FAILED:' + ','.join(proof.missing_requirements))
        proofs.append(proof)
    return proofs
