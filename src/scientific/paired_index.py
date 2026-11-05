"""Dimension-locked indexes. Never mix 384-d and 768-d vectors."""

from __future__ import annotations

from .encoder_specs import spec_for
from .ranking import cosine_similarity, top_k


class MixedDimensionError(ValueError):
    """Raised when vectors of different dimensions would share an index."""


class ExperimentalIndex:
    def __init__(
        self,
        *,
        encoder_id: str,
        embeddings: list[list[float]],
        metadata: list[dict],
    ) -> None:
        spec = spec_for(encoder_id)
        if len(embeddings) != len(metadata):
            raise ValueError("embeddings and metadata must have the same length")
        if not embeddings:
            raise ValueError("index requires at least one vector")
        dimensions = {len(vector) for vector in embeddings}
        if len(dimensions) != 1:
            raise MixedDimensionError(
                f"{encoder_id} index received mixed vector dimensions: {sorted(dimensions)}"
            )
        observed = dimensions.pop()
        if observed != spec.dimension:
            raise MixedDimensionError(
                f"{encoder_id} index expected dimension {spec.dimension}, got {observed}"
            )
        self.encoder_id = encoder_id
        self.dimension = spec.dimension
        self.embeddings = embeddings
        self.metadata = metadata

    def search(self, query_embedding: list[float], k: int) -> list[dict]:
        if len(query_embedding) != self.dimension:
            raise MixedDimensionError(
                f"query dimension {len(query_embedding)} does not match "
                f"{self.encoder_id} index dimension {self.dimension}"
            )
        scored = []
        for vector, item in zip(self.embeddings, self.metadata):
            scored.append(
                {
                    "candidate_id": item.get("block_id"),
                    "score": cosine_similarity(query_embedding, vector),
                    "metadata": item,
                }
            )
        return top_k(scored, k)


def build_paired_indexes(
    *,
    e1_embeddings: list[list[float]],
    e2_embeddings: list[list[float]],
    metadata: list[dict],
) -> tuple[ExperimentalIndex, ExperimentalIndex]:
    e1_index = ExperimentalIndex(
        encoder_id="E1_MINILM",
        embeddings=e1_embeddings,
        metadata=metadata,
    )
    e2_index = ExperimentalIndex(
        encoder_id="E2_TABERTA",
        embeddings=e2_embeddings,
        metadata=metadata,
    )
    if e1_index.dimension == e2_index.dimension:
        raise MixedDimensionError("E1 and E2 indexes must not share a dimension")
    return e1_index, e2_index
