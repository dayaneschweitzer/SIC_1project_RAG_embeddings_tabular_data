from __future__ import annotations

from pathlib import Path

try:
    from modification.modification_pipeline import (
        run_controlled_modification,
    )
except ImportError:
    import sys

    SRC_DIR = Path(__file__).resolve().parents[1]
    sys.path.insert(0, str(SRC_DIR))
    from modification.modification_pipeline import (
        run_controlled_modification,
    )


ARCHITECTURE_NAME = "single-agent-rag-controlled-modification"


def run_single_agent_modification(
    request: str,
    input_file: str,
    snapshot_path: str,
    index_dir: str = "data/index",
    basename: str | None = None,
    output_dir: str = "data/modified",
    top_k: int = 5,
    search_results: list[dict] | None = None,
    index_config: dict | None = None,
) -> dict:
    return run_controlled_modification(
        request=request,
        input_file=input_file,
        snapshot_path=snapshot_path,
        index_dir=index_dir,
        basename=basename,
        output_dir=output_dir,
        top_k=top_k,
        architecture=ARCHITECTURE_NAME,
        search_results=search_results,
        index_config=index_config,
    )
