"""Serialize semantic blocks once; both encoders consume the same UTF-8 strings."""

from __future__ import annotations

import hashlib
from collections.abc import Sequence
from dataclasses import dataclass
from typing import Any

from .tabular_base import (
    SERIALIZATION_VERSION,
    TokenCounter,
    serialize_block,
)


class SerializationDivergenceError(ValueError):
    """E1 and E2 would receive different block ids, texts, or SHA-256 digests."""


@dataclass(frozen=True)
class SerializedDocument:
    block_id: str
    serialization_version: str
    serialization_text: str
    minilm_token_count: int
    taberta_token_count: int
    truncated: bool
    serialization_sha256: str


@dataclass(frozen=True)
class SerializedCollection:
    serialization_version: str
    documents: tuple[SerializedDocument, ...]

    def block_ids(self) -> list[str]:
        return [document.block_id for document in self.documents]

    def texts(self) -> list[str]:
        return [document.serialization_text for document in self.documents]

    def sha256_list(self) -> list[str]:
        return [document.serialization_sha256 for document in self.documents]


def utf8_sha256(text: str) -> str:
    return hashlib.sha256(text.encode("utf-8")).hexdigest()


def serialize_semantic_blocks(
    blocks: Sequence[dict[str, Any]],
    *,
    count_minilm: TokenCounter,
    count_taberta: TokenCounter,
) -> SerializedCollection:
    """Produce one immutable collection shared by E1 and E2.

    Token counters must already be bound to the MiniLM and TABERTA tokenizers.
    """
    if not blocks:
        raise ValueError("at least one semantic block is required")

    documents: list[SerializedDocument] = []
    seen_ids: set[str] = set()
    for block in blocks:
        payload = serialize_block(
            block,
            count_minilm=count_minilm,
            count_taberta=count_taberta,
        )
        block_id = str(payload["source_block_id"] or "")
        if not block_id:
            raise ValueError("semantic block is missing block_id")
        if block_id in seen_ids:
            raise ValueError(f"duplicated block_id: {block_id!r}")
        seen_ids.add(block_id)
        text = str(payload["serialization_text"])
        documents.append(
            SerializedDocument(
                block_id=block_id,
                serialization_version=str(payload["serialization_version"]),
                serialization_text=text,
                minilm_token_count=int(payload["minilm_token_count"]),
                taberta_token_count=int(payload["taberta_token_count"]),
                truncated=bool(payload["truncated"]),
                serialization_sha256=utf8_sha256(text),
            )
        )

    versions = {document.serialization_version for document in documents}
    if versions != {SERIALIZATION_VERSION}:
        raise ValueError(f"unexpected serialization_version set: {versions}")

    return SerializedCollection(
        serialization_version=SERIALIZATION_VERSION,
        documents=tuple(documents),
    )


def require_identical_serialization(
    left: SerializedCollection,
    right: SerializedCollection,
) -> None:
    """Reject any E1/E2 split that would encode different strings or order."""
    if left.serialization_version != right.serialization_version:
        raise SerializationDivergenceError("serialization_version diverges between E1 and E2")
    if left.block_ids() != right.block_ids():
        raise SerializationDivergenceError("block_id sequence diverges between E1 and E2")
    if left.texts() != right.texts():
        raise SerializationDivergenceError("serialization_text diverges between E1 and E2")
    if left.sha256_list() != right.sha256_list():
        raise SerializationDivergenceError("serialization_sha256 diverges between E1 and E2")
    for left_text, left_sha in zip(left.texts(), left.sha256_list()):
        if utf8_sha256(left_text) != left_sha:
            raise SerializationDivergenceError("serialization_sha256 does not match UTF-8 text")
