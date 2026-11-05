from __future__ import annotations

import math


def cosine_similarity(left: list[float], right: list[float]) -> float:
    if len(left) != len(right):
        raise ValueError("vectors must have the same dimension")
    dot = sum(a * b for a, b in zip(left, right))
    left_norm = math.sqrt(sum(a * a for a in left))
    right_norm = math.sqrt(sum(b * b for b in right))
    if left_norm == 0.0 or right_norm == 0.0:
        return 0.0
    return dot / (left_norm * right_norm)


def l2_norm(vector: list[float]) -> float:
    return math.sqrt(sum(value * value for value in vector))


def top_k(scored: list[dict], k: int) -> list[dict]:
    ordered = sorted(
        scored,
        key=lambda item: (-float(item["score"]), str(item.get("candidate_id") or "")),
    )
    ranked = []
    for rank, item in enumerate(ordered[:k], start=1):
        ranked.append({**item, "rank": rank})
    return ranked
