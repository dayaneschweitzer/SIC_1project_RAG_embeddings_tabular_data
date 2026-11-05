"""Raw OOXML observations judged against an explicit preservation contract.

An operation label never authorizes a change. PASS applies to declared,
covered properties only; full workbook fidelity remains outside this method.
"""

from collections import Counter
from hashlib import sha256
import json
from pathlib import Path
import xml.etree.ElementTree as ET
from zipfile import BadZipFile, ZipFile

from .ooxml_inspection import MAIN, UnsupportedOOXMLFormat, inspect_ooxml


NEUTRAL_WRITES = frozenset({"update_cell", "write_value", "write_formula", "replace_values"})
PROPERTIES = ("validation_rules", "linked_objects", "panes", "sheet_views", "conditional_format_rules")
NOT_VERIFIED = "NOT_VERIFIED"
PASS = "PASS"
FAIL = "FAIL"
INSPECTION_ERROR = "INSPECTION_ERROR"
STATES = frozenset({PASS, FAIL, NOT_VERIFIED, INSPECTION_ERROR})
UNCOVERED = (
    "task_and_decision_correctness", "cell_values_and_formulas", "styles_and_differential_styles",
    "tables_and_defined_names", "merges_and_dimensions", "calculation_and_cached_values", "unknown_extensions",
    "unreferenced_parts", "visual_equivalence", "full_workbook_fidelity",
)


def _file_identity(path):
    digest = sha256()
    with path.open("rb") as handle:
        for block in iter(lambda: handle.read(1024 * 1024), b""):
            digest.update(block)
    return {"path": str(path), "sha256": digest.hexdigest()}


def _xml_signature(node):
    def content(element):
        text = element.text
        if len(element) and text is not None and not text.strip():
            text = None
        return (element.tag, sorted(element.attrib.items()), text,
                [(content(child), child.tail if child.tail and child.tail.strip() else None)
                 for child in element])
    encoded = json.dumps(content(node), ensure_ascii=False, separators=(",", ":")).encode("utf8")
    return sha256(encoded).hexdigest()


def _inventory(path):
    inventory = inspect_ooxml(path)
    sheets = {}
    with ZipFile(path, "r") as archive:
        for name, sheet in inventory["sheets"].items():
            root = ET.fromstring(archive.read(sheet["part"]))
            sheets[name] = {
                "validation_rules": [item["signature"] for item in sheet["validations"]],
                "linked_objects": [item["signature"] for item in sheet["objects"]],
                "panes": [_xml_signature(node) for node in root.iter(f"{{{MAIN}}}pane")],
                "sheet_views": [_xml_signature(node) for node in root.findall(f"{{{MAIN}}}sheetViews")],
                "conditional_format_rules": [_xml_signature(node)
                                             for node in root.findall(f"{{{MAIN}}}conditionalFormatting")],
            }
    return sheets


def aggregate_states(states) -> str:
    values = list(states)
    if any(value not in STATES for value in values):
        raise ValueError("Unknown verification state")
    if FAIL in values:
        return FAIL
    if INSPECTION_ERROR in values:
        return INSPECTION_ERROR
    if not values or NOT_VERIFIED in values:
        return NOT_VERIFIED
    return PASS


def make_ooxml_contract(before, *, authority, sheet_mapping=None, pending_sheets=()):
    """Bind explicit XML invariants to input bytes; no task inference is done here.

    External callers supply mappings from an independent task contract. The
    executor uses its own backend policy, which is not a scientific authority.
    """
    inventory = _inventory(Path(before))
    mapping = dict(sheet_mapping) if sheet_mapping is not None else {name: name for name in inventory}
    return {
        "schema_version": "ooxml_preservation_contract_v1",
        "authority": authority,
        "input_sha256": _file_identity(Path(before))["sha256"],
        "expected_sheets": list(mapping),
        "requirements": [
            {"sheet": target, "source_sheet": source, "property": prop,
             "mode": "pending" if target in pending_sheets else "exact"}
            for target, source in mapping.items() for prop in PROPERTIES
        ] + [{"scope": "package", "property": "package_linked_objects", "mode": "exact"}],
    }


