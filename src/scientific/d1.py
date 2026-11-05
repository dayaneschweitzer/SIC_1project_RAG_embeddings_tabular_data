"""Inferência D1-v3 com identidade verificada dos checkpoints."""
from __future__ import annotations

from hashlib import sha256
from pathlib import Path

import numpy as np
import torch

from .contracts import file_hash
from .program_v3 import (
    OPERATIONS, ROLE_CONTRACT, FrozenEncoder, candidate_feature,
    candidates_for_role, compile_action_plan, evidence_vector,
    extract_candidates, load_checkpoint, operation_feature, split_clauses,
    validate_program,
)


RUNTIME_NAME = "sic_d1_v3_observational_integrated_v1"
CHECKPOINT_HASHES = {
    "D1-A-v3": "3f8cec6a44135f9135b9c37227f5af193bc2a21e42455293300a2ef8d4b70646",
    "D1-B-v3": "e23235c0e0028111fbb952324f0817589a8a72a8cdeb99eb0c764c3fef769466",
}
SOURCE_CHECKPOINT_HASHES = {
    "D1-A-v3": "b2d811175359e00a1ba88d73701c7ed61018de1feb6494c4fe3527368f81c64a",
    "D1-B-v3": "3fdd94f5883ef64cbacf04e42fd0f2bce22b94b0dd108ac0b58b8aec5bca342d",
}


def validate_checkpoint_identity(condition, checkpoint):
    """Aceita o pacote de inferência ou o checkpoint de origem correspondente."""
    if condition not in CHECKPOINT_HASHES:
        raise ValueError("DECISION_CONDITION")
    digest = file_hash(Path(checkpoint))
    if digest not in (CHECKPOINT_HASHES[condition], SOURCE_CHECKPOINT_HASHES[condition]):
        raise ValueError("CHECKPOINT_IDENTITY: o peso não corresponde à condição D1 selecionada")
    return digest


