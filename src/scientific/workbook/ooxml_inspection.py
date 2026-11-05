"""Read-only diagnostics for validation rules and linked worksheet objects.

This inventory is independent of openpyxl and is not a fidelity metric or an
execution guard. A signature difference describes XML/content differences, not
their visual or scientific significance. Unchanged signatures do not certify a
workbook: cells, styles, calculation, and unreferenced parts are outside scope.
"""

from collections import Counter
from hashlib import sha256
import json
from pathlib import Path
import posixpath
from urllib.parse import unquote, urlsplit
import xml.etree.ElementTree as ET
from zipfile import ZipFile


MAIN = "http://schemas.openxmlformats.org/spreadsheetml/2006/main"
REL = "http://schemas.openxmlformats.org/officeDocument/2006/relationships"
STRICT_MAIN = "http://purl.oclc.org/ooxml/spreadsheetml/main"
STRICT_REL = "http://purl.oclc.org/ooxml/officeDocument/relationships"
PACKAGE_REL = "http://schemas.openxmlformats.org/package/2006/relationships"
CONTENT_TYPES = "http://schemas.openxmlformats.org/package/2006/content-types"
X14 = "http://schemas.microsoft.com/office/spreadsheetml/2009/9/main"
OBJECT_ELEMENTS = frozenset({"drawing", "legacyDrawing", "legacyDrawingHF", "picture", "oleObject", "control"})


class UnsupportedOOXMLFormat(ValueError):
    """Recognized OOXML profile outside the inventory's supported scope."""


def _digest(value) -> str:
    return sha256(json.dumps(value, ensure_ascii=False, separators=(",", ":")).encode("utf8")).hexdigest()


def _target(owner: str, target: str) -> str:
    parsed = urlsplit(target)
    if parsed.scheme or parsed.netloc or parsed.query or parsed.fragment:
        raise ValueError(f"Unsupported internal relationship target: {target}")
    decoded = unquote(parsed.path)
    if "\\" in decoded:
        raise ValueError(f"Invalid package target: {target}")
    part = posixpath.normpath(posixpath.join(posixpath.dirname(owner), decoded.lstrip("/")))
    if decoded.startswith("/"):
        part = posixpath.normpath(decoded.lstrip("/"))
    if part in {"", ".", ".."} or part.startswith("../"):
        raise ValueError(f"Target escapes package: {target}")
    return part


