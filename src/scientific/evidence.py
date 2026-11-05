from __future__ import annotations

from dataclasses import dataclass, asdict, field


@dataclass
class Evidence:
    rank: int
    score: float
    block_id: str
    block_type: str
    sheet_name: str
    range: str | None
    cells: list
    retrieval_text: str
    traceability: dict
    structural_metadata: dict = field(default_factory=dict)

    def to_dict(self) -> dict:
        return asdict(self)


def evidence_from_search_result(result: dict) -> Evidence:
    metadata = result.get("metadata", {})

    return Evidence(
        rank=int(result.get("rank", 0)),
        score=float(result.get("score", 0.0)),
        block_id=metadata.get("block_id", ""),
        block_type=metadata.get("block_type", ""),
        sheet_name=metadata.get("sheet_name", ""),
        range=metadata.get("range"),
        cells=metadata.get("cells", []),
        retrieval_text=metadata.get("retrieval_text", ""),
        traceability=metadata.get("traceability", {}),
        structural_metadata=metadata.get("structural_metadata") or {},
    )


def evidences_from_search_results(results: list[dict]) -> list[dict]:
    return [evidence_from_search_result(result).to_dict() for result in results]


def referenced_cell_coordinates(evidence: dict) -> list[str]:
    coordinates = []

    for cell in evidence.get("cells", []):
        if isinstance(cell, dict):
            coordinate = cell.get("coordinate")
        else:
            coordinate = cell

        if coordinate:
            coordinates.append(coordinate)

    return coordinates
