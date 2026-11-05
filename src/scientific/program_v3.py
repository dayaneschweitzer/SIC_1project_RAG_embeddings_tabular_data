from __future__ import annotations

from hashlib import sha256
import json
from pathlib import Path
import re

import numpy as np
import torch


OPERATIONS = (
    "write_value", "write_formula", "insert_row", "delete_row",
    "insert_column", "sort_range", "unmerge_range", "copy_sheet", "REFUSE",
)
ROLES = ("sheet", "targets", "value", "formula", "position", "range", "new_sheet")
KINDS = ("sheet", "cell", "cells", "range", "formula", "number", "column", "text")
ROLE_CONTRACT = {
    "write_value": ("sheet", "targets", "value"),
    "write_formula": ("sheet", "targets", "formula"),
    "insert_row": ("sheet", "position"),
    "delete_row": ("sheet", "position"),
    "insert_column": ("sheet", "position"),
    "sort_range": ("sheet", "range"),
    "unmerge_range": ("sheet", "range"),
    "copy_sheet": ("sheet", "new_sheet"),
}
ROLE_KINDS = {
    "sheet": {"sheet"}, "new_sheet": {"sheet"}, "targets": {"cell", "cells"},
    "value": {"number", "text"}, "formula": {"formula"},
    "position": {"number", "column"}, "range": {"range"},
}
CONNECTOR = re.compile(
    r"(?:;\s*(?:then|next)\s+|\s+and\s+(?:then|afterwards)\s+|\.\s*(?:Then|After that|Subsequently),?\s+|\s+followed\s+by\s+|\nNext,\s+)",
    re.I,
)
CELL = re.compile(r"\$?[A-Z]{1,3}\$?[1-9][0-9]*")
RANGE = re.compile(r"\$?[A-Z]{1,3}\$?[1-9][0-9]*:\$?[A-Z]{1,3}\$?[1-9][0-9]*")
QUOTED = re.compile(r"(['\"])(.+?)\1")
MARKER = re.compile(r"\b(sheet|worksheet|tab|new_sheet|range|target|formula|position|value)\s*[=:]\s*([^;\n.]+)", re.I)


def canonical(value):
    return json.dumps(value, ensure_ascii=False, sort_keys=True, separators=(",", ":"), allow_nan=False).encode("utf8")


def digest(value):
    return sha256(canonical(value)).hexdigest()


def split_clauses(text):
    values = []
    start = 0
    for match in CONNECTOR.finditer(text):
        values.append((start, match.start(), text[start:match.start()].strip()))
        start = match.end()
    values.append((start, len(text), text[start:].strip()))
    return [value for value in values if value[2]]


def _balanced_formulas(text):
    output = []
    start_pattern = re.compile(r"(?<![A-Za-z0-9_])=?([A-Z][A-Z0-9_.]*)\(")
    for match in start_pattern.finditer(text):
        depth = 0
        end = None
        for index in range(match.end() - 1, len(text)):
            if text[index] == "(":
                depth += 1
            elif text[index] == ")":
                depth -= 1
                if depth == 0:
                    end = index + 1
                    break
        if end is not None:
            output.append((match.start(), end, text[match.start():end]))
    return output


def _candidate(source, text, start, end, kind, evidence_id=None, score=0.0, rank=0):
    value = text[start:end]
    pointer = {"source": source, "span": [start, end]}
    if evidence_id is not None:
        pointer["evidence_id"] = evidence_id
    return {
        "source": source, "evidence_id": evidence_id, "span": [start, end],
        "kind": kind, "value": value, "pointers": pointer,
        "evidence_score": float(score), "evidence_rank": int(rank),
    }


