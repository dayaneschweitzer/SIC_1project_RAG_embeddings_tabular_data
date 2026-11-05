from __future__ import annotations


AGENT_NAME = "verifier_executor"
REQUIRED_FIELDS = ["block_id", "block_type", "sheet_name", "range", "cells", "traceability"]


def run_verifier_executor_agent(retrieved_evidences: list[dict]) -> dict:
    verified_evidences = []
    discarded_evidences = []
    warnings = []

    checks = {
        "has_block_id": True,
        "has_traceability": True,
        "has_cell_references": True,
        "has_sheet_and_range": True,
    }

    for evidence in retrieved_evidences:
        evidence_warnings = validate_evidence(evidence)

        if evidence_warnings:
            discarded = dict(evidence)
            discarded["discard_reasons"] = evidence_warnings
            discarded_evidences.append(discarded)
            warnings.extend(
                f"{evidence.get('block_id') or 'unknown_block'}: {warning}"
                for warning in evidence_warnings
            )
            _update_checks(checks, evidence)
        else:
            verified_evidences.append(evidence)

    if not retrieved_evidences:
        warnings.append("Nenhuma evidencia foi recuperada.")

    if retrieved_evidences and not verified_evidences:
        verification_status = "failed"
    elif warnings:
        verification_status = "warning"
    else:
        verification_status = "passed"

    if not verified_evidences:
        checks = {key: False for key in checks}

    return {
        "agent": AGENT_NAME,
        "verification_status": verification_status,
        "verified_evidences": verified_evidences,
        "discarded_evidences": discarded_evidences,
        "warnings": warnings,
        "structural_fidelity_checks": checks,
    }


def validate_evidence(evidence: dict) -> list[str]:
    warnings = []

    for field in REQUIRED_FIELDS:
        if field not in evidence or evidence.get(field) in (None, "", []):
            warnings.append(f"campo obrigatorio ausente ou vazio: {field}")

    traceability = evidence.get("traceability")
    if not isinstance(traceability, dict) or not traceability:
        warnings.append("traceability invalido")

    if not evidence.get("sheet_name") or not evidence.get("range"):
        warnings.append("referencia de aba/range ausente")

    if "cells" not in evidence or not isinstance(evidence.get("cells"), list):
        warnings.append("cells ausente ou invalido")

    return _deduplicate(warnings)


def _update_checks(checks: dict, evidence: dict) -> None:
    if not evidence.get("block_id"):
        checks["has_block_id"] = False

    if not evidence.get("traceability"):
        checks["has_traceability"] = False

    if not evidence.get("cells"):
        checks["has_cell_references"] = False

    if not evidence.get("sheet_name") or not evidence.get("range"):
        checks["has_sheet_and_range"] = False


def _deduplicate(values: list[str]) -> list[str]:
    seen = set()
    result = []

    for value in values:
        if value in seen:
            continue
        seen.add(value)
        result.append(value)

    return result
