from __future__ import annotations

from pathlib import Path

try:
    from modification.modification_pipeline import run_controlled_modification
except ImportError:
    import sys

    SRC_DIR = Path(__file__).resolve().parents[1]
    sys.path.insert(0, str(SRC_DIR))
    from modification.modification_pipeline import run_controlled_modification


AGENT_NAME = "modification_executor"


def run_modification_executor_agent(
    request: str,
    input_file: str,
    snapshot_path: str,
    index_dir: str,
    basename: str,
    output_dir: str,
    top_k: int,
    verified_evidences: list[dict],
    index_config: dict,
) -> dict:
    fake_search_results = [
        {
            "rank": evidence.get("rank", index + 1),
            "score": evidence.get("score", 0.0),
            "metadata": {
                "block_id": evidence.get("block_id"),
                "block_type": evidence.get("block_type"),
                "sheet_name": evidence.get("sheet_name"),
                "range": evidence.get("range"),
                "cells": evidence.get("cells", []),
                "retrieval_text": evidence.get("retrieval_text", ""),
                "traceability": evidence.get("traceability", {}),
            },
        }
        for index, evidence in enumerate(verified_evidences)
    ]

    report = run_controlled_modification(
        request=request,
        input_file=input_file,
        snapshot_path=snapshot_path,
        index_dir=index_dir,
        basename=basename,
        output_dir=output_dir,
        top_k=top_k,
        architecture="multiagent-rag-controlled-modification",
        search_results=fake_search_results,
        index_config=index_config,
    )

    return {
        "agent": AGENT_NAME,
        "modification_status": report.get("status"),
        "modified_file": report.get("modified_file"),
        "change_plan": report.get("change_plan", {}),
        "guard_report": report.get("guard_report", {}),
        "editor_report": report.get("editor_report", {}),
        "structural_diff": report.get("structural_diff", {}),
        "effect_verification": report.get("effect_verification", {}),
        "modification_report": report,
    }
