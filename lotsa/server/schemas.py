"""Pydantic response schemas for the JSON API.

Converts internal dataclasses (TaskSummary, TaskDetail, MessageRow) into
JSON-serialisable Pydantic models for API responses.
"""

from __future__ import annotations

from datetime import datetime
from typing import Any, Literal

from pydantic import BaseModel

from lotsa.db import MessageRow
from lotsa.orchestrator import TaskDetail, TaskSummary
from lotsa.status import TaskStatusLiteral


class TaskSummaryResponse(BaseModel):
    """Lightweight task info for list views."""

    id: str
    title: str
    state: str
    priority: int
    created_at: str
    status: TaskStatusLiteral = "working"
    current_step: str | None = None
    is_conversational: bool = False
    elapsed_s: int = 0
    # ADR-029 — the task's project, for the list badge + project filter.
    project_id: str = "default"
    # ADR-017 soft-timeout indicator: ``ok`` (no dot) / ``warn`` (yellow) /
    # ``over`` (red). Mirrors ``TaskSummary.timeout_status``.
    timeout_status: Literal["ok", "warn", "over"] = "ok"
    metadata: dict[str, Any] = {}

    @classmethod
    def from_summary(cls, s: TaskSummary) -> TaskSummaryResponse:
        return cls(**{f: getattr(s, f) for f in cls.model_fields if hasattr(s, f)})


class TaskDetailResponse(TaskSummaryResponse):
    """Full task info — adds body, flow_name, work_dir, and project context."""

    body: str = ""
    flow_name: str = ""
    work_dir: str = ""
    # ADR-029 — project name/path surfaced in the task detail view.
    project_name: str = ""
    project_path: str = ""

    @classmethod
    def from_detail(cls, d: TaskDetail) -> TaskDetailResponse:
        return cls(**{f: getattr(d, f) for f in cls.model_fields if hasattr(d, f)})


# Per-message content cap in API responses. Messages above this byte length
# are returned truncated with a metadata flag so the React UI can offer a
# "fetch full content" affordance. The DB keeps the raw bytes; this is a
# response-shape concern only. Picked generously: chat messages are usually
# <5 KB, agent output up to ~30 KB; the cap kicks in only on pathological
# verbose-stream dumps that would otherwise blow the browser's JSON parser.
_MAX_MESSAGE_CONTENT_BYTES = 50_000
_HEAD_BYTES = 5_000
_TAIL_BYTES = 1_000


class MessageResponse(BaseModel):
    """A single message in the task conversation log."""

    id: int
    task_id: str
    role: str
    step_name: str
    content: str
    type: str
    metadata: dict[str, Any] = {}
    created_at: str

    @classmethod
    def from_row(cls, r: MessageRow) -> MessageResponse:
        kwargs = {f: getattr(r, f) for f in cls.model_fields if hasattr(r, f)}
        content = kwargs.get("content", "")
        if isinstance(content, str):
            encoded = content.encode("utf-8")
            if len(encoded) > _MAX_MESSAGE_CONTENT_BYTES:
                head = encoded[:_HEAD_BYTES].decode("utf-8", errors="ignore")
                tail = encoded[-_TAIL_BYTES:].decode("utf-8", errors="ignore")
                marker = f"\n\n…[truncated: {len(encoded)} bytes total — fetch via /raw for full content]\n\n"
                kwargs["content"] = head + marker + tail
                # Don't mutate the underlying row.metadata — spread into a new dict.
                existing_meta = kwargs.get("metadata") or {}
                kwargs["metadata"] = {
                    **existing_meta,
                    "content_truncated": True,
                    "original_length": len(encoded),
                }
        return cls(**kwargs)


class FlowStepResponse(BaseModel):
    """One step in a flow definition."""

    name: str
    conversational: bool
    evaluate: bool = False
    output: str | None = None
    inputs: list[str] = []
    # Whether the operator can Accept this step to advance it (output artifact,
    # evaluate gate, or a conversational step with a forward advance rule, e.g.
    # verify). The chat panel shows the Accept button iff this is true.
    is_gate: bool = False


class FlowResponse(BaseModel):
    """Full flow definition for display."""

    name: str
    steps: list[FlowStepResponse]
    gate_states: list[str]


class DiffResponse(BaseModel):
    """Response for GET /api/tasks/{id}/diff."""

    diff: str | None


class TotalsResponse(BaseModel):
    """Token/cost totals for a task."""

    total_duration_s: int = 0
    total_tokens: int = 0
    total_cost_usd: float = 0.0
    display: str = ""


class AvailableOverride(BaseModel):
    """One guard-override action currently applicable to a task (ADR-019)."""

    guard_name: str
    label: str
    description: str


class AgentActivityEventResponse(BaseModel):
    """One in-flight agent activity event (ADR-017).

    Mirrors :class:`rigg.models.ActivityEvent`. ``truncated`` is lifted out
    of the event's ``detail`` dict so the UI can flag a clipped preview without
    inspecting the payload.
    """

    index: int
    timestamp: datetime
    # Mirror ``rigg.models.ActivityEvent.kind`` so an unexpected kind from
    # a future JSONL format is rejected here rather than reaching the UI's
    # KIND_LABEL/KIND_CLASS lookups as an undefined key.
    kind: Literal["thinking", "tool_use", "tool_result", "text", "system"]
    summary: str
    detail: dict[str, Any] | None = None
    truncated: bool = False


class AgentActivityResponse(BaseModel):
    """Response for GET /api/tasks/{id}/agent-activity (ADR-017).

    Degrades rather than 500s: ``session_id`` is ``None`` before the task has
    dispatched, ``runner_supports_activity`` is ``False`` for runners without a
    ``read_activity`` implementation, and ``events`` is empty on a missing file
    or parse error.
    """

    session_id: str | None
    runner_supports_activity: bool
    session_complete: bool
    events: list[AgentActivityEventResponse]
    next_index: int


class TaskDetailFullResponse(BaseModel):
    """Composite response for GET /api/tasks/{id}."""

    task: TaskDetailResponse
    messages: list[MessageResponse]
    question: str | None
    flow: FlowResponse | None
    artifacts: dict[str, str]
    next_step_name: str | None
    totals: TotalsResponse
    # Guard overrides whose detect() is True for this task — populated only on
    # the full detail response (ADR-019 D6), never on the summary/sidebar list,
    # so the per-handler detect() cost is paid once per detail load.
    available_overrides: list[AvailableOverride] = []