def extract_candidates(instruction, evidence):
    output = []

    def collect(source, text, evidence_id=None, score=0.0, rank=0):
        formula_spans = [(start, end) for start, end, _ in _balanced_formulas(text)]
        for match in QUOTED.finditer(text):
            output.append(_candidate(source, text, match.start(2), match.end(2), "sheet", evidence_id, score, rank))
        for match in MARKER.finditer(text):
            role = match.group(1).lower()
            raw_start, raw_end = match.start(2), match.end(2)
            while raw_start < raw_end and text[raw_start].isspace():
                raw_start += 1
            while raw_end > raw_start and text[raw_end - 1].isspace():
                raw_end -= 1
            kind = {"sheet": "sheet", "worksheet": "sheet", "tab": "sheet", "new_sheet": "sheet", "range": "range", "target": "cell", "formula": "formula", "position": "number", "value": "number"}[role]
            if role == "target":
                values = []
                pointers = []
                for cell_match in CELL.finditer(text[raw_start:raw_end]):
                    start = raw_start + cell_match.start()
                    end = raw_start + cell_match.end()
                    values.append(text[start:end])
                    point = {"source": source, "span": [start, end]}
                    if evidence_id is not None:
                        point["evidence_id"] = evidence_id
                    pointers.append(point)
                if values:
                    output.append({
                        "source": source, "evidence_id": evidence_id, "span": [raw_start, raw_end],
                        "kind": "cells", "value": values, "pointers": pointers,
                        "evidence_score": float(score), "evidence_rank": int(rank),
                    })
            else:
                output.append(_candidate(source, text, raw_start, raw_end, kind, evidence_id, score, rank))
        for match in RANGE.finditer(text):
            output.append(_candidate(source, text, match.start(), match.end(), "range", evidence_id, score, rank))
        list_pattern = re.compile(r"\$?[A-Z]{1,3}\$?[1-9][0-9]*(?:\s*,\s*\$?[A-Z]{1,3}\$?[1-9][0-9]*)+")
        for match in list_pattern.finditer(text):
            if any(start <= match.start() and match.end() <= end for start, end in formula_spans):
                continue
            values = []
            pointers = []
            for cell_match in CELL.finditer(match.group(0)):
                start = match.start() + cell_match.start()
                end = match.start() + cell_match.end()
                values.append(text[start:end])
                point = {"source": source, "span": [start, end]}
                if evidence_id is not None:
                    point["evidence_id"] = evidence_id
                pointers.append(point)
            output.append({
                "source": source, "evidence_id": evidence_id, "span": [match.start(), match.end()],
                "kind": "cells", "value": values, "pointers": pointers,
                "evidence_score": float(score), "evidence_rank": int(rank),
            })
        for match in CELL.finditer(text):
            if any(r.start() <= match.start() and match.end() <= r.end() for r in RANGE.finditer(text)):
                continue
            if any(start <= match.start() and match.end() <= end for start, end in formula_spans):
                continue
            output.append(_candidate(source, text, match.start(), match.end(), "cell", evidence_id, score, rank))
        for start, end, value in _balanced_formulas(text):
            if not value.startswith("="):
                continue
            output.append(_candidate(source, text, start, end, "formula", evidence_id, score, rank))
        for match in re.finditer(r"\b(?:column\s+(?:at\s+|before\s+)?|at\s+)([A-Z]{1,3})\b", text, re.I):
            output.append(_candidate(source, text, match.start(1), match.end(1), "column", evidence_id, score, rank))
        for match in re.finditer(r"\b(?:row|position)\s+([1-9][0-9]*)\b", text, re.I):
            output.append(_candidate(source, text, match.start(1), match.end(1), "number", evidence_id, score, rank))
        for match in re.finditer(r"(?<![A-Za-z0-9])(-?[0-9]+(?:\.[0-9]+)?)(?![A-Za-z0-9])", text):
            output.append(_candidate(source, text, match.start(1), match.end(1), "number", evidence_id, score, rank))

    collect("instruction", instruction)
    for item in evidence:
        collect("evidence", item["text"], item["block_id"], item.get("score", 0.0), item.get("rank", 0))
    unique = {}
    for candidate in output:
        key = (
            candidate["source"], candidate.get("evidence_id"), tuple(candidate["span"]),
            candidate["kind"], json.dumps(candidate["value"], sort_keys=True),
        )
        unique[key] = candidate
    return list(unique.values())


