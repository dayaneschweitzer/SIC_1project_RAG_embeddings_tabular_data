"""Execução de um XLSX com recuperação C1/C2 e decisão D1/D2."""
from __future__ import annotations

from hashlib import sha256
from pathlib import Path
import json

import numpy as np
import torch

from ingestion.manifest import build_manifest, save_manifest
from ingestion.semantic_blocks import save_semantic_blocks
from ingestion.structural_snapshot import extract_structural_snapshot, save_snapshot
from labels import classify_structural_complexity, inspect_workbook

from .contracts import file_hash, oracle_free, write_json
from .documents import build_documents
from .encoders import DECISION_CONDITIONS, ENCODER_IDS, THRESHOLDS, load_encoders
from .encoder_specs import spec_for
from .execution import execute_complete_plan
from .retrieval import prepare_retrieval
from .semantic_blocks import build_semantic_blocks


def run_scientific_pipeline(input_file, instruction, output_dir, mode, architecture, encoder_name, model, checkpoint):
    source = Path(input_file).resolve(strict=True)
    output = Path(output_dir).resolve()
    if not source.is_file() or source.suffix.lower() != ".xlsx":
        raise ValueError("--input-file deve indicar um arquivo .xlsx existente")
    if not instruction.strip():
        raise ValueError("--instruction deve conter uma instrução")
    encoder_id = ENCODER_IDS[encoder_name]
    spec = spec_for(encoder_id)
    if mode == "d1":
        from .d1 import validate_checkpoint_identity

        validate_checkpoint_identity(DECISION_CONDITIONS[encoder_id], checkpoint)
    output.mkdir(parents=True, exist_ok=False)
    metadata = {
        "decision_mode": mode, "architecture": architecture, "condition": architecture,
        "encoder": encoder_name, "encoder_id": encoder_id,
        "encoder_model": spec.model_id, "encoder_revision": spec.revision,
        "encoder_variant": spec.variant, "top_k": 5, "context_budget": 5,
        "threshold": THRESHOLDS[encoder_id], "document_token_limit": 256,
        "query_and_decision_chunk_limit": 512,
        "decision_source": "D1-v3 retrieval-conditioned / D2 D1-parity v1",
        "d2_source_branch": "feat/d2-llm-decision-v1",
        "d2_source_head": "1ba880464e7894c522cc26561377bd4bf333b982",
        "executor_contract": "public_complete_action_plan_v1",
        "input_file": str(source), "output_dir": str(output),
    }
    stage = "ingestion"
    response = {"status": "error", "success": False, "pipeline": metadata, "instruction": instruction}
    try:
        if mode == "d2":
            stage = "credential_configuration"
            from .deepseek_client import require_deepseek_api_key

            require_deepseek_api_key()
        stage = "ingestion"
        original_hash = file_hash(source)
        metadata["input_sha256"] = original_hash
        snapshot = extract_structural_snapshot(str(source))
        snapshot_path = output / f"{source.stem}_structural_snapshot.json"
        blocks_path = output / f"{source.stem}_semantic_blocks.json"
        save_snapshot(snapshot, str(snapshot_path))
        metrics = inspect_workbook(source)
        score, level = classify_structural_complexity(metrics)
        metadata["structural_classification"] = {"score": score, "level": level, "metrics": metrics}
        semantic = build_semantic_blocks(snapshot, snapshot_path=None)
        save_semantic_blocks(semantic, str(blocks_path))
        save_manifest(build_manifest(
            str(source), str(snapshot_path), snapshot, str(blocks_path), semantic,
        ), str(output / f"{source.stem}_manifest.json"))

        stage = "encoder_loading_and_serialization"
        torch.set_num_threads(2)
        torch.use_deterministic_algorithms(True)
        encoder, tokenizers = load_encoders(encoder_id, model)
        documents = build_documents(snapshot, semantic["blocks"], original_hash, tokenizers)
        oracle_free(documents)
        write_json(output / "documents.json", documents)
        texts = [document["retrieval_text"] for document in documents]
        stage = "indexing"
        with torch.inference_mode():
            vectors = encoder.encode(
                texts, batch_size=32, normalize_embeddings=True, convert_to_numpy=True,
                precision="float32", show_progress_bar=False,
            ).astype(np.float32)
        if vectors.shape != (len(documents), spec.dimension) or not np.isfinite(vectors).all():
            raise ValueError("INDEX_VECTOR_CONTRACT")
        index_dir = output / "index"
        index_dir.mkdir()
        np.save(index_dir / "embeddings.npy", vectors, allow_pickle=False)
        write_json(index_dir / "metadata.json", documents)
        write_json(index_dir / "manifest.json", metadata)

        stage = "decision_loading"
        decision_condition = DECISION_CONDITIONS[encoder_id]
        if mode == "d1":
            from .d1 import RetrievalConditionedDecision

            decision = RetrievalConditionedDecision(decision_condition, encoder, checkpoint)
        else:
            from .d2 import DeepSeekDecisionAdapter
            from .transport import ParityDeepSeekClient

            decision = DeepSeekDecisionAdapter(decision_condition, encoder, ParityDeepSeekClient())
        with torch.inference_mode():
            stage = "retrieval"
            condition = architecture + ("-E1" if encoder_id == "E1_MINILM" else "-E2")
            retrieval = prepare_retrieval(
                instruction, documents, vectors, decision.encode_queries, condition, THRESHOLDS[encoder_id],
            )
            context = retrieval.pop("context")
            write_json(output / "retrieval.json", retrieval)
            stage = "decision"
            outcome = decision.decide(instruction, retrieval["evidence"], original_hash, condition)
        outcome["retrieval_context_vector_sha256"] = sha256(context.tobytes()).hexdigest()
        outcome["retrieval"] = retrieval
        oracle_free(outcome)
        write_json(output / "decision.json", outcome)
        response["decision"] = outcome
        payload = outcome.get("action_dsl")
        if outcome["status"] != "PROGRAM_COMPLETE" or not payload:
            response.update(status="failed", decision_status=outcome["status"], reason=outcome.get("reason"))
        else:
            write_json(output / "action_plan.json", payload)
            stage = "complete_plan_execution"
            execution = execute_complete_plan(source, instruction, payload, retrieval["evidence"], output / "execution")
            response.update(
                status="completed" if execution["published"] else "failed",
                success=execution["published"], decision_status=outcome["status"],
                action_plan=payload, change_plan=execution.get("change_plan"),
                execution=execution, modified_file=execution["output"],
            )
        if file_hash(source) != original_hash:
            raise RuntimeError("ORIGINAL_MODIFIED")
    except Exception as error:
        response.update(
            status="error", success=False, error_type=type(error).__name__,
            error=str(error), pipeline_stage=stage,
        )
    (output / "run_result.json").write_text(
        json.dumps(response, ensure_ascii=False, indent=2, allow_nan=False) + "\n", encoding="utf-8",
    )
    return response
