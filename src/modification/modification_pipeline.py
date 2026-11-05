from __future__ import annotations

from datetime import datetime
from pathlib import Path
import json

try:
    from .change_plan import build_change_plan
    from .modification_intent import parse_modification_request
    from .plan_effect_verifier import verify_plan_effect
    from .spreadsheet_editor import apply_change_plan
    from .structural_diff import build_structural_diff, calculate_file_hash
    from .structural_guard import validate_change_plan
except ImportError:
    from change_plan import build_change_plan
    from modification_intent import parse_modification_request
    from plan_effect_verifier import verify_plan_effect
    from spreadsheet_editor import apply_change_plan
    from structural_diff import build_structural_diff, calculate_file_hash
    from structural_guard import validate_change_plan

try:
    from ingestion.structural_snapshot import extract_structural_snapshot
except ImportError:
    import sys

    SRC_DIR = Path(__file__).resolve().parents[1]
    sys.path.insert(0, str(SRC_DIR))
    from ingestion.structural_snapshot import extract_structural_snapshot

try:
    from rag.evidence import evidences_from_search_results
except ImportError:
    import sys

    SRC_DIR = Path(__file__).resolve().parents[1]
    sys.path.insert(0, str(SRC_DIR))
    from rag.evidence import evidences_from_search_results

try:
    from retrieval.search_index import search_index
    from retrieval.vector_index import infer_single_basename, load_index_artifacts
except ImportError:
    import sys

    SRC_DIR = Path(__file__).resolve().parents[1]
    sys.path.insert(0, str(SRC_DIR))
    from retrieval.search_index import search_index
    from retrieval.vector_index import infer_single_basename, load_index_artifacts


MODIFICATION_STRATEGY = "deterministic_controlled_xlsx_edit"


def run_controlled_modification(
    request: str,
    input_file: str,
    snapshot_path: str,
    index_dir: str = "data/index",
    basename: str | None = None,
    output_dir: str = "data/modified",
    top_k: int = 5,
    architecture: str = "controlled-modification-pipeline",
    search_results: list[dict] | None = None,
    index_config: dict | None = None,
) -> dict:
    basename = basename or infer_single_basename(index_dir)
    output_path = Path(output_dir)
    output_path.mkdir(parents=True, exist_ok=True)

    original_hash_before = calculate_file_hash(input_file)
    original_snapshot = load_json(snapshot_path)
    index_config = index_config or _load_index_config(index_dir=index_dir, basename=basename)

    if search_results is None:
        search_results = search_index(
            query=request,
            top_k=top_k,
            index_dir=index_dir,
            basename=basename,
        )

    evidences = evidences_from_search_results(search_results)
    intent = parse_modification_request(request)
    change_plan = build_change_plan(intent=intent, evidences=evidences, snapshot=original_snapshot)

    guard_report = {}
    editor_report = {}
    structural_diff = {}
    effect_verification = {}
    modified_snapshot = {}
    modified_file = str(output_path / f"{basename}_modified.xlsx")
    applied_file = None

    if change_plan.get("status") == "failed":
        guard_report = {
            "status": "failed",
            "checks": {},
            "errors": change_plan.get("errors", []),
            "warnings": [],
        }
    else:
        guard_report = validate_change_plan(change_plan=change_plan, snapshot=original_snapshot)

    if guard_report.get("status") == "passed":
        editor_report = apply_change_plan(
            input_xlsx=input_file,
            change_plan=change_plan,
            output_xlsx=modified_file,
        )
        if editor_report.get("status") == "applied" and Path(modified_file).is_file():
            applied_file = modified_file
            modified_snapshot = extract_structural_snapshot(modified_file)
            structural_diff = build_structural_diff(
                original_snapshot=original_snapshot,
                modified_snapshot=modified_snapshot,
                change_plan=change_plan,
                original_file=input_file,
                modified_file=modified_file,
            )
        effect_verification = verify_plan_effect(
            change_plan=change_plan,
            original_snapshot=original_snapshot,
            modified_snapshot=modified_snapshot,
            structural_diff=structural_diff,
            execution_result=editor_report,
            changed_cells=editor_report.get("changed_cells"),
        )

    original_hash_after = calculate_file_hash(input_file)
    original_preserved = original_hash_before == original_hash_after
    report = build_modification_report(
        request=request,
        architecture=architecture,
        basename=basename,
        input_file=input_file,
        snapshot_path=snapshot_path,
        modified_file=applied_file,
        evidences=evidences,
        intent=intent,
        change_plan=change_plan,
        guard_report=guard_report,
        editor_report=editor_report,
        structural_diff=structural_diff,
        effect_verification=effect_verification,
        index_config=index_config,
        top_k=top_k,
        original_preserved=original_preserved,
    )

    save_modification_outputs(report=report, output_dir=str(output_path), basename=basename)
    return report


