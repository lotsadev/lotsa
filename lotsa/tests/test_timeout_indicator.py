"""Tests for the soft-timeout indicator (ADR-017).

Covers the new ``timeout_warn_seconds`` / ``timeout_kill_seconds`` job fields,
the orchestrator's ``_timeout_status`` computation, and the ``timeout_status``
field on the task summary dataclass and its Pydantic mirror.
"""

from __future__ import annotations

from types import SimpleNamespace

import pytest

from lotsa.flows import build_process


def _write_process(tmp_path, body: str):
    p = tmp_path / "process.yaml"
    p.write_text(body)
    return p


# ---------------------------------------------------------------------------
# Flow job fields
# ---------------------------------------------------------------------------


def test_job_parses_timeout_fields(tmp_path):
    path = _write_process(
        tmp_path,
        """
process: custom
jobs:
  - name: code
    timeout_warn_seconds: 1200
    timeout_kill_seconds: 3600
flows:
  main:
    steps:
      - code
""",
    )
    process = build_process("custom", process_file=path)
    code = next(j for j in process.flows["main"].jobs if j.name == "code")
    assert code.timeout_warn_seconds == 1200
    assert code.timeout_kill_seconds == 3600


def test_job_timeout_fields_default_none(tmp_path):
    path = _write_process(
        tmp_path,
        """
process: custom
jobs:
  - name: code
flows:
  main:
    steps:
      - code
""",
    )
    process = build_process("custom", process_file=path)
    code = next(j for j in process.flows["main"].jobs if j.name == "code")
    assert code.timeout_warn_seconds is None
    assert code.timeout_kill_seconds is None


# ---------------------------------------------------------------------------
# _timeout_status computation
# ---------------------------------------------------------------------------


@pytest.mark.parametrize(
    "elapsed,expected",
    [(0, "ok"), (1199, "ok"), (1200, "warn"), (3599, "warn"), (3600, "over"), (9999, "over")],
)
def test_timeout_status_thresholds(elapsed, expected):
    from lotsa.orchestrator import OrchestratorService

    step = SimpleNamespace(timeout_warn_seconds=1200, timeout_kill_seconds=3600)
    assert OrchestratorService._timeout_status(elapsed, step) == expected


def test_timeout_status_no_thresholds_is_ok():
    from lotsa.orchestrator import OrchestratorService

    step = SimpleNamespace(timeout_warn_seconds=None, timeout_kill_seconds=None)
    assert OrchestratorService._timeout_status(99999, step) == "ok"


def test_timeout_status_no_step_is_ok():
    from lotsa.orchestrator import OrchestratorService

    assert OrchestratorService._timeout_status(99999, None) == "ok"


# ---------------------------------------------------------------------------
# Dataclass + schema fields
# ---------------------------------------------------------------------------


def test_task_summary_defaults_timeout_status_ok():
    from lotsa.orchestrator import TaskSummary

    s = TaskSummary(id="t1", title="T", state="coding", priority=0, created_at="2026-06-14")
    assert s.timeout_status == "ok"


def test_task_summary_response_exposes_timeout_status():
    from lotsa.server.schemas import TaskSummaryResponse

    assert "timeout_status" in TaskSummaryResponse.model_fields
