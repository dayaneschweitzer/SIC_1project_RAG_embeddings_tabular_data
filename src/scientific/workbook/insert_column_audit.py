from copy import deepcopy, copy
from hashlib import sha256
import re
from pathlib import Path
from xml.etree import ElementTree as ET
from zipfile import ZipFile
from collections import Counter

from openpyxl import load_workbook
from openpyxl.cell.cell import MergedCell
from .ooxml_inspection import MAIN, inspect_ooxml

ALLOWED_FUNCTIONS = frozenset("SUM AVERAGE MIN MAX COUNT COUNTA COUNTBLANK IF IFERROR AND OR NOT ABS ROUND ROUNDUP ROUNDDOWN INT MOD POWER SQRT LEN LEFT RIGHT MID CONCATENATE CONCAT TRIM UPPER LOWER VALUE TEXT SUMIF SUMIFS COUNTIF COUNTIFS AVERAGEIF AVERAGEIFS".split())
LEX = re.compile(r'\"(?:[^"]|\"\")*\"|(?:\x27(?:[^\x27]|\x27\x27)*\x27|[A-Za-z_][A-Za-z0-9_.]*)!\$?[A-Za-z_][A-Za-z0-9_.$]*(?::\$?[A-Za-z0-9$]+)?|\$?[A-Za-z_][A-Za-z0-9_.$]*(?::\$?[A-Za-z0-9$]+)?|\$?[0-9]+:\$?[0-9]+|(?:[0-9]+(?:\.[0-9]*)?|\.[0-9]+)(?:[Ee][+-]?[0-9]+)?|\s+|[=+*/^&%,()<>:-]')
ATOM = re.compile(r"(\$?)([A-Za-z]{1,3})(\$?)([1-9][0-9]*)?$")


def number(letters):
    n = 0
    for char in letters.upper():
        n = n * 26 + ord(char) - 64
    return n


def letters(n):
    result = ""
    while n:
        n, r = divmod(n - 1, 26)
        result = chr(65 + r) + result
    return result


def reference(text, context, target, k, names, sheets):
    chunks = text.rsplit("!", 1)
    scope = chunks[0].strip("'").replace("''", "'") if len(chunks) == 2 else context
    body = chunks[-1]
    if len(chunks) == 2 and scope.casefold() not in [s.casefold() for s in sheets]:
        raise ValueError("INSERT_DEPENDENCY_UNSUPPORTED: unknown sheet")
    endpoints = body.split(":")
    parsed = [ATOM.fullmatch(p) for p in endpoints]
    rows = all(re.fullmatch(r"\$?[1-9][0-9]*", p) for p in endpoints)
    cell = len(endpoints) == 1 and parsed[0] and parsed[0][4]
    area = len(endpoints) == 2 and ((all(parsed) and bool(parsed[0][4]) == bool(parsed[1][4])) or rows)
    if not cell and not area:
        available = names.get("", set()) | names.get(scope.casefold() if scope else "", set())
        if body.casefold() not in available:
            raise ValueError("INSERT_DEPENDENCY_UNSUPPORTED: unresolved name " + body)
        return text
    if scope is None:
        raise ValueError("INSERT_DEPENDENCY_UNSUPPORTED: global relative context")
    if rows:
        if max(int(p.replace("$", "")) for p in endpoints) > 1048576:
            raise ValueError("INSERT_REFERENCE_OVERFLOW")
        return text
    result = []
    for item in parsed:
        col = number(item[2])
        row = int(item[4]) if item[4] else None
        if col > 16384 or (row is not None and row > 1048576):
            raise ValueError("INSERT_REFERENCE_OVERFLOW")
        shifted = col + int(scope.casefold() == target.casefold() and col >= k)
        if shifted > 16384:
            raise ValueError("INSERT_REFERENCE_OVERFLOW")
        new = letters(shifted)
        if item[2].islower():
            new = new.lower()
        result.append(item[1] + new + (item[3] + item[4] if item[4] else ""))
    return (chunks[0] + "!" if len(chunks) == 2 else "") + ":".join(result)


def formula(value, context, target, k, names, sheets):
    if value is None or isinstance(value, (int, float)):
        return value
    text = str(value)
    tokens = []
    offset = 0
    while offset < len(text):
        match = LEX.match(text, offset)
        if match is None:
            raise ValueError("INSERT_DEPENDENCY_UNSUPPORTED: formula token " + text[offset:offset+20])
        tokens.append(match[0])
        offset = match.end()
    out = []
    for i, token in enumerate(tokens):
        if token.startswith('"') or token.isspace() or re.fullmatch(r"[=+*/^&%,()<>:-]", token) or re.fullmatch(r"(?:[0-9]+(?:\.[0-9]*)?|\.[0-9]+)(?:[Ee][+-]?[0-9]+)?", token):
            out.append(token)
            continue
        rest = next((t for t in tokens[i+1:] if not t.isspace()), None)
        if rest == "(":
            if token.upper() not in ALLOWED_FUNCTIONS:
                raise ValueError("INSERT_DEPENDENCY_UNSUPPORTED: function " + token)
            out.append(token)
        elif token.upper() in {"TRUE", "FALSE"}:
            out.append(token)
        else:
            out.append(reference(token, context, target, k, names, sheets))
    return "".join(out)


