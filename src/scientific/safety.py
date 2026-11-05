from __future__ import annotations

from dataclasses import dataclass
from hashlib import sha256
import json
from math import ceil, isfinite
import re
from typing import Any, Mapping

from .retrieval_policy import _stable_copy


SAFETY_SCHEMA_VERSION = "d2_llm_safety_v1"

_FORBIDDEN_KEY_FRAGMENTS = (
    "golden",
    "oracle",
    "reference_action",
    "required_evidence",
    "functional_contract",
    "authorized_change_mask",
    "private_evaluation",
    "api_key",
    "authorization",
    "input_sha256",
    "workbook_id",
    "task_id",
    "official_key",
    "case_id",
    "output_root",
    "source_file",
    "snapshot_file",
)
_CELL_FIELDS = frozenset({"coordinate", "value", "data_type", "is_formula", "number_format"})
_STRUCTURAL_FIELDS = frozenset(
    {
        "max_row",
        "max_column",
        "merged_ranges",
        "merged_cells",
        "source_block_complete",
        "serialized_body_truncated",
        "workbook_content_sheets",
    }
)
_EVIDENCE_FIELDS = frozenset(
    {"rank", "score", "block_id", "block_type", "sheet_name", "range", "cells", "retrieval_text", "structural_metadata"}
)
_TRACEABILITY_DROPPED_CUSTODY_FIELDS = frozenset({"input_sha256"})
_SECRET_PATTERN = re.compile(r"(?:sk-[A-Za-z0-9_-]{12,}|bearer\s+[A-Za-z0-9._-]{12,})", re.IGNORECASE)
_WINDOWS_PATH = re.compile(r"[A-Za-z]:[\\/]")
_UNIX_PATH = re.compile(r"(?:^|\s)/(?:home|Users|var|tmp|opt|etc)/")


class PrivacyViolationError(ValueError):
    """A prospective prompt contains data outside the public decision boundary."""


class BudgetExceededError(RuntimeError):
    """A transport attempt would exceed a prospectively configured budget."""


class ConfigurationMismatchError(RuntimeError):
    """The runtime configuration differs from the required configuration."""


def _canonical_json(value: Any) -> str:
    return json.dumps(value, ensure_ascii=False, sort_keys=True, separators=(",", ":"))


def hash_json(value: Any) -> str:
    return sha256(_canonical_json(value).encode("utf-8")).hexdigest()


def minimize_public_evidence(evidence: Mapping[str, Any]) -> tuple[dict[str, Any], dict[str, Any]]:
    """Return the smallest evidence representation needed by the D2 prompts.

    Provenance remains available through ``block_id``.  Local paths, workbook
    identities, input hashes and arbitrary traceability metadata are never sent.
    """

    if not isinstance(evidence, Mapping):
        raise TypeError("evidence must be a mapping")
    # Drop custody metadata, allowing only a typed input digest.
    # Reject private fields even inside the envelope that will be discarded.
    traceability = evidence.get("traceability")
    if traceability is not None:
        if not isinstance(traceability, Mapping):
            raise TypeError("evidence traceability must be a mapping")
        input_digest = traceability.get("input_sha256")
        if input_digest is not None and (
            not isinstance(input_digest, str)
            or re.fullmatch(r"[0-9a-fA-F]{64}", input_digest) is None
        ):
            raise PrivacyViolationError("traceability input_sha256 must be a SHA-256 digest")
        _reject_forbidden_fields(
            {
                key: item
                for key, item in traceability.items()
                if key not in _TRACEABILITY_DROPPED_CUSTODY_FIELDS
            }
        )
    prompt_bound_evidence = {
        key: item for key, item in evidence.items() if key != "traceability"
    }
    _reject_forbidden_fields(prompt_bound_evidence)
    unknown = sorted(set(evidence) - (_EVIDENCE_FIELDS | {"traceability"}))
    cells = evidence.get("cells") or []
    if not isinstance(cells, (list, tuple)):
        raise TypeError("evidence cells must be a list")
    minimized_cells = []
    for cell in cells:
        if isinstance(cell, str):
            minimized_cells.append({"coordinate": cell})
            continue
        if not isinstance(cell, Mapping):
            raise TypeError("evidence cells must contain strings or mappings")
        _reject_forbidden_fields(cell)
        minimized_cells.append({key: _stable_copy(cell[key]) for key in sorted(_CELL_FIELDS & set(cell))})

    metadata = evidence.get("structural_metadata") or {}
    if not isinstance(metadata, Mapping):
        raise TypeError("structural_metadata must be a mapping")
    _reject_forbidden_fields(metadata)
    minimized_metadata = {key: _stable_copy(metadata[key]) for key in sorted(_STRUCTURAL_FIELDS & set(metadata))}
    result = {
        "rank": evidence.get("rank"),
        "score": evidence.get("score"),
        "block_id": evidence.get("block_id"),
        "block_type": evidence.get("block_type"),
        "sheet_name": evidence.get("sheet_name"),
        "range": evidence.get("range"),
        "cells": minimized_cells,
        "retrieval_text": evidence.get("retrieval_text"),
        "structural_metadata": minimized_metadata,
    }
    _validate_minimized_evidence(result)
    removed = sorted(set(evidence) - set(result))
    report = {
        "schema_version": SAFETY_SCHEMA_VERSION,
        "policy": "strict_public_evidence_minimization_v1",
        "policy_sha256": hash_json(
            {
                "evidence_fields": sorted(_EVIDENCE_FIELDS),
                "cell_fields": sorted(_CELL_FIELDS),
                "structural_fields": sorted(_STRUCTURAL_FIELDS),
                "forbidden_key_fragments": list(_FORBIDDEN_KEY_FRAGMENTS),
            }
        ),
        "block_id": result["block_id"],
        "removed_top_level_fields": removed,
        "unknown_top_level_fields_rejected": unknown,
        "traceability_sent": False,
        "status": "PASS",
    }
    if unknown:
        raise PrivacyViolationError("unexpected evidence fields are not authorized for external prompts")
    return result, report


