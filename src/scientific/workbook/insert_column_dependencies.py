from copy import copy
import re
from zipfile import ZipFile, ZIP_DEFLATED
from xml.etree import ElementTree as ET

from openpyxl.formula import Tokenizer
from openpyxl.utils import column_index_from_string, get_column_letter
from .ooxml_inspection import MAIN, inspect_ooxml

FUNCTIONS = frozenset("SUM AVERAGE MIN MAX COUNT COUNTA COUNTBLANK IF IFERROR AND OR NOT ABS ROUND ROUNDUP ROUNDDOWN INT MOD POWER SQRT LEN LEFT RIGHT MID CONCATENATE CONCAT TRIM UPPER LOWER VALUE TEXT SUMIF SUMIFS COUNTIF COUNTIFS AVERAGEIF AVERAGEIFS".split())
CELL = re.compile(r"(\$?)([A-Za-z]{1,3})(\$?)([1-9][0-9]*)")
COL = re.compile(r"(\$?)([A-Za-z]{1,3})")
ROW = re.compile(r"\$?[1-9][0-9]*")


def shifted_reference(value, context, target, position, names, sheets):
    prefix, separator, body = value.rpartition("!")
    owner = prefix[1:-1].replace("''", "'") if prefix.startswith("'") and prefix.endswith("'") else prefix
    owner = owner if separator else context
    body = body if separator else value
    if separator and owner.casefold() not in {s.casefold() for s in sheets}:
        raise ValueError("INSERT_DEPENDENCY_UNSUPPORTED: sheet reference")
    parts = body.split(":")
    if len(parts) > 2:
        raise ValueError("INSERT_DEPENDENCY_UNSUPPORTED: 3D reference")
    kinds = [("cell" if CELL.fullmatch(x) else "col" if COL.fullmatch(x) else "row" if ROW.fullmatch(x) else "") for x in parts]
    is_range = len(parts) == 2 and kinds[0] == kinds[1] and kinds[0]
    is_cell = len(parts) == 1 and kinds[0] == "cell"
    if not is_cell and not is_range:
        if body.casefold() not in names.get(owner.casefold() if owner else "", set()) | names.get("", set()):
            raise ValueError("INSERT_DEPENDENCY_UNSUPPORTED: unresolved name " + value)
        return value
    if owner is None:
        raise ValueError("INSERT_DEPENDENCY_UNSUPPORTED: unqualified global reference")
    result = []
    for part, kind in zip(parts, kinds):
        if kind == "row":
            if int(part.replace("$", "")) > 1048576:
                raise ValueError("INSERT_REFERENCE_OVERFLOW")
            result.append(part)
            continue
        match = (CELL if kind == "cell" else COL).fullmatch(part)
        col = column_index_from_string(match[2])
        if kind == "cell" and int(match[4]) > 1048576:
            raise ValueError("INSERT_REFERENCE_OVERFLOW")
        if owner.casefold() == target.casefold() and col >= position:
            col += 1
        if col > 16384:
            raise ValueError("INSERT_REFERENCE_OVERFLOW")
        letters = get_column_letter(col)
        if match[2].islower():
            letters = letters.lower()
        result.append(match[1] + letters + (match[3] + match[4] if kind == "cell" else ""))
    return (prefix + "!" if separator else "") + ":".join(result)


def translate_formula(value, context, target, position, names, sheets):
    if value is None or isinstance(value, (int, float)):
        return value
    raw = str(value)
    tokens = Tokenizer(raw if raw.startswith("=") else "=" + raw).items
    out = []
    for token in tokens:
        if token.type == "FUNC" and token.subtype == "OPEN" and token.value[:-1].upper() not in FUNCTIONS:
            raise ValueError("INSERT_DEPENDENCY_UNSUPPORTED: function " + token.value)
        if token.type == "ARRAY" or token.subtype == "ERROR":
            raise ValueError("INSERT_DEPENDENCY_UNSUPPORTED: formula construct")
        out.append(shifted_reference(token.value, context, target, position, names, sheets)
                   if token.subtype == "RANGE" else token.value)
    return ("=" if raw.startswith("=") else "") + "".join(out)


def update_dependencies(workbook, target, position):
    names = {"": {x.casefold() for x in workbook.defined_names}}
    for sheet in workbook:
        names[sheet.title.casefold()] = {x.casefold() for x in sheet.defined_names}
    for sheet in workbook:
        for cell in list(sheet._cells.values()):
            if cell.data_type == "f":
                cell.value = translate_formula(cell.value, sheet.title, target, position, names, workbook.sheetnames)
        for name in sheet.defined_names.values():
            name.attr_text = translate_formula(name.attr_text, sheet.title, target, position, names, workbook.sheetnames)
        for validation in sheet.data_validations.dataValidation:
            if sheet.title == target:
                validation.sqref = " ".join(shifted_reference(str(r), sheet.title, target, position, names, workbook.sheetnames)
                                            for r in validation.sqref.sorted())
            validation.formula1 = translate_formula(validation.formula1, sheet.title, target, position, names, workbook.sheetnames)
            validation.formula2 = translate_formula(validation.formula2, sheet.title, target, position, names, workbook.sheetnames)
    for name in workbook.defined_names.values():
        name.attr_text = translate_formula(name.attr_text, None, target, position, names, workbook.sheetnames)
    sheet = workbook[target]
    dimensions = []
    for key, original in sheet.column_dimensions.items():
        item = copy(original)
        low = item.min or column_index_from_string(key)
        high = item.max or low
        item.min = low + (low >= position)
        item.max = high + (high >= position)
        item.index = get_column_letter(item.min)
        dimensions.append(item)
    sheet.column_dimensions.clear()
    for item in dimensions:
        sheet.column_dimensions[item.index] = item


def persist_append_extent(path, target, expected_column):
    inventory = inspect_ooxml(path)
    part = inventory["sheets"][target]["part"]
    with ZipFile(path) as archive:
        entries = [(info, archive.read(info.filename)) for info in archive.infolist()]
    root = ET.fromstring(dict((i.filename, data) for i, data in entries)[part])
    data = root.find(f"{{{MAIN}}}sheetData")
    row = next((r for r in data if r.get("r") == "1"), None)
    if row is None:
        row = ET.Element(f"{{{MAIN}}}row", {"r": "1"})
        data.insert(0, row)
    ET.SubElement(row, f"{{{MAIN}}}c", {"r": get_column_letter(expected_column) + "1", "t": "n"})
    dimension = root.find(f"{{{MAIN}}}dimension")
    old = dimension.get("ref").split(":")[-1]
    last_row = max(1, int(re.sub("[A-Z]", "", old)))
    dimension.set("ref", f"A1:{get_column_letter(expected_column)}{last_row}")
    rewritten = ET.tostring(root, encoding="utf-8")
    with ZipFile(path, "w", ZIP_DEFLATED) as archive:
        for info, payload in entries:
            archive.writestr(info, rewritten if info.filename == part else payload)
