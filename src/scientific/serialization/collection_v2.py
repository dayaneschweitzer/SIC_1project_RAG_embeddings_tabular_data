"""Serialize structural blocks with the shared tabular v2 contract."""

from __future__ import annotations

from collections.abc import Sequence
from typing import Any

from .collection_contract import (
    SerializedCollection,
    SerializedDocument,
    utf8_sha256,
)
from .tabular_v2 import (
    SERIALIZATION_VERSION,
    TokenCounter,
    serialize_block,
)


def serialize_semantic_blocks_v2(
    blocks: Sequence[dict[str, Any]],
    *,
    count_minilm: TokenCounter,
    count_taberta: TokenCounter,
) -> tuple[SerializedCollection, tuple[dict[str, Any], ...]]:
    """Produce one immutable collection shared by E1 and E2.

    Returns the collection (document_id as block_id) and the raw v2 payloads.
    """
    if not blocks:
        raise ValueError("at least one semantic block is required")

    documents: list[SerializedDocument] = []
    payloads: list[dict[str, Any]] = []
    seen_ids: set[str] = set()
    for block in blocks:
        for payload in serialize_block(
            block,
            count_minilm=count_minilm,
            count_taberta=count_taberta,
        ):
            document_id = str(payload.get("document_id") or "")
            if not document_id:
                raise ValueError("v2 document is missing document_id")
            if document_id in seen_ids:
                raise ValueError(f"duplicated document_id: {document_id!r}")
            seen_ids.add(document_id)
            text = str(payload["serialization_text"])
            documents.append(
                SerializedDocument(
                    block_id=document_id,
                    serialization_version=str(payload["serialization_version"]),
                    serialization_text=text,
                    minilm_token_count=int(payload["minilm_token_count"]),
                    taberta_token_count=int(payload["taberta_token_count"]),
                    truncated=bool(payload["truncated"]),
                    serialization_sha256=utf8_sha256(text),
                )
            )
            payloads.append(payload)

    versions = {document.serialization_version for document in documents}
    if versions != {SERIALIZATION_VERSION}:
        raise ValueError(f"unexpected serialization_version set: {versions}")

    return (
        SerializedCollection(
            serialization_version=SERIALIZATION_VERSION,
            documents=tuple(documents),
        ),
        tuple(payloads),
    )
