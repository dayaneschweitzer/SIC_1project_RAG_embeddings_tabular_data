from __future__ import annotations

from argparse import ArgumentParser
from datetime import datetime
from pathlib import Path
import json
import sys


SRC_DIR = Path(__file__).resolve().parent / "src"
if str(SRC_DIR) not in sys.path:
    sys.path.insert(0, str(SRC_DIR))


def _run_baseline_pipeline(
    input_file: Path,
    instruction: str,
    output_dir: Path,
    condition: str = "C1",
    model: str = "sentence-transformers/all-MiniLM-L6-v2",
    top_k: int = 5,
) -> dict:
    from ingestion.manifest import build_manifest, save_manifest
    from ingestion.semantic_blocks import build_semantic_blocks, save_semantic_blocks
    from ingestion.structural_snapshot import extract_structural_snapshot, save_snapshot
    from labels import (
        CONDITION_LABELS,
        MODIFICATION_ARCHITECTURES,
        SOURCE_COMMIT,
        SOURCE_REFERENCE,
        classify_structural_complexity,
        inspect_workbook,
    )
    from retrieval.build_embedding_index import build_embedding_index

    input_file = Path(input_file).resolve(strict=True)
    output_dir = Path(output_dir).resolve()
    if not input_file.is_file() or input_file.suffix.lower() != ".xlsx":
        raise ValueError("--input-file deve indicar um arquivo .xlsx existente.")
    if not instruction.strip():
        raise ValueError("--instruction deve conter uma instrução.")
    if top_k < 1:
        raise ValueError("--top-k deve ser maior que zero.")
    if condition not in CONDITION_LABELS:
        raise ValueError(f"Condição desconhecida: {condition}")
    output_dir.mkdir(parents=True, exist_ok=False)
    basename = input_file.stem
    snapshot_path = output_dir / f"{basename}_structural_snapshot.json"
    blocks_path = output_dir / f"{basename}_semantic_blocks.json"
    manifest_path = output_dir / f"{basename}_manifest.json"
    index_dir = output_dir / "index"
    execution_dir = output_dir / "execution"
    metadata = {
        "decision_mode": "baseline",
        "architecture": condition,
        "encoder": "minilm",
        "source_reference": SOURCE_REFERENCE,
        "source_commit": SOURCE_COMMIT,
        "condition": condition,
        "input_file": str(input_file),
        "output_dir": str(output_dir),
        "model": model,
        "top_k": top_k,
    }
    stage = "ingestion"
    try:
        snapshot = extract_structural_snapshot(str(input_file))
        save_snapshot(snapshot, str(snapshot_path))
        stage = "structural_labels"
        metrics = inspect_workbook(input_file)
        score, level = classify_structural_complexity(metrics)
        metadata["structural_classification"] = {
            "score": score, "level": level, "metrics": metrics,
        }

        stage = "segmentation"
        blocks = build_semantic_blocks(snapshot, snapshot_path=str(snapshot_path))
        save_semantic_blocks(blocks, str(blocks_path))
        manifest = build_manifest(
            input_path=str(input_file),
            snapshot_path=str(snapshot_path),
            snapshot=snapshot,
            semantic_blocks_path=str(blocks_path),
            semantic_blocks=blocks,
        )
        save_manifest(manifest, str(manifest_path))

        stage = "indexing"
        build_embedding_index(
            semantic_blocks_path=str(blocks_path),
            output_dir=str(index_dir),
            provider_name="sentence-transformers",
            model_name=model,
            basename=basename,
        )
        stage = "retrieval_decision_execution_verification"
        kwargs = {
            "request": instruction,
            "input_file": str(input_file),
            "snapshot_path": str(snapshot_path),
            "index_dir": str(index_dir),
            "basename": basename,
            "output_dir": str(execution_dir),
            "top_k": top_k,
        }
        if condition == "C1":
            from rag.single_agent_modification_baseline import run_single_agent_modification

            response = run_single_agent_modification(**kwargs)
        else:
            from multiagent.multiagent_modification_orchestrator import run_multiagent_modification

            response = run_multiagent_modification(**kwargs)
        response["architecture"] = MODIFICATION_ARCHITECTURES[condition]
        response.setdefault("execution_metadata", {})["architecture"] = MODIFICATION_ARCHITECTURES[condition]
        response.setdefault("success", response.get("status") in {"completed", "passed"})
    except Exception as error:
        response = {
            "status": "error",
            "success": False,
            "error_type": type(error).__name__,
            "error": str(error),
            "pipeline_stage": stage,
        }
    response["pipeline"] = metadata
    response["instruction"] = instruction
    result_path = output_dir / "run_result.json"
    result_path.write_text(
        json.dumps(response, ensure_ascii=False, indent=2), encoding="utf-8",
    )
    return response


