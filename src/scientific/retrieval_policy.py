from __future__ import annotations

from copy import deepcopy
from dataclasses import dataclass, field
from math import isfinite


RETRIEVAL_POLICY_VERSION = "retrieval_policy_v1"


def _validate_top_k(top_k: int) -> None:
    if isinstance(top_k, bool) or not isinstance(top_k, int):
        raise TypeError("top_k must be an integer")
    if top_k <= 0:
        raise ValueError("top_k must be positive")


def _validate_number(value: int | float, name: str) -> None:
    if isinstance(value, bool) or not isinstance(value, (int, float)):
        raise TypeError(f"{name} must be an int or float")
    if isinstance(value, float) and not isfinite(value):
        raise ValueError(f"{name} must be finite")


def _stable_copy(value):
    if isinstance(value, dict):
        return {key: _stable_copy(value[key]) for key in sorted(value)}
    if isinstance(value, list):
        return [_stable_copy(item) for item in value]
    if isinstance(value, tuple):
        return tuple(_stable_copy(item) for item in value)
    return deepcopy(value)


@dataclass(frozen=True)
class RetrievalPolicyResult:
    top_k: int
    threshold: int | float
    candidates: tuple[dict, ...]
    accepted: tuple[dict, ...] = field(init=False)
    rejected: tuple[dict, ...] = field(init=False)
    schema_version: str = field(default=RETRIEVAL_POLICY_VERSION, init=False)

    def __post_init__(self) -> None:
        _validate_top_k(self.top_k)
        _validate_number(self.threshold, "threshold")
        if not isinstance(self.candidates, (list, tuple)):
            raise TypeError("candidates must be a list or tuple")
        if len(self.candidates) > self.top_k:
            raise ValueError("candidates must not exceed top_k")
        for candidate in self.candidates:
            if not isinstance(candidate, dict):
                raise TypeError("each search result must be a dict")
            if "score" not in candidate:
                raise ValueError("each search result must contain score")
            _validate_number(candidate["score"], "score")
            if not isinstance(candidate.get("metadata", {}), dict):
                raise TypeError("search result metadata must be a dict")
        candidates = tuple(_stable_copy(candidate) for candidate in self.candidates)
        object.__setattr__(self, "candidates", candidates)
        object.__setattr__(self, "accepted", tuple(candidate for candidate in candidates if candidate["score"] >= self.threshold))
        object.__setattr__(self, "rejected", tuple(candidate for candidate in candidates if candidate["score"] < self.threshold))

    def to_dict(self) -> dict:
        return {
            "schema_version": self.schema_version,
            "top_k": self.top_k,
            "threshold": self.threshold,
            "candidates": [_stable_copy(candidate) for candidate in self.candidates],
            "accepted": [_stable_copy(candidate) for candidate in self.accepted],
            "rejected": [_stable_copy(candidate) for candidate in self.rejected],
        }


def apply_retrieval_policy(
    search_results: list[dict],
    *,
    top_k: int,
    threshold: int | float,
) -> RetrievalPolicyResult:
    _validate_top_k(top_k)
    if not isinstance(search_results, (list, tuple)):
        raise TypeError("search_results must be a list or tuple")
    return RetrievalPolicyResult(top_k=top_k, threshold=threshold, candidates=tuple(search_results[:top_k]))