class _Package:
    def __init__(self, archive: ZipFile):
        self.archive = archive
        names = archive.namelist()
        if len(names) != len(set(names)):
            raise ValueError("Duplicate ZIP members")
        self.names = set(names)
        types = self.xml("[Content_Types].xml")
        if types.tag != f"{{{CONTENT_TYPES}}}Types":
            raise ValueError("Invalid content-types root")
        self.overrides = {node.attrib["PartName"].lstrip("/"): node.attrib["ContentType"]
                          for node in types.findall(f"{{{CONTENT_TYPES}}}Override")}
        self.defaults = {node.attrib["Extension"]: node.attrib["ContentType"]
                         for node in types.findall(f"{{{CONTENT_TYPES}}}Default")}
        self.relationship_cache = {}
        self.signature_cache = {}

    def xml(self, part):
        try:
            return ET.fromstring(self.archive.read(part))
        except (KeyError, ET.ParseError) as exc:
            raise ValueError(f"Missing or invalid XML part: {part}") from exc

    def relationships(self, owner):
        if owner in self.relationship_cache:
            return self.relationship_cache[owner]
        directory, filename = posixpath.split(owner)
        path = posixpath.join(directory, "_rels", filename + ".rels") if owner else "_rels/.rels"
        rels = {}
        if path in self.names:
            root = self.xml(path)
            if root.tag != f"{{{PACKAGE_REL}}}Relationships":
                raise ValueError(f"Invalid relationships root: {path}")
            for node in root:
                if node.tag != f"{{{PACKAGE_REL}}}Relationship" or any(
                    not node.get(key) or not node.get(key).strip() for key in ("Id", "Type", "Target")
                ):
                    raise ValueError(f"Invalid relationship record: {path}")
                identity = node.attrib["Id"]
                if identity in rels:
                    raise ValueError(f"Duplicate relationship ID: {owner}:{identity}")
                mode = node.get("TargetMode", "Internal")
                if mode not in {"Internal", "External"}:
                    raise ValueError(f"Unsupported relationship mode: {mode}")
                destination = node.attrib["Target"] if mode == "External" else _target(owner, node.attrib["Target"])
                rels[identity] = (node.attrib["Type"], mode, destination)
        self.relationship_cache[owner] = rels
        return rels

    def relationship_signature(self, owner, identity, trail):
        try:
            kind, mode, destination = self.relationships(owner)[identity]
        except KeyError as exc:
            raise ValueError(f"Unresolved relationship: {owner}:{identity}") from exc
        content = destination if mode == "External" else self.part_signature(destination, trail)
        return kind, mode, content

    def canonical(self, node, owner, trail=()):
        attributes = []
        for key, value in sorted(node.attrib.items()):
            if key.startswith((f"{{{REL}}}", f"{{{STRICT_REL}}}")):
                value = self.relationship_signature(owner, value, trail)
            attributes.append((key, value))
        text = node.text
        if len(node) and text is not None and not text.strip():
            text = None
        children = [(self.canonical(child, owner, trail),
                     child.tail if child.tail and child.tail.strip() else None) for child in node]
        return node.tag, attributes, text, children

    def part_signature(self, part, trail=()):
        if part in trail:
            raise ValueError(f"Cyclic object relationship: {part}")
        if part in self.signature_cache:
            return self.signature_cache[part]
        if part not in self.names:
            raise ValueError(f"Missing referenced part: {part}")
        extension = posixpath.splitext(part)[1].lstrip(".")
        content_type = self.overrides.get(part) or self.defaults.get(extension)
        if not content_type:
            raise ValueError(f"Missing content type: {part}")
        next_trail = (*trail, part)
        if extension in {"xml", "vml"} or content_type.endswith("+xml") or content_type == "application/xml":
            payload = self.canonical(self.xml(part), part, next_trail)
        else:
            payload = sha256(self.archive.read(part)).hexdigest()
        attachments = sorted(self.relationship_signature(part, identity, next_trail)
                             for identity in self.relationships(part))
        result = _digest((content_type, payload, attachments))
        self.signature_cache[part] = result
        return result