def candidates_for_role(candidates, role, clause_bounds=None):
    output = []
    for candidate in candidates:
        if candidate["kind"] not in ROLE_KINDS[role]:
            continue
        if clause_bounds is not None and candidate["source"] == "instruction":
            if not (clause_bounds[0] <= candidate["span"][0] and candidate["span"][1] <= clause_bounds[1]):
                continue
        if role == "targets" and candidate["kind"] == "cell":
            candidate = dict(candidate)
            candidate["kind"] = "cells"
            candidate["value"] = [candidate["value"]]
            candidate["pointers"] = [candidate["pointers"]]
        output.append(candidate)
    return output


def _chunks(tokenizer, text, max_length=512, overlap=32):
    ids = tokenizer.encode(text, add_special_tokens=False, truncation=False)
    capacity = max_length - 2
    if len(ids) <= capacity:
        return [text], {"tokens": len(ids) + 2, "chunks": 1, "truncated": False}
    chunks = []
    step = capacity - overlap
    for start in range(0, len(ids), step):
        chunk_ids = ids[start:start + capacity]
        chunks.append(tokenizer.decode(chunk_ids, skip_special_tokens=True, clean_up_tokenization_spaces=False))
        if start + capacity >= len(ids):
            break
    return chunks, {"tokens": len(ids) + 2, "chunks": len(chunks), "truncated": False}


class FrozenEncoder:
    def __init__(self, encoder, max_length=512):
        self.encoder = encoder
        self.tokenizer = encoder.tokenizer
        self.dimension = encoder.get_sentence_embedding_dimension()
        self.max_length = max_length
        if int(encoder[0].auto_model.config.max_position_embeddings) < max_length:
            raise ValueError("ENCODER_POSITION_LIMIT")
        self.encoder.max_seq_length = max_length
        self.cache = {}
        self.audit = {}

    def prime(self, texts, batch_size=32):
        pending = []
        owners = []
        for text in dict.fromkeys(texts):
            if text in self.cache:
                continue
            chunks, audit = _chunks(self.tokenizer, text, self.max_length)
            self.audit[text] = audit
            for chunk in chunks:
                pending.append(chunk)
                owners.append(text)
        if not pending:
            return
        values = self.encoder.encode(
            pending, batch_size=batch_size, normalize_embeddings=True,
            convert_to_numpy=True, precision="float32", show_progress_bar=False,
        ).astype(np.float32)
        grouped = {}
        for owner, value in zip(owners, values):
            grouped.setdefault(owner, []).append(value)
        for owner, owner_values in grouped.items():
            combined = np.stack(owner_values).mean(axis=0)
            norm = np.linalg.norm(combined)
            self.cache[owner] = combined / norm if norm else combined

    def encode_one(self, text):
        if text not in self.cache:
            chunks, audit = _chunks(self.tokenizer, text, self.max_length)
            values = self.encoder.encode(
                chunks, normalize_embeddings=True, convert_to_numpy=True,
                precision="float32", show_progress_bar=False,
            ).astype(np.float32)
            combined = values.mean(axis=0)
            norm = np.linalg.norm(combined)
            self.cache[text] = combined / norm if norm else combined
            self.audit[text] = audit
        return self.cache[text]

    def encode_many(self, texts):
        return np.stack([self.encode_one(text) for text in texts]).astype(np.float32)


def evidence_vector(frozen, evidence):
    if not evidence:
        return np.zeros(frozen.dimension, np.float32), np.zeros(4, np.float32)
    vectors = frozen.encode_many([item["text"] for item in evidence])
    scores = np.asarray([float(item.get("score", 0.0)) for item in evidence], np.float32)
    ranks = np.asarray([float(item.get("rank", index + 1)) for index, item in enumerate(evidence)], np.float32)
    weights = np.exp(scores - 0.08 * (ranks - 1) - np.max(scores - 0.08 * (ranks - 1)))
    weights /= weights.sum()
    value = (vectors * weights[:, None]).sum(axis=0)
    norm = np.linalg.norm(value)
    value = value / norm if norm else value
    stats = np.asarray([len(evidence) / 5.0, scores.max(), scores.mean(), float((weights * ranks).sum()) / 5.0], np.float32)
    return value.astype(np.float32), stats


