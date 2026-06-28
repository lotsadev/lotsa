"""Tests for lotsa.server.schemas — Pydantic response models."""

from __future__ import annotations

from lotsa.db import MessageRow
from lotsa.orchestrator import TaskDetail, TaskSummary
from lotsa.server.schemas import (
    FlowResponse,
    FlowStepResponse,
    MessageResponse,
    TaskDetailFullResponse,
    TaskDetailResponse,
    TaskSummaryResponse,
)


def test_task_summary_from_dataclass():
    summary = TaskSummary(
        id="abc123",
        title="Fix the bug",
        state="backlog",
        priority=2,
        created_at="2026-06-13T10:00:00+00:00",
        status="working",
        current_step="coding",
        is_conversational=False,
        elapsed_s=42,
    )
    resp = TaskSummaryResponse.from_summary(summary)

    assert resp.id == "abc123"
    assert resp.title == "Fix the bug"
    assert resp.state == "backlog"
    assert resp.priority == 2
    assert resp.status == "working"
    assert resp.current_step == "coding"
    assert resp.is_conversational is False
    assert resp.elapsed_s == 42
    # metadata defaults to {} when not on dataclass
    assert resp.metadata == {}
    assert resp.created_at == "2026-06-13T10:00:00+00:00"


def test_task_summary_from_dataclass_defaults():
    """Minimal TaskSummary — optional fields should get their defaults."""
    summary = TaskSummary(id="x", title="T", state="complete", priority=0, created_at="2026-01-01T00:00:00+00:00")
    resp = TaskSummaryResponse.from_summary(summary)

    assert resp.current_step is None
    assert resp.status == "working"
    assert resp.is_conversational is False
    assert resp.elapsed_s == 0
    assert resp.metadata == {}


def test_task_detail_from_dataclass():
    detail = TaskDetail(
        id="def456",
        title="Build feature",
        state="coding",
        priority=1,
        created_at="2026-06-13T10:00:00+00:00",
        status="waiting",
        current_step="coding",
        is_conversational=False,
        elapsed_s=0,
        body="Implement the widget",
        flow_name="standard",
        work_dir="/tmp/worktrees/def456",
    )
    resp = TaskDetailResponse.from_detail(detail)

    # Inherited fields
    assert resp.id == "def456"
    assert resp.title == "Build feature"
    assert resp.state == "coding"
    assert resp.priority == 1
    assert resp.status == "waiting"
    assert resp.current_step == "coding"
    assert resp.created_at == "2026-06-13T10:00:00+00:00"
    # Added fields
    assert resp.body == "Implement the widget"
    assert resp.flow_name == "standard"
    assert resp.work_dir == "/tmp/worktrees/def456"


def test_task_detail_inherits_summary_fields():
    """TaskDetailResponse is a superset of TaskSummaryResponse."""
    assert issubclass(TaskDetailResponse, TaskSummaryResponse)


def test_message_from_row():
    row = MessageRow(
        id=7,
        task_id="abc123",
        role="agent",
        step_name="coding",
        content="Here is the output",
        type="output",
        metadata={"duration_ms": 1500, "cost_usd": 0.02},
        created_at="2026-04-15T10:00:00+00:00",
    )
    resp = MessageResponse.from_row(row)

    assert resp.id == 7
    assert resp.task_id == "abc123"
    assert resp.role == "agent"
    assert resp.step_name == "coding"
    assert resp.content == "Here is the output"
    assert resp.type == "output"
    assert resp.metadata == {"duration_ms": 1500, "cost_usd": 0.02}
    assert resp.created_at == "2026-04-15T10:00:00+00:00"


def test_message_from_row_empty_metadata():
    row = MessageRow(
        id=1,
        task_id="t1",
        role="user",
        step_name="",
        content="Hello",
        type="chat",
        metadata={},
        created_at="2026-04-15T09:00:00+00:00",
    )
    resp = MessageResponse.from_row(row)
    assert resp.metadata == {}


# ---------------------------------------------------------------------------
# MessageResponse — per-message content truncation
# ---------------------------------------------------------------------------
#
# Rationale: agent output messages can be megabytes when the agent
# terminates abnormally with --verbose enabled. The DB keeps the raw
# bytes (audit log append-only); the API caps per-message content so
# the browser can parse the JSON envelope without blowing the heap.

_CAP_BYTES = 50_000


def _row_with_content(content: str, *, msg_id: int = 100) -> MessageRow:
    return MessageRow(
        id=msg_id,
        task_id="t-cap",
        role="agent",
        step_name="code",
        content=content,
        type="output",
        metadata={},
        created_at="2026-05-17T10:00:00+00:00",
    )


def test_message_response_under_cap_returns_full_content():
    """A normal-sized message round-trips verbatim, no truncation flag."""
    content = "ok" * 100  # 200 bytes
    resp = MessageResponse.from_row(_row_with_content(content))
    assert resp.content == content
    assert resp.metadata.get("content_truncated") is not True


def test_message_response_over_cap_truncates_head_and_tail():
    """A 100 KB message is truncated; serialized form stays under ~7 KB."""
    content = "x" * 100_000
    resp = MessageResponse.from_row(_row_with_content(content))
    serialized_size = len(resp.content.encode("utf-8"))
    assert serialized_size <= 7_000, f"Expected truncated content ≤7 KB, got {serialized_size}"
    # Truncation marker visible to the operator
    assert "truncated" in resp.content.lower()


def test_message_response_over_cap_sets_metadata_flags():
    """Truncation sets `content_truncated` and `original_length` on metadata."""
    content = "y" * 100_000
    resp = MessageResponse.from_row(_row_with_content(content))
    assert resp.metadata.get("content_truncated") is True
    assert resp.metadata.get("original_length") == 100_000


