from __future__ import annotations
from hashlib import sha256
from pathlib import Path
import json


FORBIDDEN_FIELDS = frozenset({"answer_position", "answer_sheet", "data_position", "criteria", "exclude",
                              "operation_family", "controlled_task_id", "allowed_change_mask", "reference_action",
                              "task_label", "task_answer", "evaluation_payload"})


class ExecutionContractError(ValueError):
    pass


def canonical(value):
    return json.dumps(value, ensure_ascii=False, sort_keys=True, separators=(",", ":"), allow_nan=False).encode("utf8")


def value_hash(value):
    return sha256(canonical(value)).hexdigest()


def file_hash(path):
    result = sha256()
    with Path(path).open("rb") as handle:
        for chunk in iter(lambda: handle.read(1024 * 1024), b""):
            result.update(chunk)
    return result.hexdigest()


def write_json(path, value):
    with Path(path).open("x", encoding="utf8") as handle:
        json.dump(value, handle, ensure_ascii=False, indent=2, allow_nan=False)


def require(condition, message):
    if not condition:
        raise ExecutionContractError(message)


def oracle_free(value, location="payload"):
    if isinstance(value, dict):
        for key, item in value.items():
            lower = str(key).casefold()
            forbidden = (lower in FORBIDDEN_FIELDS or lower.startswith(("oracle", "golden", "gold_", "answer_"))
                         or (lower.startswith("expected_") and lower != "expected_dimension"))
            require(not forbidden, f"Evaluation field prohibited at {location}.{key}")
            oracle_free(item, f"{location}.{key}")
    elif isinstance(value, (list, tuple)):
        for index, item in enumerate(value):
            oracle_free(item, f"{location}[{index}]")