def operation_feature(frozen, text, evidence):
    instruction = frozen.encode_one(text)
    context, stats = evidence_vector(frozen, evidence)
    return np.concatenate([instruction, context, instruction * context, np.abs(instruction - context), stats]).astype(np.float32)


def candidate_feature(frozen, clause, evidence, candidate, operation, role, instruction_length):
    clause_vector = frozen.encode_one(clause)
    context, _ = evidence_vector(frozen, evidence)
    value_text = ", ".join(candidate["value"]) if isinstance(candidate["value"], list) else str(candidate["value"])
    candidate_vector = frozen.encode_one(value_text)
    op = np.zeros(len(OPERATIONS), np.float32)
    op[OPERATIONS.index(operation)] = 1.0
    role_vector = np.zeros(len(ROLES), np.float32)
    role_vector[ROLES.index(role)] = 1.0
    kind = np.zeros(len(KINDS), np.float32)
    kind[KINDS.index(candidate["kind"])] = 1.0
    source = np.asarray([candidate["source"] == "instruction", candidate["source"] == "evidence"], np.float32)
    denominator = max(1, instruction_length if candidate["source"] == "instruction" else candidate["span"][1])
    position = np.asarray([candidate["span"][0] / denominator, candidate["span"][1] / denominator], np.float32)
    metadata = np.asarray([candidate.get("evidence_score", 0.0), candidate.get("evidence_rank", 0) / 5.0], np.float32)
    return np.concatenate([clause_vector, context, candidate_vector, clause_vector * candidate_vector, op, role_vector, kind, source, position, metadata]).astype(np.float32)


class OperationHead(torch.nn.Module):
    def __init__(self, input_dimension, hidden_dimension=128):
        super().__init__()
        self.network = torch.nn.Sequential(
            torch.nn.LayerNorm(input_dimension), torch.nn.Linear(input_dimension, hidden_dimension),
            torch.nn.GELU(), torch.nn.Linear(hidden_dimension, len(OPERATIONS)),
        )

    def forward(self, values):
        return self.network(values)


class ArgumentRanker(torch.nn.Module):
    def __init__(self, input_dimension, hidden_dimension=128):
        super().__init__()
        self.network = torch.nn.Sequential(
            torch.nn.LayerNorm(input_dimension), torch.nn.Linear(input_dimension, hidden_dimension),
            torch.nn.GELU(), torch.nn.Linear(hidden_dimension, 1),
        )

    def forward(self, values):
        return self.network(values).squeeze(-1)


def _pointer_text(pointer_value, instruction, evidence):
    if pointer_value["source"] == "instruction":
        text = instruction
    else:
        matching = [item for item in evidence if item["block_id"] == pointer_value.get("evidence_id")]
        if len(matching) != 1:
            raise ValueError("EVIDENCE_POINTER_IDENTITY")
        text = matching[0]["text"]
    start, end = pointer_value["span"]
    if not 0 <= start < end <= len(text):
        raise ValueError("POINTER_BOUNDS")
    return text[start:end]


