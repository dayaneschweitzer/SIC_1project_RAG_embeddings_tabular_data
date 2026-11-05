from __future__ import annotations

import re
from typing import Any

from .intent_patterns import match_catalog
from .parsed_intent import (
    PARSE_AMBIGUOUS,
    PARSE_PARSED,
    PARSE_UNSUPPORTED,
    PARSER_NAME,
    UNSUPPORTED,
    UPDATE_CELL,
    ParsedModificationIntent,
)


def parse_modification_request(
    request: str,
    snapshot: dict[str, Any] | None = None,
    evidences: list[dict[str, Any]] | None = None,
) -> dict[str, Any]:
    """Parse a natural-language modification request.

    Contextual snapshot/evidence may be supplied for later resolution, but the
    operation is decided from the instruction text. Benchmark labels, masks,
    oracle/golden, and expected cells are not parser inputs.
    """
    del snapshot, evidences
    normalized = normalize_request(request)
    catalog_hits = match_catalog(request, normalized)
    if len(catalog_hits) == 1:
        return catalog_hits[0].to_dict()
    if len(catalog_hits) > 1:
        operations = {(hit.operation, hit.expression) for hit in catalog_hits}
        if len(operations) == 1:
            return catalog_hits[0].to_dict()
        ambiguous = ParsedModificationIntent(
            request=request,
            normalized_request=normalized,
            operation=UNSUPPORTED,
            parse_status=PARSE_AMBIGUOUS,
            confidence="low",
            warnings=["Instruction matched more than one distinct operation."],
            parser=PARSER_NAME,
        )
        return ambiguous.to_dict()

    return _parse_legacy_update_cell(request, normalized).to_dict()


def detect_operation(
    normalized_request: str,
    target_coordinate: str | None,
    target_value: str | None,
    new_value: str | None,
) -> str:
    modification_terms = [
        "alterar",
        "altere",
        "mudar",
        "mude",
        "renomear",
        "renomeie",
        "substituir",
        "substitua",
        "corrigir",
        "preencher",
        "set",
        "change",
        "rename",
        "replace",
        "update",
    ]

    has_term = any(term in normalized_request.lower() for term in modification_terms)
    if has_term and new_value and (target_coordinate or target_value):
        return UPDATE_CELL

    return UNSUPPORTED


def normalize_request(request: str) -> str:
    return " ".join(str(request).strip().split())


def extract_coordinate(text: str) -> str | None:
    match = re.search(r"\b([A-Z]{1,3}[1-9][0-9]{0,6})\b", text.upper())
    if not match:
        return None

    return match.group(1)


def extract_sheet_name(text: str) -> str | None:
    match = re.search(
        r"(?:na\s+aba|aba|in\s+sheet|sheet)\s+(.+?)\s*,\s*"
        r"(?:altere|mude|renomeie|substitua|change|update|replace|rename)",
        text,
        flags=re.IGNORECASE,
    )
    return match.group(1).strip() if match else None


def extract_value_replacement(text: str) -> tuple[str | None, str | None]:
    quoted_pair = re.search(
        r"['\"]([^'\"]+)['\"]\s+(?:to|para|por|as|como)\s+['\"]([^'\"]+)['\"]",
        text,
        flags=re.IGNORECASE,
    )
    if quoted_pair:
        return quoted_pair.group(1).strip(), quoted_pair.group(2).strip()

    replace_pattern = re.search(
        r"(?:rename|renomear|change|alterar|replace|substituir|update|corrigir)"
        r"(?:\s+(?:field|campo|header|cabecalho|column|coluna|cell|celula))?"
        r"\s+(.+?)\s+(?:to|para|por|as|como)\s+(.+)$",
        text,
        flags=re.IGNORECASE,
    )
    if replace_pattern:
        target = _clean_value_fragment(replace_pattern.group(1))
        new_value = _clean_value_fragment(replace_pattern.group(2))
        return target or None, new_value or None

    set_coordinate_pattern = re.search(
        r"(?:set|preencher|alterar|change|update)\s+([A-Z]{1,3}[1-9][0-9]{0,6})"
        r"\s+(?:to|para|por|as|como)\s+(.+)$",
        text,
        flags=re.IGNORECASE,
    )
    if set_coordinate_pattern:
        return set_coordinate_pattern.group(1).upper(), _clean_value_fragment(set_coordinate_pattern.group(2))

    return None, None


def _parse_legacy_update_cell(request: str, normalized: str) -> ParsedModificationIntent:
    target_sheet = extract_sheet_name(normalized)
    target_coordinate = extract_coordinate(normalized)
    target_value, new_value = extract_value_replacement(normalized)
    operation = detect_operation(normalized, target_coordinate, target_value, new_value)

    warnings: list[str] = []
    parse_status = PARSE_PARSED
    if operation == UNSUPPORTED:
        parse_status = PARSE_UNSUPPORTED
        warnings.append("Nao foi possivel identificar uma operacao de modificacao segura.")

    if operation == UPDATE_CELL and not new_value:
        warnings.append("Novo valor nao identificado na solicitacao.")

    confidence = "medium" if operation == UPDATE_CELL and new_value else "low"
    return ParsedModificationIntent(
        request=request,
        normalized_request=normalized,
        operation=operation,
        parse_status=parse_status,
        target_sheet=target_sheet,
        destination=target_coordinate,
        target_coordinate=target_coordinate,
        target_value=target_value,
        new_value=new_value,
        literal_value=new_value if operation == UPDATE_CELL else None,
        replacement_old=target_value if operation == UPDATE_CELL else None,
        replacement_new=new_value if operation == UPDATE_CELL else None,
        confidence=confidence,
        warnings=warnings,
        parser=PARSER_NAME,
    )


def _clean_value_fragment(value: str) -> str:
    cleaned = str(value).strip().strip("'\"")
    cleaned = re.sub(r"[.;,]+$", "", cleaned).strip()
    return cleaned
