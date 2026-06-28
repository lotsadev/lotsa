"""Tests for Rigg core models."""

from rigg.models import (
    AgentResult,
    Assertion,
    BlockingReason,
    DispatchResult,
    Item,
    Proof,
    ReviewStatus,
    RunRecord,
    ValidationResult,
)


def test_item_defaults():
    item = Item(id="123", state="backlog")
    assert item.id == "123"
    assert item.state == "backlog"
    assert item.priority == 0
    assert item.title == ""
    assert item.body == ""
    assert item.metadata == {}


def test_item_metadata_isolation():
    """Each Item gets its own metadata dict."""
    a = Item(id="1", state="x")
    b = Item(id="2", state="x")
    a.metadata["key"] = "val"
    assert "key" not in b.metadata


def test_agent_result_minimal():
    r = AgentResult(success=True, stdout="ok", stderr="", return_code=0, duration_ms=100)
    assert r.cost_usd is None
    assert r.model is None
    assert r.session_id is None


def test_agent_result_with_session():
    r = AgentResult(
        success=True,
        stdout="",
        stderr="",
        return_code=0,
        duration_ms=500,
        session_id="ses-abc",
    )
    assert r.session_id == "ses-abc"


def test_review_status_values():
    assert ReviewStatus.PENDING == "pending"
    assert ReviewStatus.CLEAN == "clean"
    assert ReviewStatus.FEEDBACK == "feedback"


def test_proof_structure():
    p = Proof(
        title="API test",
        body="Response: 200 OK",
        assertions=[Assertion(description="status 200", passed=True)],
        artifacts=[],
    )
    assert p.assertions[0].passed is True


def test_validation_result():
    v = ValidationResult(valid=False, missing_sections=["Proof"], placeholder_detected=True, details="missing")
    assert v.valid is False
    assert v.placeholder_detected is True


def test_blocking_reason():
    br = BlockingReason(code="AGENT_CRASH", title="Agent crashed", message="Exit 1", context={"exit": 1})
    assert br.code == "AGENT_CRASH"


def test_dispatch_result_idle():
    dr = DispatchResult(job_type=None, item=None, agent_result=None)
    assert dr.job_type is None


def test_dispatch_result_with_work():
    item = Item(id="1", state="coding")
    ar = AgentResult(success=True, stdout="", stderr="", return_code=0, duration_ms=100)
    dr = DispatchResult(job_type="coding", item=item, agent_result=ar)
    assert dr.job_type == "coding"
    assert dr.item is item


def test_run_record():
    from datetime import UTC, datetime

    now = datetime.now(UTC)
    ar = AgentResult(success=True, stdout="", stderr="", return_code=0, duration_ms=100)
    rr = RunRecord(
        item_id="1",
        job_type="coding",
        agent_type="claude-code",
        result=ar,
        started_at=now,
        completed_at=now,
    )
    assert rr.session_id is None
    assert rr.parent_session_id is None
