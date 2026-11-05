"""Documentos tabulares vinculados ao snapshot e à identidade da entrada."""
from copy import deepcopy

from openpyxl.utils.cell import coordinate_to_tuple, range_boundaries

from .serialization.collection_v2 import serialize_semantic_blocks_v2


def _inside(cell, bounds):
    row, column = coordinate_to_tuple(cell["coordinate"])
    return bounds[0] <= column <= bounds[2] and bounds[1] <= row <= bounds[3]


def build_documents(snapshot, semantic_blocks, input_sha256, tokenizers):
    def count(identity, text):
        return len(tokenizers[identity].encode(text, add_special_tokens=True, truncation=False))

    collection, payloads = serialize_semantic_blocks_v2(
        semantic_blocks,
        count_minilm=lambda text: count("E1_MINILM", text),
        count_taberta=lambda text: count("E2_TABERTA", text),
    )
    source = {block["block_id"]: block for block in semantic_blocks}
    content_sheets = [sheet["sheet_name"] for sheet in snapshot["sheets"] if sheet.get("cells")]
    documents = []
    for serialized, payload in zip(collection.documents, payloads):
        block = source[payload["source_block_id"]]
        bounds = range_boundaries(payload["range"]) if payload["range"] else None
        cells = [deepcopy(cell) for cell in block["cells"] if bounds is None or _inside(cell, bounds)]
        structural = deepcopy(block.get("structural_metadata", {}))
        structural.update(
            workbook_content_sheets=content_sheets,
            source_block_complete=payload["kind"] != "header_fragment" and payload["range"] == block.get("range"),
            serialized_body_truncated=payload["truncated"],
        )
        documents.append({
            "block_id": serialized.block_id, "block_type": block["block_type"],
            "sheet_name": block["sheet_name"], "range": payload["range"], "cells": cells,
            "retrieval_text": serialized.serialization_text, "structural_metadata": structural,
            "serialization_text": serialized.serialization_text,
            "serialization_sha256": serialized.serialization_sha256,
            "traceability": {
                "input_sha256": input_sha256, "source_block_id": block["block_id"],
                "sheet_name": block["sheet_name"], "range": payload["range"],
                "serialization_sha256": serialized.serialization_sha256,
                "enrichment_contract": "input_cells_within_serialized_document_range_v1",
            },
        })
    return documents
