from __future__ import annotations

from dataclasses import dataclass, field, asdict


@dataclass
class AgentContext:
    question: str
    basename: str
    index_dir: str
    top_k: int = 5
    planner_output: dict = field(default_factory=dict)
    retriever_output: dict = field(default_factory=dict)
    verifier_output: dict = field(default_factory=dict)
    final_response: dict = field(default_factory=dict)
    agent_trace: list[dict] = field(default_factory=list)

    def to_dict(self) -> dict:
        return asdict(self)