def verify_ooxml_v2(before: str | Path, after: str | Path, *, operation=None, contract=None) -> dict:
    """Judge required signatures, retaining observations and coverage separately.

    Exact mode requires the canonical XML/content multiset, not visual
    equivalence. Pending mode has no approved transformation and cannot PASS.
    Missing artifacts have no judgment; malformed artifacts are inspection
    errors. A FAIL is never erased by another property's missing coverage.
    """
    paths = {"before": Path(before), "after": Path(after)}
    report = {
        "schema_version": "ooxml_verifier_v2",
        "operation": operation,
        "contract": contract,
        "overall_status": NOT_VERIFIED,
        "covered_status": NOT_VERIFIED,
        "checks": [],
        "observations": [],
        "not_verified_properties": list(UNCOVERED),
        "identities": {},
    }
    missing = [name for name, path in paths.items() if not path.is_file()]
    if missing:
        report["reason"] = "ARTIFACT_NOT_AVAILABLE"
        report["missing_artifacts"] = missing
        return report
    try:
        identities = {name: _file_identity(path) for name, path in paths.items()}
        report["identities"] = identities
        original, modified = _inventory(paths["before"]), _inventory(paths["after"])
        package_before = inspect_ooxml(paths["before"])["package_objects"]
        package_after = inspect_ooxml(paths["after"])["package_objects"]
        if Counter(package_before) != Counter(package_after):
            report["observations"].append({"scope": "package", "property": "package_linked_objects",
                                           "removed": dict(Counter(package_before) - Counter(package_after)),
                                           "added": dict(Counter(package_after) - Counter(package_before))})
        for sheet in sorted(original.keys() | modified.keys()):
            present_before, present_after = sheet in original, sheet in modified
            if present_before != present_after:
                report["observations"].append({"sheet": sheet, "property": "sheet_presence",
                                               "before": present_before, "after": present_after})
            for property_name in PROPERTIES:
                left = Counter(original.get(sheet, {}).get(property_name, []))
                right = Counter(modified.get(sheet, {}).get(property_name, []))
                removed, added = left - right, right - left
                observation = {"sheet": sheet, "property": property_name, "before_count": sum(left.values()),
                               "after_count": sum(right.values()), "removed": dict(sorted(removed.items())),
                               "added": dict(sorted(added.items()))}
                if removed or added:
                    report["observations"].append(observation)
        if contract is None:
            report["checks"] = [{"sheet": sheet, "property": prop, "status": NOT_VERIFIED,
                                  "reason": "INDEPENDENT_CONTRACT_NOT_PROVIDED"}
                                 for sheet in sorted(original.keys() | modified.keys()) for prop in PROPERTIES]
        else:
            _judge_contract(report, contract, original, modified, identities["before"]["sha256"], package_before, package_after)
        final_identities = {name: _file_identity(path) for name, path in paths.items()}
        if final_identities != identities:
            report["overall_status"] = INSPECTION_ERROR
            report["covered_status"] = INSPECTION_ERROR
            report["reason"] = "ARTIFACT_CHANGED_DURING_INSPECTION"
            report["checks"] = []
            report["observations"] = []
            report["final_identities"] = final_identities
        else:
            report["covered_status"] = aggregate_states(item["status"] for item in report["checks"])
            report["overall_status"] = aggregate_states([report["covered_status"], NOT_VERIFIED])
            report["reason"] = "PARTIAL_COVERAGE_ONLY"
    except UnsupportedOOXMLFormat as exc:
        report["reason"] = "FORMAT_NOT_SUPPORTED"
        report["detail"] = str(exc)
        report["checks"] = []
        report["observations"] = []
    except (OSError, ValueError, KeyError, ET.ParseError, BadZipFile, RecursionError) as exc:
        report["overall_status"] = INSPECTION_ERROR
        report["covered_status"] = INSPECTION_ERROR
        report["reason"] = type(exc).__name__
        report["error"] = str(exc)
        report["checks"] = []
        report["observations"] = []
    return report


def _judge_contract(report, contract, original, modified, input_hash, package_before, package_after):
    if (not isinstance(contract, dict)
            or contract.get("schema_version") != "ooxml_preservation_contract_v1"
            or not contract.get("authority") or contract.get("input_sha256") != input_hash):
        raise ValueError("Invalid preservation contract or input binding")
    expected = contract.get("expected_sheets")
    if expected is not None:
        report["checks"].append({"property": "sheet_presence",
                                 "status": PASS if set(expected) == set(modified) else FAIL,
                                 "reason": "EXPLICIT_SHEET_SET", "expected": expected,
                                 "observed": sorted(modified)})
    requirements = contract.get("requirements")
    if not isinstance(requirements, list) or not requirements:
        raise ValueError("Preservation contract has no requirements")
    for rule in requirements:
        if rule.get("scope") == "package":
            covered = rule.get("property") == "package_linked_objects" and rule.get("mode") == "exact"
            status = (PASS if Counter(package_before) == Counter(package_after) else FAIL) if covered else NOT_VERIFIED
            report["checks"].append({"scope": "package", "property": rule.get("property"), "status": status,
                                     "reason": "REQUIRED_PACKAGE_OBJECT_SIGNATURES" if covered else "PROPERTY_NOT_COVERED"})
            continue
        target, source, prop = rule["sheet"], rule["source_sheet"], rule["property"]
        check = {"sheet": target, "source_sheet": source, "property": prop}
        if source not in original:
            raise ValueError(f"Contract source sheet absent: {source}")
        if target not in modified:
            status, reason = FAIL, "REQUIRED_SHEET_MISSING"
        elif prop not in PROPERTIES:
            status, reason = NOT_VERIFIED, "PROPERTY_NOT_COVERED"
        elif prop == "validation_rules" and rule.get("mode") == "insert_column_chain_v2":
            from .insert_column_audit import verify_insert_column
            transitions = contract.get("insertion_transitions", [])
            previous = input_hash
            valid = bool(transitions)
            for transition in transitions:
                first, last = Path(transition["before"]), Path(transition["after"])
                valid = valid and _file_identity(first)["sha256"] == previous
                judged = verify_insert_column(first, last, transition["sheet"], transition["position"])
                valid = valid and judged["covered_status"] == PASS
                previous = _file_identity(last)["sha256"]
            valid = valid and previous == report["identities"]["after"]["sha256"]
            status, reason = (PASS, "INDEPENDENT_INSERTION_TRANSFORM_VERIFIED") if valid else (FAIL, "INSERTION_TRANSFORM_OR_CHAIN_INVALID")
        elif rule.get("mode") != "exact":
            status, reason = NOT_VERIFIED, "TRANSFORMATION_CONTRACT_PENDING"
        else:
            left, right = Counter(original[source][prop]), Counter(modified[target][prop])
            status, reason = (PASS, "REQUIRED_SIGNATURES_EQUAL") if left == right else (FAIL, "REQUIRED_XML_INVARIANT_CHANGED")
            check.update(removed=dict(left - right), added=dict(right - left))
        check.update(status=status, reason=reason)
        report["checks"].append(check)