def relative_axes(text):
    result = set()
    for token in LEX.findall(str(text or "")):
        if token.startswith('"'):
            continue
        body = token.rsplit("!", 1)[-1]
        parts = body.split(":")
        matches = [ATOM.fullmatch(part) for part in parts]
        if all(matches) and (len(parts) == 2 or matches[0][4]):
            for match in matches:
                if match[1] != "$":
                    result.add("column")
                if match[4] and match[3] != "$":
                    result.add("row")
        elif len(parts) == 2 and all(re.fullmatch(r"\$?[1-9][0-9]*", part) for part in parts):
            if any(not part.startswith("$") for part in parts):
                result.add("row")
    return result


def signature(element):
    return (element.tag, tuple(sorted(element.attrib.items())), element.text or "", tuple(signature(x) for x in element))


def package(path):
    inv = inspect_ooxml(path)
    with ZipFile(path) as archive:
        roots = {name: ET.fromstring(archive.read(sheet["part"])) for name, sheet in inv["sheets"].items()}
        wb = ET.fromstring(archive.read("xl/workbook.xml"))
        entries = archive.infolist()
        for entry in entries:
            name = entry.orig_filename
            segments = name[:-1].split("/") if entry.is_dir() else name.split("/")
            if not name or "\\" in name or ":" in name or any(x in {"", ".", ".."} for x in segments):
                raise ValueError("INSERT_DEPENDENCY_UNSUPPORTED: unsafe package path")
            if entry.is_dir() and entry.file_size:
                raise ValueError("INSERT_DEPENDENCY_UNSUPPORTED: nonempty directory entry")
        members = [entry.filename for entry in entries if not entry.is_dir()]
        for name in members:
            if name.startswith("xl/") and not (name in {"xl/workbook.xml", "xl/styles.xml", "xl/sharedStrings.xml", "xl/_rels/workbook.xml.rels"} or re.fullmatch(r"xl/worksheets/sheet[0-9]+.xml", name) or name.startswith("xl/theme/")):
                raise ValueError("INSERT_DEPENDENCY_UNSUPPORTED: package part " + name)
    return roots, wb


def definitions(wb, sheets):
    names = {"": set()}
    for node in wb.findall(f"{{{MAIN}}}definedNames/{{{MAIN}}}definedName"):
        if node.get("name", "").startswith("_xlnm."):
            raise ValueError("INSERT_DEPENDENCY_UNSUPPORTED: print/filter name")
        scope = sheets[int(node.get("localSheetId"))] if node.get("localSheetId") is not None else ""
        names.setdefault(scope.casefold(), set()).add(node.get("name").casefold())
    return names