class RetrievalConditionedDecision:
    def __init__(self, condition, encoder, checkpoint):
        if condition not in CHECKPOINT_HASHES:
            raise ValueError("DECISION_CONDITION")
        self.condition = condition
        self.encoder_id = "E1_MINILM" if condition == "D1-A-v3" else "E2_TABERTA"
        self.checkpoint = Path(checkpoint).resolve(strict=True)
        self.checkpoint_sha256 = validate_checkpoint_identity(condition, self.checkpoint)
        self.frozen = FrozenEncoder(encoder, max_length=512)
        self.payload, self.operation_head, self.argument_ranker = load_checkpoint(self.checkpoint)

    def encode_queries(self, queries):
        return self.frozen.encode_many(queries)

    def _infer(self, instruction, evidence):
        parts = split_clauses(instruction)
        values = np.stack([operation_feature(self.frozen, part[2], evidence) for part in parts])
        with torch.inference_mode():
            logits = self.operation_head(torch.from_numpy(values)).numpy()
        labels = [OPERATIONS[index] for index in logits.argmax(axis=1)]
        raw = {
            "labels": labels,
            "operation_logits": logits.tolist(),
            "clauses": [{"span": [part[0], part[1]], "text": part[2]} for part in parts],
            "argument_candidates": [],
            "selected_arguments": [],
        }
        if labels == ["REFUSE"]:
            program = {"schema_version": "structured_program_v2_dev", "instruction_sha256": sha256(instruction.encode("utf8")).hexdigest(), "states": ["START", "REFUSE", "EOS"], "actions": [], "refusal": "LEARNED_OUT_OF_DOMAIN"}
            return {"status": "REFUSAL", "reason": "LEARNED_OUT_OF_DOMAIN", "raw_model_decision": raw, "program": program, "action_dsl": None, "tokenization": [self.frozen.audit.get(part[2]) for part in parts]}
        program = None
        plan = None
        status = "PROGRAM_INCOMPLETE"
        reason = None
        try:
            if "REFUSE" in labels:
                raise ValueError("MIXED_REFUSAL")
            all_candidates = extract_candidates(instruction, evidence)
            actions = []
            for action_index, (part, operation) in enumerate(zip(parts, labels)):
                arguments = {}
                pointers = {}
                for role in ROLE_CONTRACT[operation]:
                    candidates = candidates_for_role(all_candidates, role, (part[0], part[1]))
                    raw["argument_candidates"].append({"action_id": f"a{action_index + 1}", "role": role, "count": len(candidates)})
                    if not candidates:
                        raise ValueError(f"NO_CANDIDATE:{operation}:{role}")
                    features = np.stack([candidate_feature(self.frozen, part[2], evidence, candidate, operation, role, len(instruction)) for candidate in candidates])
                    with torch.inference_mode():
                        scores = self.argument_ranker(torch.from_numpy(features)).numpy()
                    selected_index = int(scores.argmax())
                    selected = candidates[selected_index]
                    arguments[role] = selected["value"]
                    pointers[role] = selected["pointers"]
                    raw["selected_arguments"].append({"action_id": f"a{action_index + 1}", "role": role, "candidate_index": selected_index, "score": float(scores[selected_index]), "value": selected["value"], "pointer": selected["pointers"], "all_scores": scores.tolist()})
                actions.append({"action_id": f"a{action_index + 1}", "operation": operation, "arguments": arguments, "pointers": pointers, "depends_on": [] if action_index == 0 else [f"a{action_index}"]})
            states = ["START"]
            for index in range(len(actions)):
                states.extend(["ACTION", "CONTINUE" if index + 1 < len(actions) else "STOP"])
            states.append("EOS")
            program = {"schema_version": "structured_program_v2_dev", "instruction_sha256": sha256(instruction.encode("utf8")).hexdigest(), "states": states, "actions": actions, "refusal": None}
            validate_program(program, instruction, evidence)
            plan = compile_action_plan(program)
            status = "PROGRAM_COMPLETE"
        except (ValueError, KeyError, TypeError) as error:
            reason = f"{type(error).__name__}:{error}"
        return {"status": status, "reason": reason, "raw_model_decision": raw, "program": program, "action_dsl": plan, "tokenization": [self.frozen.audit.get(part[2]) for part in parts]}

    def decide(self, instruction, evidence, input_sha256, scientific_condition):
        normalized_evidence = []
        for item in evidence:
            value = dict(item)
            text = value.get("text")
            source_field = "text"
            if text is None:
                text = value.get("retrieval_text")
                source_field = "retrieval_text"
            if not isinstance(text, str) or not text:
                raise ValueError("EVIDENCE_TEXT_CONTRACT:" + str(value.get("block_id")))
            value["text"] = text
            normalized_evidence.append(value)
        parts = split_clauses(instruction)
        features = [operation_feature(self.frozen, part[2], normalized_evidence) for part in parts]
        _, statistics = evidence_vector(self.frozen, normalized_evidence)
        outcome = self._infer(instruction, normalized_evidence)
        raw = outcome["raw_model_decision"]
        outcome.update({
            "schema_version": "sic_d1_v3_integrated_decision_v1",
            "runtime_identity": RUNTIME_NAME,
            "scientific_condition": scientific_condition,
            "decision_condition": self.condition,
            "decision_encoder_id": self.encoder_id,
            "checkpoint_sha256": self.checkpoint_sha256,
            "input_sha256": input_sha256,
            "instruction": instruction,
            "instruction_sha256": sha256(instruction.encode("utf8")).hexdigest(),
            "predicted_operations": raw.get("labels"),
            "logits": raw.get("operation_logits"),
            "argument_candidates": raw.get("argument_candidates", []),
            "raw_arguments": raw.get("selected_arguments", []),
            "retrieval_conditioning": {
                "evidence_ids": [item["block_id"] for item in evidence],
                "scores": [float(item.get("score", 0.0)) for item in evidence],
                "ranks": [int(item.get("rank", index + 1)) for index, item in enumerate(evidence)],
                "statistics_fields": ["count_fraction", "maximum_score", "mean_score", "weighted_rank_fraction"],
                "statistics": statistics.tolist(),
                "feature_vectors": [value.tolist() for value in features],
                "feature_sha256": [sha256(value.astype(np.float32).tobytes()).hexdigest() for value in features],
                "feature_dimensions": [int(value.shape[0]) for value in features],
                "evidence_text_contract": "text if present, otherwise retrieval_text copied without content change",
                "evidence_text_sha256": [sha256(item["text"].encode("utf8")).hexdigest() for item in normalized_evidence],
            },
        })
        return outcome
