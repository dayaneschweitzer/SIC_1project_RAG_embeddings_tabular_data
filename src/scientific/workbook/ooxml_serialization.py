"""Retain source spelling and attribute presence lost by the XLSX backend."""

from copy import deepcopy
import xml.etree.ElementTree as ET
from zipfile import ZipFile

from openpyxl.worksheet.cell_range import MultiCellRange
from openpyxl.xml.functions import fromstring

from .ooxml_inspection import MAIN, inspect_ooxml


class _PreservedHeaderFooter:
    def __init__(self, element):
        self.element = fromstring(ET.tostring(element, encoding="utf-8"))

    def __bool__(self):
        return True

    def to_tree(self):
        return deepcopy(self.element)


class _SourceSqref(MultiCellRange):
    def __init__(self, source):
        super().__init__(source)
        self._source = source
        self._bounds = self._current_bounds()

    def _current_bounds(self):
        return frozenset(item.bounds for item in self.ranges)

    def __str__(self):
        if self._current_bounds() == self._bounds:
            return self._source
        return super().__str__()

    def __hash__(self):
        return hash(super().__str__())


def preserve_source_serialization(workbook, input_path, plan):
    inventory = inspect_ooxml(input_path)
    mapping = {name: name for name in inventory["sheets"]}
    if plan.get("operation") == "copy_sheet" and plan.get("new_sheet_name") in workbook.sheetnames:
        mapping[plan["new_sheet_name"]] = plan["target_sheet"]
    with ZipFile(input_path) as archive:
        for target, source in mapping.items():
            root = ET.fromstring(archive.read(inventory["sheets"][source]["part"]))
            sheet = workbook[target]
            if plan.get("operation") == "insert_column":
                header_footer = root.find(f"{{{MAIN}}}headerFooter")
                if header_footer is not None:
                    sheet.HeaderFooter = _PreservedHeaderFooter(header_footer)
            original_views = root.findall(f"{{{MAIN}}}sheetViews/{{{MAIN}}}sheetView")
            if len(original_views) == len(sheet.views.sheetView):
                for original, view in zip(original_views, sheet.views.sheetView):
                    selections = original.findall(f"{{{MAIN}}}selection")
                    if len(selections) != len(view.selection):
                        continue
                    for original_selection, selection in zip(selections, view.selection):
                        for attribute in ("activeCell", "sqref"):
                            if attribute not in original_selection.attrib and getattr(selection, attribute) == "A1":
                                setattr(selection, attribute, None)
            original_cf = root.findall(f"{{{MAIN}}}conditionalFormatting")
            rules = sheet.conditional_formatting._cf_rules
            if len(original_cf) == len(rules):
                preserved = rules.__class__()
                for original, (key, values) in zip(original_cf, rules.items()):
                    literal = original.get("sqref")
                    if literal and MultiCellRange(literal) == key.sqref:
                        key = deepcopy(key)
                        key.sqref = _SourceSqref(literal)
                    preserved[key] = values
                sheet.conditional_formatting._cf_rules = preserved