def expected_xml(path, target, k):
    roots, wb = package(path)
    sheets = list(roots)
    if target not in sheets:
        raise ValueError("INSERT_TARGET_ABSENT")
    names = definitions(wb, sheets)
    allowed = {"sheetPr", "dimension", "sheetViews", "sheetFormatPr", "cols", "sheetData", "mergeCells", "dataValidations", "pageMargins", "pageSetup", "printOptions", "sheetProtection", "headerFooter"}
    for title, root in roots.items():
        for node in root:
            if node.tag.split("}")[-1] not in allowed:
                raise ValueError("INSERT_DEPENDENCY_UNSUPPORTED: worksheet feature " + node.tag)
        for f in root.iter(f"{{{MAIN}}}f"):
            if f.attrib:
                raise ValueError("INSERT_DEPENDENCY_UNSUPPORTED: array/shared formula")
            f.text = formula(f.text, title, target, k, names, sheets)
        for dv in root.iter(f"{{{MAIN}}}dataValidation"):
            if any("column" in relative_axes(child.text) for child in dv):
                cols = set()
                for area in dv.get("sqref", "").split():
                    ends = [ATOM.fullmatch(x) for x in area.split(":")]
                    if not all(ends):
                        raise ValueError("INSERT_DEPENDENCY_UNSUPPORTED: validation range")
                    cols.update(number(x[2]) for x in ends)
                if len(cols) != 1:
                    raise ValueError("INSERT_DEPENDENCY_UNSUPPORTED: relative validation across columns")
            if title == target:
                dv.set("sqref", " ".join(reference(x, title, target, k, names, sheets) for x in dv.get("sqref", "").split()))
            for child in dv:
                child.text = formula(child.text, title, target, k, names, sheets)
        intervals = []
        for col in root.findall(f"{{{MAIN}}}cols/{{{MAIN}}}col"):
            lo, hi = int(col.get("min")), int(col.get("max"))
            if any(lo <= b and a <= hi for a, b in intervals):
                raise ValueError("INSERT_DEPENDENCY_UNSUPPORTED: overlapping column dimensions")
            intervals.append((lo, hi))
            if title == target:
                if hi >= 16384 and hi >= k:
                    raise ValueError("INSERT_REFERENCE_OVERFLOW")
                col.set("min", str(lo + int(lo >= k)))
                col.set("max", str(hi + int(hi >= k)))
        for cell in root.iter(f"{{{MAIN}}}c"):
            if set(cell.attrib) - {"r", "s", "t"} or any(x.tag.split("}")[-1] not in {"v", "f", "is"} for x in cell):
                raise ValueError("INSERT_DEPENDENCY_UNSUPPORTED: cell extension")
            if any(x.tag == f"{{{MAIN}}}r" for x in cell.iter()):
                raise ValueError("INSERT_DEPENDENCY_UNSUPPORTED: rich text")
            if title == target:
                cell.set("r", reference(cell.get("r"), title, target, k, names, sheets))
    for node in wb.findall(f"{{{MAIN}}}definedNames/{{{MAIN}}}definedName"):
        context = sheets[int(node.get("localSheetId"))] if node.get("localSheetId") is not None else None
        if relative_axes(node.text):
            raise ValueError("INSERT_DEPENDENCY_UNSUPPORTED: relative defined name lacks anchor")
        node.text = formula(node.text, context, target, k, names, sheets)
    return roots, wb, names


def merged_projection(path, book):
    roots, _ = package(path)
    for title, root in roots.items():
        for node in root.iter(f"{{{MAIN}}}c"):
            cell = book[title][node.get("r")]
            if not isinstance(cell, MergedCell):
                continue
            if any(child.tag in {f"{{{MAIN}}}f", f"{{{MAIN}}}is"} or (child.tag == f"{{{MAIN}}}v" and child.text is not None) for child in node):
                raise ValueError("INSERT_DEPENDENCY_UNSUPPORTED: hidden merged content")
            if node.get("s") is not None and book._cell_styles[int(node.get("s"))] != cell._style:
                raise ValueError("INSERT_DEPENDENCY_UNSUPPORTED: noncanonical merged style")


def preflight(path, target, k):
    with ZipFile(path) as archive:
        styles = ET.fromstring(archive.read("xl/styles.xml"))
        if any(node.tag == f"{{{MAIN}}}extLst" for node in styles.iter()):
            raise ValueError("INSERT_DEPENDENCY_UNSUPPORTED: stylesheet extensions cannot be preserved by backend")
    expected_xml(path, target, k)
    book = load_workbook(path, data_only=False)
    try:
        merged_projection(path, book)
        size = book[target].max_column
        if k < 1 or k > size + 1 or size >= 16384:
            raise ValueError("INSERT_POSITION_OUT_OF_SUPPORTED_BOUNDS")
        for sheet in book:
            if sheet.protection.sheet:
                raise ValueError("INSERT_DEPENDENCY_UNSUPPORTED: protected worksheet")
        return {"before_max_column": size, "append": k == size + 1}
    finally:
        book.close()