def validate_program(program, instruction, evidence):
    if program["schema_version"] != "structured_program_v2_dev":
        raise ValueError("PROGRAM_SCHEMA")
    if program["instruction_sha256"] != sha256(instruction.encode("utf8")).hexdigest():
        raise ValueError("INSTRUCTION_IDENTITY")
    if program["refusal"]:
        if program["actions"] or program["states"] != ["START", "REFUSE", "EOS"]:
            raise ValueError("REFUSAL_TRANSITION")
        return program
    if not 1 <= len(program["actions"]) <= 32:
        raise ValueError("PROGRAM_CARDINALITY")
    expected_states = ["START"]
    for index in range(len(program["actions"])):
        expected_states.extend(["ACTION", "CONTINUE" if index + 1 < len(program["actions"]) else "STOP"])
    expected_states.append("EOS")
    if program["states"] != expected_states:
        raise ValueError("PROGRAM_TRANSITIONS")
    for index, action in enumerate(program["actions"]):
        operation = action["operation"]
        if operation not in ROLE_CONTRACT or set(action["arguments"]) != set(ROLE_CONTRACT[operation]):
            raise ValueError("ROLE_CONTRACT")
        if action["action_id"] != f"a{index + 1}" or action["depends_on"] != ([] if index == 0 else [f"a{index}"]):
            raise ValueError("DEPENDENCY_CONTRACT")
        for role, value in action["arguments"].items():
            pointers = action["pointers"][role]
            values = value if isinstance(value, list) else [value]
            pointers = pointers if isinstance(pointers, list) else [pointers]
            if len(values) != len(pointers):
                raise ValueError("ROLE_CARDINALITY")
            if [_pointer_text(item, instruction, evidence) for item in pointers] != [str(item) for item in values]:
                raise ValueError("POINTER_VALUE")
        if "formula" in action["arguments"] and not action["arguments"]["formula"].startswith("="):
            raise ValueError("FORMULA_LITERAL")
        if "targets" in action["arguments"] and (not action["arguments"]["targets"] or any(CELL.fullmatch(value) is None for value in action["arguments"]["targets"])):
            raise ValueError("TARGET_LITERAL")
        if "range" in action["arguments"] and RANGE.fullmatch(action["arguments"]["range"]) is None:
            raise ValueError("RANGE_LITERAL")
    return program


def compile_action_plan(program):
    steps = []
    requirements = []
    previous = None
    ordinal = 0
    for action in program["actions"]:
        args = action["arguments"]
        targets = args.get("targets", [None])
        for target in targets:
            ordinal += 1
            operation = action["operation"]
            source = {"cells": [], "range": None}
            destination = {"cells": [], "range": None, "new_sheet": None}
            arguments = {}
            if operation == "write_value":
                destination["cells"] = [target]
                arguments = {"expression": None, "literal_value": args["value"]}
            elif operation == "write_formula":
                destination["cells"] = [target]
                arguments = {"formula": args["formula"]}
            elif operation in {"insert_row", "delete_row", "insert_column"}:
                arguments = {"position": args["position"]}
                if operation == "delete_row":
                    source["cells"] = ["A" + str(args["position"])]
            elif operation in {"sort_range", "unmerge_range"}:
                source["range"] = args["range"]
                destination["range"] = args["range"]
                if operation == "sort_range":
                    arguments = {"sort_key": "first_column", "sort_order": "ascending"}
            elif operation == "copy_sheet":
                destination["new_sheet"] = args["new_sheet"]
            requirements.append(f"r{ordinal}")
            steps.append({
                "step_id": f"s{ordinal}",
                "action": {"schema_version": "action_dsl_v1", "operation": operation, "sheet": args["sheet"], "source": source, "target": destination, "arguments": arguments},
                "requirement_ids": [f"r{ordinal}"],
                "evidence_block_ids": sorted({pointer.get("evidence_id") for value in action["pointers"].values() for pointer in (value if isinstance(value, list) else [value]) if pointer.get("evidence_id")}) or ["instruction"],
                "depends_on": [] if previous is None else [previous],
            })
            previous = f"s{ordinal}"
    return {"schema_version": "action_plan_v1", "instruction_sha256": program["instruction_sha256"], "requirement_ids": requirements, "steps": steps}






def load_checkpoint(path):
    payload = torch.load(path, map_location="cpu", weights_only=True)
    operation_head = OperationHead(payload["operation_input_dimension"], payload["hidden_dimension"])
    argument_ranker = ArgumentRanker(payload["argument_input_dimension"], payload["hidden_dimension"])
    operation_head.load_state_dict(payload["operation_state"])
    argument_ranker.load_state_dict(payload["argument_state"])
    operation_head.eval()
    argument_ranker.eval()
    return payload, operation_head, argument_ranker