def inspect_ooxml(path: str | Path) -> dict:
    """Inventory supported validation/object XML without saving or repairing it.

    Only transitional worksheets are inventoried. Recognized other sheet types
    raise UnsupportedOOXMLFormat after their sheet relationships are checked.
    Detected invalid/missing parts raise ValueError, never an empty inventory.
    Relationship IDs, part filenames, namespace prefixes, and XML attribute
    ordering are excluded from signatures; text and rule attributes are kept.
    """
    with ZipFile(path, "r") as archive:
        package = _Package(archive)
        roots = [rel for rel in package.relationships("").values()
                 if rel[0] in {f"{REL}/officeDocument", f"{STRICT_REL}/officeDocument"}]
        if len(roots) != 1 or roots[0][1] != "Internal":
            raise ValueError("Expected one internal workbook relationship")
        workbook = roots[0][2]
        book = package.xml(workbook)
        strict = roots[0][0] == f"{STRICT_REL}/officeDocument"
        main, rel = (STRICT_MAIN, STRICT_REL) if strict else (MAIN, REL)
        if book.tag != f"{{{main}}}workbook":
            raise ValueError("Invalid workbook root or namespace/profile mismatch")
        sheet_nodes = book.findall(f"{{{main}}}sheets/{{{main}}}sheet")
        if not sheet_nodes:
            raise ValueError("Workbook has no sheet relationships")
        sheet_types = {
            f"{rel}/{kind}": (kind, f"{{{main}}}{kind}",
                              f"application/vnd.openxmlformats-officedocument.spreadsheetml.{kind}+xml")
            for kind in ("worksheet", "chartsheet", "dialogsheet")
        }
        if not strict:
            for kind, content in (("xlMacrosheet", "macrosheet"), ("xlIntlMacrosheet", "intlmacrosheet")):
                sheet_types[f"http://schemas.microsoft.com/office/2006/relationships/{kind}"] = (
                    kind, "{http://schemas.microsoft.com/office/excel/2006/main}macrosheet",
                    f"application/vnd.ms-excel.{content}+xml")
        sheets = {}
        sheet_parts = {}
        unsupported = set()
        for sheet in sheet_nodes:
            name = sheet.attrib["name"]
            if name in sheet_parts:
                raise ValueError(f"Duplicate sheet name: {name}")
            identity = sheet.attrib[f"{{{rel}}}id"]
            relation = package.relationships(workbook).get(identity)
            if relation is None or relation[1] != "Internal" or relation[0] not in sheet_types:
                raise ValueError(f"Invalid or unresolved sheet relationship: {name}")
            kind, expected_root, expected_type = sheet_types[relation[0]]
            part = relation[2]
            root = package.xml(part)
            if root.tag != expected_root:
                raise ValueError(f"Invalid {kind} root or namespace/profile mismatch: {name}")
            if kind != "worksheet":
                content_type = package.overrides.get(part) or package.defaults.get(posixpath.splitext(part)[1].lstrip("."))
                if content_type != expected_type:
                    raise ValueError(f"Invalid or missing {kind} content type: {name}")
                package.canonical(root, part)
                unsupported.add(kind)
            sheet_parts[name] = (part, root)
        if strict:
            unsupported.add("Strict SpreadsheetML")
        if unsupported:
            raise UnsupportedOOXMLFormat("Inventory is not supported for: " + ", ".join(sorted(unsupported)))
        for name, (part, root) in sheet_parts.items():
            validations, objects = [], []
            for node in root.iter():
                if node.tag in {f"{{{MAIN}}}dataValidation", f"{{{X14}}}dataValidation"}:
                    validations.append({"namespace": node.tag[1:].split("}")[0],
                                        "signature": _digest(package.canonical(node, part)),
                                        "xml": ET.tostring(node, encoding="unicode")})
                if node.tag in {f"{{{MAIN}}}{local}" for local in OBJECT_ELEMENTS}:
                    objects.append({"element": node.tag, "signature": _digest(package.canonical(node, part)),
                                    "xml": ET.tostring(node, encoding="unicode")})
            cell_metadata = [{"cell": node.get("r"), "cm": node.get("cm"), "vm": node.get("vm")}
                             for node in root.iter(f"{{{MAIN}}}c")
                             if node.get("cm") is not None or node.get("vm") is not None]
            sheets[name] = {"part": part, "validations": validations, "objects": objects,
                            "cell_metadata": cell_metadata}
        package_objects = [
            _digest(package.relationship_signature("", identity, ()))
            for identity, relation in package.relationships("").items()
            if relation[0].endswith("/metadata/thumbnail")
        ]
        return {"schema_version": "ooxml_inventory_v1", "sheets": sheets, "package_objects": package_objects,
                "scope": "standard/x14 validation rules and linked worksheet objects; not a fidelity certificate"}


def compare_ooxml(before: str | Path, after: str | Path) -> dict:
    """Report unmatched signatures, preserving multiplicity; assign no pass/fail.

    Changed rules/objects appear as both absent and added signatures. Differences
    need interpretation against the intended operation, particularly for copies,
    insertions, or other operations that legitimately change coordinates.
    """
    original, modified = inspect_ooxml(before), inspect_ooxml(after)
    differences = []
    for sheet in sorted(original["sheets"].keys() | modified["sheets"].keys()):
        for component in ("validations", "objects"):
            left = Counter(item["signature"] for item in original["sheets"].get(sheet, {}).get(component, []))
            right = Counter(item["signature"] for item in modified["sheets"].get(sheet, {}).get(component, []))
            for direction, delta in (("absent_after", left - right), ("added_after", right - left)):
                for signature, count in sorted(delta.items()):
                    differences.append({"sheet": sheet, "component": component, "direction": direction,
                                        "signature": signature, "count": count})
    return {"schema_version": "ooxml_comparison_v1", "differences": differences,
            "sheets_absent_after": sorted(original["sheets"].keys() - modified["sheets"].keys()),
            "sheets_added_after": sorted(modified["sheets"].keys() - original["sheets"].keys()),
            "scope": original["scope"]}