def verify_insert_column(before, after, target, k):
    report = {"schema_version": "insert_column_dependency_verifier_v2", "covered_status": "NOT_VERIFIED", "overall_status": "NOT_VERIFIED", "errors": [], "not_verified": ["calculation_and_cached_values", "visual_equivalence", "task_correctness", "full_workbook_fidelity"]}
    if not Path(before).is_file() or not Path(after).is_file():
        report["reason"] = "ARTIFACT_NOT_AVAILABLE"
        return report
    try:
        identities = {"before": sha256(Path(before).read_bytes()).hexdigest(), "after": sha256(Path(after).read_bytes()).hexdigest()}
        report["identities"] = identities
        pre = preflight(before, target, k)
        expected, expected_wb, names = expected_xml(before, target, k)
        actual, actual_wb = package(after)
        errors = report["errors"]
        if list(expected) != list(actual):
            raise ValueError("sheet set changed")
        with ZipFile(before) as first, ZipFile(after) as last:
            original_styles = ET.fromstring(first.read("xl/styles.xml"))
            observed_styles = ET.fromstring(last.read("xl/styles.xml"))
            for section in original_styles:
                counterpart = observed_styles.find(section.tag)
                if counterpart is None or len(counterpart) < len(section) or any(signature(a) != signature(b) for a, b in zip(section, counterpart)):
                    errors.append("style_definition_changed:" + section.tag)
        original_roots, _ = package(before)
        for title in expected:
            for tag in ("sheetPr", "sheetViews", "sheetFormatPr", "pageMargins", "pageSetup", "printOptions", "sheetProtection", "headerFooter"):
                before_node = original_roots[title].find(f"{{{MAIN}}}{tag}")
                after_node = actual[title].find(f"{{{MAIN}}}{tag}")
                if (signature(before_node) if before_node is not None else None) != (signature(after_node) if after_node is not None else None):
                    errors.append(title + ":preserved_metadata:" + tag)
            for tag in ("dataValidations", "cols"):
                left = expected[title].find(f"{{{MAIN}}}{tag}")
                right = actual[title].find(f"{{{MAIN}}}{tag}")
                def norm(node):
                    if node is None:
                        return None
                    node = deepcopy(node)
                    if tag == "dataValidations":
                        for dv in node:
                            dv.set("sqref", " ".join(sorted(dv.get("sqref", "").split())))
                    return (tuple(sorted(node.attrib.items())), tuple(sorted((signature(x) for x in node), key=repr)))
                if norm(left) != norm(right):
                    errors.append(title + ":" + tag)
        def named(root):
            return Counter(signature(n) for n in root.findall(f"{{{MAIN}}}definedNames/{{{MAIN}}}definedName"))
        if named(expected_wb) != named(actual_wb):
            errors.append("defined_names")
        left = load_workbook(before, data_only=False)
        right = load_workbook(after, data_only=False)
        try:
            merged_projection(after, right)
            for sheet in left:
                dest = right[sheet.title]
                want = {}
                for (row, col), cell in sheet._cells.items():
                    c = col + int(sheet.title == target and col >= k)
                    value = formula(cell.value, sheet.title, target, k, names, left.sheetnames) if cell.data_type == "f" else cell.value
                    want[(row, c)] = (value, cell.data_type, (copy(cell.font), copy(cell.fill), copy(cell.border), copy(cell.alignment), copy(cell.protection), cell.number_format))
                for coord, (value, dtype, style) in want.items():
                    cell = dest.cell(*coord)
                    if cell.value != value or cell.data_type != dtype or (copy(cell.font), copy(cell.fill), copy(cell.border), copy(cell.alignment), copy(cell.protection), cell.number_format) != style:
                        errors.append(sheet.title + ":cell:" + str(coord))
                for coord, cell in dest._cells.items():
                    if coord not in want and not isinstance(cell, MergedCell) and (cell.value is not None or cell.has_style):
                        errors.append(sheet.title + ":extra_cell:" + str(coord))
                merges = set()
                for m in sheet.merged_cells.ranges:
                    lo = m.min_col + int(sheet.title == target and m.min_col >= k)
                    hi = m.max_col + int(sheet.title == target and m.max_col >= k)
                    merges.add(f"{letters(lo)}{m.min_row}:{letters(hi)}{m.max_row}")
                observed = {str(m) for m in dest.merged_cells.ranges}
                if merges != observed:
                    errors.append(sheet.title + ":merges")
                maxcol = sheet.max_column + int(sheet.title == target)
                if dest.max_column != maxcol or dest.max_row != sheet.max_row:
                    errors.append(sheet.title + ":extent")
                if {key:signature(v.to_tree()) for key,v in sheet.row_dimensions.items()} != {key:signature(v.to_tree()) for key,v in dest.row_dimensions.items()}:
                    errors.append(sheet.title + ":row_dimensions")
        finally:
            left.close()
            right.close()
        report["covered_status"] = "FAIL" if errors else "PASS"
        report["properties"] = {"transformed": ["cells_and_formula_references", "merged_ranges", "defined_names", "validation_references", "column_dimensions", "logical_extent"], "preserved": ["unaffected_cell_values_and_styles", "sheet_set", "row_dimensions"]}
    except ValueError as exc:
        report["covered_status"] = "FAIL"
        report["errors"].append(str(exc))
    except Exception as exc:
        report["covered_status"] = "INSPECTION_ERROR"
        report["errors"].append(type(exc).__name__ + ": " + str(exc))
    if "identities" in report:
        try:
            final = {"before": sha256(Path(before).read_bytes()).hexdigest(), "after": sha256(Path(after).read_bytes()).hexdigest()}
            if final != report["identities"]:
                report["covered_status"] = "INSPECTION_ERROR"
                report["errors"].append("ARTIFACT_CHANGED_DURING_INSPECTION")
        except OSError as exc:
            report["covered_status"] = "INSPECTION_ERROR"
            report["errors"].append(str(exc))
    report["overall_status"] = report["covered_status"] if report["covered_status"] in {"FAIL", "INSPECTION_ERROR"} else "NOT_VERIFIED"
    return report
