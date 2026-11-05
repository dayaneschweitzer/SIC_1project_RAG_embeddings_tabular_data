from openpyxl.formula.tokenizer import Tokenizer
from openpyxl.utils.cell import coordinate_to_tuple
import re


REFERENCE = re.compile(r"(\$?[A-Z]{1,3})(\$?)([1-9]\d*)", re.I)


def snapshot_formula_text(value):
    text = str(value or "")
    if text.startswith("ArrayFormula(") and ", text=" in text and text.endswith(")"):
        return text.split(", text=", 1)[1][:-1]
    return text


def formula_at(plan, coordinate):
    formula = str(plan.get("formula") or "")
    if plan.get("fill_mode") != "relative_rows":
        return formula
    row, column = coordinate_to_tuple(coordinate)
    anchor_row, anchor_column = coordinate_to_tuple(plan["destination"])
    if column != anchor_column:
        raise ValueError("Only vertical formula fill is qualified")
    pieces = []
    for token in Tokenizer(formula).items:
        text = token.value
        if token.type == "OPERAND" and token.subtype == "RANGE":
            if "!" in text or "[" in text:
                raise ValueError("Cross-sheet/external formula fill is not qualified")
            def shift(match):
                target = int(match[3]) if match[2] else int(match[3]) + row - anchor_row
                if not 1 <= target <= 1048576:
                    raise ValueError("Translated reference outside worksheet")
                return match[1] + match[2] + str(target)
            text = REFERENCE.sub(shift, text)
        pieces.append(text)
    return "=" + "".join(pieces)