def build_modification_report(
    request: str,
    architecture: str,
    basename: str,
    input_file: str,
    snapshot_path: str,
    modified_file: str | None,
    evidences: list[dict],
    intent: dict,
    change_plan: dict,
    guard_report: dict,
    editor_report: dict,
    structural_diff: dict,
    effect_verification: dict,
    index_config: dict,
    top_k: int,
    original_preserved: bool,
) -> dict:
    return {
        "request": request,
        "architecture": architecture,
        "status": _overall_status(
            change_plan,
            guard_report,
            editor_report,
            structural_diff,
            original_preserved,
            effect_verification,
        ),
        "input_file": input_file,
        "snapshot_file": snapshot_path,
        "modified_file": modified_file,
        "top_k": top_k,
        "evidence_count": len(evidences),
        "evidences": evidences,
        "modification_intent": intent,
        "change_plan": change_plan,
        "guard_report": guard_report,
        "editor_report": editor_report,
        "structural_diff": structural_diff,
        "effect_verification": effect_verification,
        "execution_metadata": {
            "created_at": datetime.now().isoformat(),
            "modification_strategy": MODIFICATION_STRATEGY,
            "retrieval_strategy": "semantic_vector_search",
            "embedding_model": index_config.get("embedding_model"),
            "embedding_provider": index_config.get("embedding_provider"),
            "similarity_metric": index_config.get("similarity_metric", "cosine"),
            "index_strategy": index_config.get("index_strategy"),
            "basename": basename,
            "original_file_preserved": original_preserved,
        },
    }


def save_modification_outputs(report: dict, output_dir: str, basename: str) -> dict:
    output_path = Path(output_dir)
    output_path.mkdir(parents=True, exist_ok=True)

    report_path = output_path / f"{basename}_modification_report.json"
    diff_path = output_path / f"{basename}_structural_diff.json"

    with open(report_path, "w", encoding="utf-8") as file:
        json.dump(report, file, ensure_ascii=False, indent=2)

    with open(diff_path, "w", encoding="utf-8") as file:
        json.dump(report.get("structural_diff", {}), file, ensure_ascii=False, indent=2)

    return {
        "report": str(report_path),
        "diff": str(diff_path),
    }


def load_json(path: str) -> dict:
    with open(path, "r", encoding="utf-8") as file:
        return json.load(file)


def _load_index_config(index_dir: str, basename: str) -> dict:
    _, _, config = load_index_artifacts(index_dir=index_dir, basename=basename)
    return config


def _overall_status(
    change_plan: dict,
    guard_report: dict,
    editor_report: dict,
    structural_diff: dict,
    original_preserved: bool,
    effect_verification: dict | None = None,
) -> str:
    if change_plan.get("status") == "failed":
        return "failed"
    if guard_report.get("status") != "passed":
        return "failed"
    if not editor_report or editor_report.get("status") == "failed":
        return "failed"
    if not original_preserved:
        return "failed"
    if effect_verification:
        if effect_verification.get("status") == "failed":
            return "failed"
        if effect_verification.get("status") == "passed":
            return "completed"
    if structural_diff.get("fidelity_status") not in {"passed", None}:
        return "warning"
    return "completed"