def _validate_minimized_evidence(value: dict[str, Any]) -> None:
    if set(value) != _EVIDENCE_FIELDS:
        raise PrivacyViolationError("minimized evidence schema mismatch")
    if not isinstance(value["block_id"], str) or not value["block_id"]:
        raise PrivacyViolationError("public evidence requires a stable block_id")
    if not isinstance(value["sheet_name"], str) or not value["sheet_name"]:
        raise PrivacyViolationError("public evidence requires sheet_name")
    for item in value["cells"]:
        if set(item) - _CELL_FIELDS:
            raise PrivacyViolationError("unauthorized cell metadata")
    if set(value["structural_metadata"]) - _STRUCTURAL_FIELDS:
        raise PrivacyViolationError("unauthorized structural metadata")


def _reject_forbidden_fields(value: Any) -> None:
    if isinstance(value, Mapping):
        for key, item in value.items():
            name = str(key).lower()
            if any(fragment in name for fragment in _FORBIDDEN_KEY_FRAGMENTS):
                raise PrivacyViolationError(f"forbidden private or sensitive field: {key}")
            _reject_forbidden_fields(item)
    elif isinstance(value, (list, tuple)):
        for item in value:
            _reject_forbidden_fields(item)


class PromptPrivacyGuard:
    """Validates prospective API messages without persisting their content."""

    def inspect(self, messages: list[dict[str, str]]) -> dict[str, Any]:
        for message in messages:
            content = message["content"]
            if _SECRET_PATTERN.search(content):
                raise PrivacyViolationError("credential-like content detected in prompt")
            if message["role"] != "user":
                continue
            try:
                payload = json.loads(content)
            except json.JSONDecodeError:
                payload = content
            self._inspect_value(payload)
        return {
            "schema_version": SAFETY_SCHEMA_VERSION,
            "policy": "prompt_privacy_guard_v1",
            "message_count": len(messages),
            "prompt_sha256": hash_json(messages),
            "status": "PASS",
        }

    def _inspect_value(self, value: Any) -> None:
        if isinstance(value, Mapping):
            for key, item in value.items():
                name = str(key).lower()
                if any(fragment in name for fragment in _FORBIDDEN_KEY_FRAGMENTS):
                    raise PrivacyViolationError(f"forbidden prompt field: {key}")
                self._inspect_value(item)
        elif isinstance(value, (list, tuple)):
            for item in value:
                self._inspect_value(item)
        elif isinstance(value, str):
            if _SECRET_PATTERN.search(value) or _WINDOWS_PATH.search(value) or _UNIX_PATH.search(value):
                raise PrivacyViolationError("secret or local filesystem path detected in prompt")


def estimate_message_tokens(messages: list[dict[str, str]]) -> int:
    """Conservative tokenizer-independent estimate used only for budget gating."""

    encoded_bytes = len(_canonical_json(messages).encode("utf-8"))
    return max(1, ceil(encoded_bytes / 3) + 16 * len(messages))


@dataclass(frozen=True)
class PricingTable:
    version: str
    input_usd_per_million_tokens: float
    output_usd_per_million_tokens: float

    def __post_init__(self) -> None:
        if not self.version:
            raise ValueError("pricing version is required")
        for value in (self.input_usd_per_million_tokens, self.output_usd_per_million_tokens):
            if isinstance(value, bool) or not isinstance(value, (int, float)) or not isfinite(value) or value < 0:
                raise ValueError("pricing values must be finite and nonnegative")

    def cost(self, input_tokens: int, output_tokens: int) -> float:
        return (
            input_tokens * self.input_usd_per_million_tokens
            + output_tokens * self.output_usd_per_million_tokens
        ) / 1_000_000

    def to_dict(self) -> dict[str, Any]:
        return {
            "version": self.version,
            "input_usd_per_million_tokens": self.input_usd_per_million_tokens,
            "output_usd_per_million_tokens": self.output_usd_per_million_tokens,
        }