def validate_configuration(decision_mode, condition, encoder, top_k, d1_checkpoint):
    if decision_mode not in {"baseline", "d1", "d2"}:
        raise ValueError("Modo de decisão desconhecido")
    if encoder not in {"minilm", "taberta"}:
        raise ValueError("Encoder desconhecido")
    if condition not in {"C1", "C2"}:
        raise ValueError("Arquitetura desconhecida")
    if top_k < 1:
        raise ValueError("--top-k deve ser maior que zero")
    if decision_mode == "baseline":
        if encoder != "minilm":
            raise ValueError("O baseline implementa somente MiniLM com C1/C2")
        if d1_checkpoint is not None:
            raise ValueError("--d1-checkpoint pertence somente ao modo d1")
    else:
        if top_k != 5:
            raise ValueError("D1/D2 preservam top-k=5 e orçamento de contexto=5")
        if decision_mode == "d1" and d1_checkpoint is None:
            raise ValueError("D1 exige --d1-checkpoint com o peso treinado correspondente ao encoder")
        if decision_mode == "d2" and d1_checkpoint is not None:
            raise ValueError("D2 não carrega pesos da D1; remova --d1-checkpoint")


def run_pipeline(
    input_file: Path,
    instruction: str,
    output_dir: Path,
    condition: str = "C1",
    model: str | None = None,
    top_k: int = 5,
    *,
    decision_mode: str = "baseline",
    encoder: str = "minilm",
    d1_checkpoint: Path | None = None,
) -> dict:
    validate_configuration(decision_mode, condition, encoder, top_k, d1_checkpoint)
    if decision_mode == "baseline":
        return _run_baseline_pipeline(
            input_file, instruction, output_dir, condition,
            model or "sentence-transformers/all-MiniLM-L6-v2", top_k,
        )
    from scientific.pipeline import run_scientific_pipeline

    return run_scientific_pipeline(
        input_file, instruction, output_dir, decision_mode, condition, encoder, model, d1_checkpoint,
    )


def main() -> int:
    parser = ArgumentParser(
        description="Pipeline SIC: C1 single-agent e C2 multi-agent; baseline com MiniLM, D1/D2 com MiniLM/TabERTa.",
    )
    parser.add_argument("--input-file", type=Path, required=True, help="Caminho da planilha .xlsx original")
    parser.add_argument("--instruction", required=True, help="Instrução de edição em linguagem natural")
    parser.add_argument("--decision-mode", type=str.lower, choices=("baseline", "d1", "d2"), default="baseline", help="Decisor (padrão: baseline)")
    parser.add_argument("--architecture", "--condition", dest="condition", choices=("C1", "C2"), default="C1", help="C1 single-agent ou C2 multi-agent; --condition é alias")
    parser.add_argument("--encoder", type=str.lower, choices=("minilm", "taberta"), default="minilm", help="Encoder (padrão: minilm)")
    parser.add_argument("--model", "--encoder-model", dest="model", default=None, help="Encoder do baseline ou pasta local fixada para D1/D2")
    parser.add_argument("--d1-checkpoint", type=Path, default=None, help="Arquivo decision_v3.pt de weights/d1-v3/D1-A-v3 (MiniLM) ou D1-B-v3 (TabERTa)")
    parser.add_argument("--top-k", type=int, default=5, help="Número de evidências recuperadas (padrão: 5)")
    parser.add_argument("--output-dir", type=Path, default=None, help="Pasta nova de saída (padrão: outputs/<data-hora>)")
    args = parser.parse_args()
    output_dir = args.output_dir or Path("outputs") / datetime.now().strftime("%Y%m%d_%H%M%S_%f")
    try:
        response = run_pipeline(
            input_file=args.input_file,
            instruction=args.instruction,
            output_dir=output_dir,
            condition=args.condition,
            model=args.model,
            top_k=args.top_k,
            decision_mode=args.decision_mode,
            encoder=args.encoder,
            d1_checkpoint=args.d1_checkpoint,
        )
    except (OSError, ValueError, ImportError) as error:
        print(f"Erro: {error}", file=sys.stderr)
        return 2
    print(f"Estado: {response.get('status')}")
    print(f"Relatório: {output_dir.resolve() / 'run_result.json'}")
    if response.get("error"):
        print(f"Erro: {response['error']}", file=sys.stderr)
    if response.get("modified_file"):
        print(f"Planilha gerada: {response['modified_file']}")
    if response.get("status") == "error":
        return 2
    return 0 if response.get("success") else 1


if __name__ == "__main__":
    raise SystemExit(main())
