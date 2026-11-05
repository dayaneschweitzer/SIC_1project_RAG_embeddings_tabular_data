from __future__ import annotations

from dataclasses import dataclass
from hashlib import sha256

from .action_dsl import ActionDSL, ActionDSLValidationError, ActionSource, ActionTarget
from .action_dsl_adapter import dsl_to_change_plan


def action_from_dict(payload):
    if set(payload) != {"schema_version", "operation", "sheet", "source", "target", "arguments"}:
        raise ActionDSLValidationError("Unexpected action fields")
    return ActionDSL(operation=payload["operation"], sheet=payload["sheet"],
                     source=ActionSource(**payload["source"]), target=ActionTarget(**payload["target"]),
                     arguments=payload["arguments"], schema_version=payload["schema_version"])


@dataclass(frozen=True)
class PlanStep:
    step_id: str
    action: ActionDSL
    requirement_ids: tuple[str, ...]
    evidence_block_ids: tuple[str, ...]
    depends_on: tuple[str, ...] = ()

    def to_dict(self):
        return {"step_id": self.step_id, "action": self.action.to_dict(),
                "requirement_ids": list(self.requirement_ids), "evidence_block_ids": list(self.evidence_block_ids),
                "depends_on": list(self.depends_on)}


@dataclass(frozen=True)
class PreservedRequirement:
    requirement_id: str
    sheet: str
    coordinate: str
    predicate: dict
    evidence_block_ids: tuple[str, ...]

    def __post_init__(self):
        from .semantic_ir import Expression, ValueType
        if not self.requirement_id or not isinstance(self.sheet, str) or not self.sheet:
            raise ActionDSLValidationError("Invalid preserved requirement")
        ActionSource(cells=(self.coordinate,))
        if Expression.from_dict(self.predicate).value_type != ValueType.BOOLEAN:
            raise ActionDSLValidationError("Preservation requires a boolean predicate")
        if not self.evidence_block_ids or any(not isinstance(v, str) or not v for v in self.evidence_block_ids):
            raise ActionDSLValidationError("Preservation requires evidence")

    def to_dict(self):
        return {"requirement_id": self.requirement_id, "sheet": self.sheet, "coordinate": self.coordinate,
                "predicate": self.predicate, "predicate_result": False,
                "evidence_block_ids": list(self.evidence_block_ids)}

    @classmethod
    def from_dict(cls, value):
        if set(value) != {"requirement_id", "sheet", "coordinate", "predicate", "predicate_result", "evidence_block_ids"} or value["predicate_result"] is not False:
            raise ActionDSLValidationError("Invalid preservation proof")
        return cls(value["requirement_id"], value["sheet"], value["coordinate"], value["predicate"],
                   tuple(value["evidence_block_ids"]))


@dataclass(frozen=True)
class ActionPlan:
    instruction_sha256: str
    requirement_ids: tuple[str, ...]
    steps: tuple[PlanStep, ...]
    preserved_requirements: tuple[PreservedRequirement, ...] = ()

    def __post_init__(self):
        if len(self.instruction_sha256) != 64 or any(c not in "0123456789abcdef" for c in self.instruction_sha256):
            raise ActionDSLValidationError("Invalid instruction hash")
        if not self.requirement_ids or len(set(self.requirement_ids)) != len(self.requirement_ids):
            raise ActionDSLValidationError("Requirements must be nonempty and unique")
        if not 1 <= len(self.steps) <= 1024:
            raise ActionDSLValidationError("Plan must contain between one and 1024 steps")
        seen, covered = set(), set()
        for index, step in enumerate(self.steps):
            if not isinstance(step, PlanStep) or not step.step_id or step.step_id in seen:
                raise ActionDSLValidationError("Duplicate or invalid step")
            if len(set(step.depends_on)) != len(step.depends_on) or not set(step.depends_on) <= seen:
                raise ActionDSLValidationError("Dependencies must identify earlier steps")
            if index and self.steps[index - 1].step_id not in step.depends_on:
                raise ActionDSLValidationError("Ordered plan must depend on the preceding step")
            if not step.requirement_ids or not set(step.requirement_ids) <= set(self.requirement_ids):
                raise ActionDSLValidationError("Unbound requirement in step")
            if not step.evidence_block_ids or any(not isinstance(v, str) or not v for v in step.evidence_block_ids):
                raise ActionDSLValidationError("Each step requires evidence")
            if index < len(self.steps) - 1 and step.action.operation in {"insert_row", "insert_column", "sort_range", "unmerge_range"}:
                raise ActionDSLValidationError("Nonterminal coordinate-changing actions are not qualified")
            seen.add(step.step_id)
            covered.update(step.requirement_ids)
        preserved = set()
        for proof in self.preserved_requirements:
            if not isinstance(proof, PreservedRequirement) or proof.requirement_id in preserved or proof.requirement_id in covered:
                raise ActionDSLValidationError("Duplicate or conflicting preserved requirement")
            preserved.add(proof.requirement_id)
        if covered | preserved != set(self.requirement_ids):
            raise ActionDSLValidationError("Plan omits an instruction requirement")

    def to_dict(self):
        result = {"schema_version": "action_plan_v2" if self.preserved_requirements else "action_plan_v1",
                  "instruction_sha256": self.instruction_sha256, "requirement_ids": list(self.requirement_ids),
                  "steps": [s.to_dict() for s in self.steps]}
        if self.preserved_requirements:
            result["preserved_requirements"] = [p.to_dict() for p in self.preserved_requirements]
        return result

    def to_change_plan(self):
        result = {"schema_version": "composite_change_plan_v2" if self.preserved_requirements else "composite_change_plan_v1", "operation": "sequence", "status": "planned",
                "instruction_sha256": self.instruction_sha256, "requirement_ids": list(self.requirement_ids),
                "steps": [{**s.to_dict(), "change_plan": dsl_to_change_plan(s.action)} for s in self.steps], "apply_xlsx": False}
        if self.preserved_requirements:
            result["preserved_requirements"] = [p.to_dict() for p in self.preserved_requirements]
        return result

    @classmethod
    def from_dict(cls, value):
        expected = {"schema_version", "instruction_sha256", "requirement_ids", "steps"}
        version = value.get("schema_version")
        if version == "action_plan_v2":
            expected.add("preserved_requirements")
        if set(value) != expected or version not in {"action_plan_v1", "action_plan_v2"}:
            raise ActionDSLValidationError("Invalid action plan schema")
        steps = []
        for step in value["steps"]:
            if set(step) != {"step_id", "action", "requirement_ids", "evidence_block_ids", "depends_on"}:
                raise ActionDSLValidationError("Unexpected step fields")
            steps.append(PlanStep(step["step_id"], action_from_dict(step["action"]), tuple(step["requirement_ids"]),
                                  tuple(step["evidence_block_ids"]), tuple(step["depends_on"])))
        return cls(value["instruction_sha256"], tuple(value["requirement_ids"]), tuple(steps),
                   tuple(PreservedRequirement.from_dict(p) for p in value.get("preserved_requirements", [])))

    def bind_instruction(self, instruction):
        if sha256(instruction.encode("utf8")).hexdigest() != self.instruction_sha256:
            raise ActionDSLValidationError("Plan does not bind the original instruction")
