from __future__ import annotations

from dataclasses import dataclass, asdict


@dataclass
class AgentTraceEntry:
    step: int
    agent: str
    action: str
    input_summary: str
    output_summary: str

    def to_dict(self) -> dict:
        return asdict(self)


def append_trace(
    trace: list[dict],
    agent: str,
    action: str,
    input_summary: str,
    output_summary: str,
) -> list[dict]:
    entry = AgentTraceEntry(
        step=len(trace) + 1,
        agent=agent,
        action=action,
        input_summary=input_summary,
        output_summary=output_summary,
    )
    trace.append(entry.to_dict())
    return trace