def test_message_response_cap_boundary_exact():
    """A message exactly at the cap is NOT truncated."""
    content = "z" * _CAP_BYTES
    resp = MessageResponse.from_row(_row_with_content(content))
    assert len(resp.content.encode("utf-8")) == _CAP_BYTES
    assert resp.metadata.get("content_truncated") is not True


def test_message_response_cap_boundary_over_by_one():
    """One byte over the cap triggers truncation."""
    content = "z" * (_CAP_BYTES + 1)
    resp = MessageResponse.from_row(_row_with_content(content))
    assert resp.metadata.get("content_truncated") is True
    assert resp.metadata.get("original_length") == _CAP_BYTES + 1


def test_message_response_preserves_existing_metadata_on_truncation():
    """The existing metadata dict must not be clobbered when we add flags."""
    row = MessageRow(
        id=200,
        task_id="t-cap",
        role="agent",
        step_name="code",
        content="x" * 100_000,
        type="output",
        metadata={"duration_ms": 1500, "cost_usd": 0.42},
        created_at="2026-05-17T10:00:00+00:00",
    )
    resp = MessageResponse.from_row(row)
    assert resp.metadata.get("duration_ms") == 1500
    assert resp.metadata.get("cost_usd") == 0.42
    assert resp.metadata.get("content_truncated") is True
    assert resp.metadata.get("original_length") == 100_000


def test_flow_response_construction():
    steps = [
        FlowStepResponse(name="spec", conversational=True, output="spec", inputs=[]),
        FlowStepResponse(name="coding", conversational=False, output=None, inputs=["spec"]),
        FlowStepResponse(name="review", conversational=False, output=None, inputs=[]),
    ]
    flow = FlowResponse(name="standard", steps=steps, gate_states=["specd", "coded"])

    assert flow.name == "standard"
    assert len(flow.steps) == 3
    assert flow.steps[0].name == "spec"
    assert flow.steps[0].conversational is True
    assert flow.steps[0].output == "spec"
    assert flow.steps[1].inputs == ["spec"]
    assert flow.gate_states == ["specd", "coded"]

    # Verify round-trip serialization
    data = flow.model_dump()
    assert data["name"] == "standard"
    assert data["steps"][0]["name"] == "spec"
    assert data["gate_states"] == ["specd", "coded"]

    # Verify JSON serialization doesn't raise
    json_str = flow.model_dump_json()
    assert "standard" in json_str


def test_task_detail_full_response_construction():
    task_resp = TaskDetailResponse(
        id="t1",
        title="Test",
        state="complete",
        priority=0,
        created_at="2026-06-13T10:00:00+00:00",
        body="",
        flow_name="simple",
        work_dir="",
    )
    msg_resp = MessageResponse(
        id=1,
        task_id="t1",
        role="user",
        step_name="",
        content="hello",
        type="chat",
        metadata={},
        created_at="2026-04-15T00:00:00+00:00",
    )
    full = TaskDetailFullResponse(
        task=task_resp,
        messages=[msg_resp],
        question=None,
        flow=None,
        artifacts={"spec": "the spec content"},
        next_step_name=None,
        totals={"total_tokens": 0, "display": ""},
    )

    assert full.task.id == "t1"
    assert len(full.messages) == 1
    assert full.question is None
    assert full.flow is None
    assert full.artifacts == {"spec": "the spec content"}
    assert full.totals.display == ""


def test_task_summary_response_has_status_and_current_step():
    from lotsa.orchestrator import TaskSummary
    from lotsa.server.schemas import TaskSummaryResponse

    s = TaskSummary(
        id="x",
        title="T",
        state="coding",
        priority=0,
        created_at="2026-06-13T08:00:00+00:00",
        status="waiting",
        current_step="coding",
    )
    r = TaskSummaryResponse.from_summary(s)
    assert r.status == "waiting"
    assert r.current_step == "coding"


def test_task_summary_response_drops_legacy_flags():
    from lotsa.server.schemas import TaskSummaryResponse

    fields = TaskSummaryResponse.model_fields
    for legacy in ("is_spec_complete", "has_question", "needs_review", "is_running", "step_name"):
        assert legacy not in fields, f"{legacy} should be removed"


def test_task_summary_created_at_flows_to_response():
    """created_at from the DB row must reach the API response so the dashboard
    can display task start time without a separate round-trip."""
    from lotsa.orchestrator import TaskSummary
    from lotsa.server.schemas import TaskSummaryResponse

    iso = "2026-05-01T09:30:00+00:00"
    s = TaskSummary(
        id="ts-1",
        title="Show timestamps",
        state="coding",
        priority=0,
        created_at=iso,
    )
    r = TaskSummaryResponse.from_summary(s)

    assert r.created_at == iso


def test_task_detail_created_at_inherited_from_summary():
    """TaskDetailResponse inherits created_at from TaskSummaryResponse —
    both the dataclass inheritance and the Pydantic inheritance carry the
    field so a single code path populates it."""
    from lotsa.orchestrator import TaskDetail
    from lotsa.server.schemas import TaskDetailResponse

    iso = "2026-05-15T14:00:00+00:00"
    d = TaskDetail(
        id="td-1",
        title="Detail timestamps",
        state="complete",
        priority=1,
        created_at=iso,
        body="body",
        flow_name="full",
        work_dir="/tmp/td-1",
    )
    r = TaskDetailResponse.from_detail(d)

    assert r.created_at == iso
    # Verify the field is present on the model itself (not just the instance value).
    assert "created_at" in TaskDetailResponse.model_fields
