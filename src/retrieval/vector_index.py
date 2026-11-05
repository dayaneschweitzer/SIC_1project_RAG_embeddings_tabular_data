from __future__ import annotations

from pathlib import Path
import json
import math


SIMILARITY_METRIC = "cosine"
INDEX_STRATEGY = "numpy_cosine_similarity"


class VectorIndex:
    def __init__(
        self,
        embeddings: list[list[float]],
        metadata: list[dict],
        similarity_metric: str = SIMILARITY_METRIC,
    ) -> None:
        self.embeddings = _to_list_matrix(embeddings)
        self.metadata = metadata
        self.similarity_metric = similarity_metric

        errors = validate_embeddings_metadata_alignment(self.embeddings, self.metadata)
        if errors:
            raise ValueError("; ".join(errors))

        if self.similarity_metric != SIMILARITY_METRIC:
            raise ValueError(f"Unsupported similarity metric: {self.similarity_metric}")

    def search(self, query_embedding: list[float], top_k: int = 5) -> list[dict]:
        query_vector = [float(value) for value in query_embedding]
        scored_results = []

        for index, embedding in enumerate(self.embeddings):
            score = cosine_similarity(query_vector, embedding)
            scored_results.append(
                {
                    "rank": 0,
                    "score": score,
                    "metadata": self.metadata[index],
                }
            )

        scored_results.sort(
            key=lambda result: (
                -result["score"],
                result["metadata"].get("block_id", ""),
            )
        )

        for rank, result in enumerate(scored_results[:top_k], start=1):
            result["rank"] = rank

        return scored_results[:top_k]


def load_semantic_blocks(path: str) -> dict:
    with open(path, "r", encoding="utf-8") as file:
        return json.load(file)


def metadata_from_semantic_blocks(semantic_blocks: dict) -> list[dict]:
    metadata = []

    for block in semantic_blocks.get("blocks", []):
        metadata.append(
            {
                "block_id": block.get("block_id"),
                "block_type": block.get("block_type"),
                "sheet_name": block.get("sheet_name"),
                "range": block.get("range"),
                "cells": block.get("cells", []),
                "retrieval_text": block.get("retrieval_text", ""),
                "traceability": block.get("traceability", {}),
            }
        )

    return metadata


def validate_embeddings_metadata_alignment(
    embeddings: list[list[float]],
    metadata: list[dict],
) -> list[str]:
    errors = []

    if len(embeddings) != len(metadata):
        errors.append("number of embeddings differs from number of metadata entries")

    dimensions = {len(vector) for vector in embeddings}
    if len(dimensions) > 1:
        errors.append("vectors have inconsistent dimensions")

    if any(_has_nan(vector) for vector in embeddings):
        errors.append("embeddings contain NaN")

    block_ids = [entry.get("block_id") for entry in metadata]
    if len(block_ids) != len(set(block_ids)):
        errors.append("metadata contains duplicated block_id")

    for entry in metadata:
        if not entry.get("block_id"):
            errors.append("metadata entry missing block_id")
            break

        if not entry.get("retrieval_text"):
            errors.append(f"metadata entry missing retrieval_text: {entry.get('block_id')}")
            break

        if not entry.get("traceability"):
            errors.append(f"metadata entry missing traceability: {entry.get('block_id')}")
            break

    return errors


def cosine_similarity(left: list[float], right: list[float]) -> float:
    if len(left) != len(right):
        raise ValueError("vectors must have the same dimension")

    dot_product = sum(a * b for a, b in zip(left, right))
    left_norm = math.sqrt(sum(a * a for a in left))
    right_norm = math.sqrt(sum(b * b for b in right))

    if left_norm == 0 or right_norm == 0:
        return 0.0

    return dot_product / (left_norm * right_norm)


def save_index_artifacts(
    embeddings: list[list[float]],
    metadata: list[dict],
    config: dict,
    output_dir: str,
    basename: str,
) -> dict:
    np = _require_numpy()
    output_path = Path(output_dir)
    output_path.mkdir(parents=True, exist_ok=True)

    embeddings_path = output_path / f"{basename}_embeddings.npy"
    metadata_path = output_path / f"{basename}_embedding_metadata.json"
    config_path = output_path / f"{basename}_index_config.json"

    np.save(str(embeddings_path), np.asarray(embeddings, dtype="float32"))

    with open(metadata_path, "w", encoding="utf-8") as file:
        json.dump(metadata, file, ensure_ascii=False, indent=2)

    with open(config_path, "w", encoding="utf-8") as file:
        json.dump(config, file, ensure_ascii=False, indent=2)

    return {
        "embeddings": str(embeddings_path),
        "metadata": str(metadata_path),
        "config": str(config_path),
    }


def load_index_artifacts(index_dir: str, basename: str) -> tuple[list[list[float]], list[dict], dict]:
    np = _require_numpy()
    index_path = Path(index_dir)
    embeddings_path, metadata_path, config_path = _index_artifact_paths(index_path, basename)

    embeddings = np.load(str(embeddings_path)).astype("float32").tolist()

    with open(metadata_path, "r", encoding="utf-8") as file:
        metadata = json.load(file)

    with open(config_path, "r", encoding="utf-8") as file:
        config = json.load(file)

    return embeddings, metadata, config


def infer_basename_from_semantic_blocks_path(path: str) -> str:
    stem = Path(path).stem
    suffix = "_semantic_blocks"

    if stem.endswith(suffix):
        return stem[:-len(suffix)]

    return stem


def infer_single_basename(index_dir: str) -> str:
    generic_config = Path(index_dir) / "index_config.json"
    if generic_config.exists():
        return ""

    config_files = sorted(Path(index_dir).glob("*_index_config.json"))

    if not config_files:
        raise FileNotFoundError(f"No *_index_config.json found in {index_dir}")

    if len(config_files) > 1:
        names = ", ".join(path.name for path in config_files)
        raise ValueError(f"More than one index config found. Use --basename. Found: {names}")

    return config_files[0].name.removesuffix("_index_config.json")


def _index_artifact_paths(index_path: Path, basename: str) -> tuple[Path, Path, Path]:
    if not basename:
        return (
            index_path / "embeddings.npy",
            index_path / "embedding_metadata.json",
            index_path / "index_config.json",
        )
    return (
        index_path / f"{basename}_embeddings.npy",
        index_path / f"{basename}_embedding_metadata.json",
        index_path / f"{basename}_index_config.json",
    )


def _has_nan(vector: list[float]) -> bool:
    return any(math.isnan(float(value)) for value in vector)


def _to_list_matrix(embeddings) -> list[list[float]]:
    if hasattr(embeddings, "tolist"):
        embeddings = embeddings.tolist()

    return [[float(value) for value in vector] for vector in embeddings]


def _require_numpy():
    try:
        import numpy as np
    except ImportError as error:
        raise RuntimeError(
            "numpy is required to read or write .npy index artifacts. "
            "Install requirements.txt before building or loading the index."
        ) from error

    return np
