"""Static E1/E2 specs for the paired embedding protocol. No model download."""

from __future__ import annotations

from dataclasses import dataclass

from .encoder_constants import (
    E1_KEY,
    E2_KEY,
    MINILM_DIMENSION,
    MINILM_MAX_SEQUENCE_LENGTH,
    MINILM_MODEL_ID,
    MINILM_REVISION,
    TABERTA_DIMENSION,
    TABERTA_MAX_SEQUENCE_LENGTH,
    TABERTA_REPO_ID,
    TABERTA_REVISION,
    TABERTA_VARIANT,
)


@dataclass(frozen=True)
class EncoderSpec:
    encoder_id: str
    model_id: str
    revision: str
    dimension: int
    max_sequence_length: int
    repo_id: str | None = None
    variant: str | None = None


E1_MINILM = EncoderSpec(
    encoder_id=E1_KEY,
    model_id=MINILM_MODEL_ID,
    revision=MINILM_REVISION,
    dimension=MINILM_DIMENSION,
    max_sequence_length=MINILM_MAX_SEQUENCE_LENGTH,
    repo_id=MINILM_MODEL_ID,
    variant=None,
)

E2_TABERTA = EncoderSpec(
    encoder_id=E2_KEY,
    model_id=TABERTA_REPO_ID,
    revision=TABERTA_REVISION,
    dimension=TABERTA_DIMENSION,
    max_sequence_length=TABERTA_MAX_SEQUENCE_LENGTH,
    repo_id=TABERTA_REPO_ID,
    variant=TABERTA_VARIANT,
)


def encoder_registry() -> dict[str, EncoderSpec]:
    return {
        E1_MINILM.encoder_id: E1_MINILM,
        E2_TABERTA.encoder_id: E2_TABERTA,
    }


def spec_for(encoder_id: str) -> EncoderSpec:
    registry = encoder_registry()
    if encoder_id not in registry:
        raise KeyError(f"unknown experimental encoder: {encoder_id!r}")
    return registry[encoder_id]
