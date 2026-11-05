from pathlib import Path
from datetime import datetime
from collections import Counter
import hashlib
import json


def calculate_file_hash(file_path: str) -> str:
    """SHA-256 da entrada para rastreabilidade e integridade."""
    sha256 = hashlib.sha256()

    with open(file_path, "rb") as file:
        for block in iter(lambda: file.read(4096), b""):
            sha256.update(block)

    return sha256.hexdigest()


def build_manifest(
    input_path: str,
    snapshot_path: str,
    snapshot: dict,
    semantic_blocks_path: str | None = None,
    semantic_blocks: dict | None = None
) -> dict:
    """Metadados de entrada, hashes e contagens estruturais da ingestão."""
    input_file = Path(input_path)
    snapshot_file = Path(snapshot_path)

    total_sheets = len(snapshot.get("sheets", []))
    total_cells = 0
    total_formulas = 0
    total_merged_cells = 0
    total_semantic_blocks = 0
    semantic_block_types = {}
    total_referenced_cells = 0

    for sheet in snapshot.get("sheets", []):
        cells = sheet.get("cells", [])
        total_cells += len(cells)
        total_formulas += sum(1 for cell in cells if cell.get("is_formula"))
        total_merged_cells += len(sheet.get("merged_cells", []))

    if semantic_blocks is not None:
        blocks = semantic_blocks.get("blocks", [])
        total_semantic_blocks = len(blocks)
        semantic_block_types = dict(
            sorted(Counter(block.get("block_type") for block in blocks).items())
        )

        referenced_cells = set()
        for block in blocks:
            sheet_name = block.get("sheet_name", "")
            for cell in block.get("cells", []):
                coordinate = cell.get("coordinate") if isinstance(cell, dict) else cell
                if coordinate:
                    referenced_cells.add(f"{sheet_name}!{coordinate}")

        total_referenced_cells = len(referenced_cells)

    manifest = {
        "ingestion_datetime": datetime.now().isoformat(),
        "input_file": {
            "name": input_file.name,
            "path": str(input_file),
            "sha256": calculate_file_hash(str(input_file))
        },
        "generated_files": {
            "structural_snapshot": str(snapshot_file)
        },
        "summary": {
            "total_sheets": total_sheets,
            "total_cells": total_cells,
            "total_formulas": total_formulas,
            "total_merged_cells": total_merged_cells,
            "total_semantic_blocks": total_semantic_blocks,
            "semantic_block_types": semantic_block_types,
            "total_referenced_cells": total_referenced_cells
        }
    }

    if semantic_blocks_path is not None:
        manifest["generated_files"]["semantic_blocks"] = str(Path(semantic_blocks_path))

    return manifest


def save_manifest(manifest: dict, output_path: str) -> None:
    with open(output_path, "w", encoding="utf-8") as file:
        json.dump(manifest, file, ensure_ascii=False, indent=2)
