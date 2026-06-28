"""Core data types used across all Rigg components."""

from __future__ import annotations

from dataclasses import dataclass, field
from datetime import datetime
from enum import StrEnum
from pathlib import Path
from typing import Any, Literal


@dataclass
class Item:
    """A work item from any backend (GitHub issue, DB row, etc.)."""

    id: str
    state: str
    priority: int = 0
    title: str = ""
    body: str = ""
    metadata: dict = field(default_factory=dict)


@dataclass
class AgentResult:
    """Result of an agent invocation."""

    success: bool
    stdout: str
    stderr: str
    return_code: int
    duration_ms: int
    cost_usd: float | None = None
    input_tokens: int | None = None
    output_tokens: int | None = None
    model: str | None = None
    session_id: str | None = None


@dataclass
class ActivityEvent:
    """One projected event from an agent runner's native session persistence.

    See ADR-017. ``index`` is monotonic per session (the client passes it back
    as ``since_index`` for incremental polling); ``detail`` is a kind-specific,
    truncated structured payload (full content stays in the on-disk JSONL).
    """

    index: int
    timestamp: datetime
    kind: Literal["thinking", "tool_use", "tool_result", "text", "system"]
    summary: str
    detail: dict[str, Any] | None = None


@dataclass
class ActivityResult:
    """A batch of activity events read from a runner's session persistence.

    ``supported=False`` means the runner has no ``read_activity`` implementation
    (the dashboard shows an "unavailable" empty state). ``next_index`` is the
    value the caller passes as ``since_index`` on the next poll.
    """

    events: list[ActivityEvent]
    supported: bool = False
    session_complete: bool = False
    next_index: int = 0


class ReviewStatus(StrEnum):
    PENDING = "pending"
    CLEAN = "clean"
    FEEDBACK = "feedback"


@dataclass
class Assertion:
    description: str
    passed: bool


@dataclass
class Proof:
    title: str
    body: str
    assertions: list[Assertion]
    artifacts: list[Path]


@dataclass
class ValidationResult:
    valid: bool
    missing_sections: list[str]
    placeholder_detected: bool
    details: str


@dataclass
class BlockingReason:
    code: str
    title: str
    message: str
    context: dict


@dataclass
class DispatchResult:
    """What happened during a dispatch cycle."""

    job_type: str | None
    item: Item | None
    agent_result: AgentResult | None


@dataclass
class RunRecord:
    """Audit trail entry for agent runs."""

    item_id: str
    job_type: str
    agent_type: str
    result: AgentResult
    started_at: datetime
    completed_at: datetime
    session_id: str | None = None
    parent_session_id: str | None = None