@dataclass(frozen=True)
class BudgetPolicy:
    pricing: PricingTable
    max_total_tokens: int
    max_cost_usd: float
    max_transport_attempts: int

    def __post_init__(self) -> None:
        if type(self.max_total_tokens) is not int or self.max_total_tokens <= 0:
            raise ValueError("max_total_tokens must be positive")
        if isinstance(self.max_cost_usd, bool) or self.max_cost_usd <= 0:
            raise ValueError("max_cost_usd must be positive")
        if type(self.max_transport_attempts) is not int or self.max_transport_attempts <= 0:
            raise ValueError("max_transport_attempts must be positive")

    def to_dict(self) -> dict[str, Any]:
        return {
            "pricing": self.pricing.to_dict(),
            "max_total_tokens": self.max_total_tokens,
            "max_cost_usd": self.max_cost_usd,
            "max_transport_attempts": self.max_transport_attempts,
        }


class BudgetLedger:
    """Conservative per-attempt ledger; failed retries retain their reservation."""

    def __init__(self, policy: BudgetPolicy) -> None:
        self.policy = policy
        self._attempts: list[dict[str, Any]] = []

    def reserve_attempt(self, *, estimated_input_tokens: int, output_reserve_tokens: int) -> int:
        if type(estimated_input_tokens) is not int or estimated_input_tokens <= 0:
            raise ValueError("estimated_input_tokens must be positive")
        if type(output_reserve_tokens) is not int or output_reserve_tokens <= 0:
            raise ValueError("output_reserve_tokens must be positive")
        candidate = self._attempts + [
            {
                "estimated_input_tokens": estimated_input_tokens,
                "output_reserve_tokens": output_reserve_tokens,
                "actual_input_tokens": None,
                "actual_output_tokens": None,
                "status": "reserved",
            }
        ]
        projection = self._totals(candidate)
        if len(candidate) > self.policy.max_transport_attempts:
            raise BudgetExceededError("transport attempt budget exceeded")
        if projection["conservative_total_tokens"] > self.policy.max_total_tokens:
            raise BudgetExceededError("token budget exceeded before transport")
        if projection["conservative_cost_usd"] > self.policy.max_cost_usd:
            raise BudgetExceededError("cost budget exceeded before transport")
        self._attempts = candidate
        return len(self._attempts) - 1

    def record_failure(self, reservation_id: int, *, error_type: str) -> None:
        attempt = self._attempts[reservation_id]
        attempt["status"] = "failed"
        attempt["error_type"] = error_type

    def record_success(self, reservation_id: int, *, input_tokens: int | None, output_tokens: int | None) -> None:
        attempt = self._attempts[reservation_id]
        if type(input_tokens) is int and input_tokens >= 0:
            attempt["actual_input_tokens"] = input_tokens
        if type(output_tokens) is int and output_tokens >= 0:
            attempt["actual_output_tokens"] = output_tokens
        attempt["status"] = "succeeded"

    def snapshot(self) -> dict[str, Any]:
        totals = self._totals(self._attempts)
        return {
            "schema_version": "d2_budget_ledger_v1",
            "policy": self.policy.to_dict(),
            "policy_sha256": hash_json(self.policy.to_dict()),
            "transport_attempt_count": len(self._attempts),
            "attempts": _stable_copy(self._attempts),
            **totals,
            "status": "WITHIN_BUDGET",
        }

    def _totals(self, attempts: list[dict[str, Any]]) -> dict[str, Any]:
        conservative_input = sum(
            max(item["estimated_input_tokens"], item["actual_input_tokens"] or 0) for item in attempts
        )
        conservative_output = sum(
            max(item["output_reserve_tokens"], item["actual_output_tokens"] or 0) for item in attempts
        )
        actual_input = sum(item["actual_input_tokens"] or 0 for item in attempts)
        actual_output = sum(item["actual_output_tokens"] or 0 for item in attempts)
        return {
            "actual_input_tokens": actual_input,
            "actual_output_tokens": actual_output,
            "actual_total_tokens": actual_input + actual_output,
            "actual_cost_usd": self.policy.pricing.cost(actual_input, actual_output),
            "conservative_input_tokens": conservative_input,
            "conservative_output_tokens": conservative_output,
            "conservative_total_tokens": conservative_input + conservative_output,
            "conservative_cost_usd": self.policy.pricing.cost(conservative_input, conservative_output),
        }
