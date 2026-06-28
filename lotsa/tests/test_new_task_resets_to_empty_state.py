"""Acceptance tests for 'New Task reveals the EmptyState instead of creating a
junk task'.

These tests verify the acceptance criteria from the implementation plan:

  * Clicking **New Task** in the sidebar no longer calls ``createTask`` / fires
    an agent — ``handleNewTask`` becomes a pure, synchronous selection reset
    (``onSelectTask(null)``), which causes ``app-layout.tsx`` to render the
    existing ``EmptyState`` start screen.
  * The redundant sidebar footer ``ProcessPicker`` and the dead state it
    supported (``process``, ``createError``, the retry/timeout label, the
    ``useState`` import, the ``createTask`` import) are removed.
  * ``EmptyState`` remains the *sole* task-creation surface (regression guard —
    unchanged: it still calls ``createTask`` with a trimmed, non-empty message
    and renders its own ``ProcessPicker``).
  * ``app-layout.tsx`` still renders ``EmptyState`` whenever no task is selected
    (regression guard — the wiring the fix relies on).
  * The orchestrator's ``"Untitled"`` fallback is left intact (out of scope —
    backend is untouched; the bug is fixed by not calling that path from the UI).

There is no JS/TS test harness in this repo (no vitest/jest, no ``test`` npm
script, no ``*.test.*`` files), and introducing one would be a new top-level
dependency. The established idiom for frontend-source changes in this codebase
is a Python source-text acceptance test (see ``test_dead_code_removal.py``,
which scrubs the React SPA the same way). These tests follow that idiom: they
read the frontend source files as text and assert on their content.

Tests in the "fix" sections fail until the change is implemented. The
"regression" sections guard against over-deletion / collateral damage and pass
both before and after the change.
"""

from __future__ import annotations

import re
from pathlib import Path

import pytest

REPO_ROOT = Path(__file__).resolve().parents[2]

SIDEBAR = "lotsa/frontend/src/components/sidebar/sidebar.tsx"
EMPTY_STATE = "lotsa/frontend/src/components/empty-state.tsx"
APP_LAYOUT = "lotsa/frontend/src/components/layout/app-layout.tsx"
ORCHESTRATOR = "lotsa/orchestrator.py"


def _read(relative_path: str) -> str:
    return (REPO_ROOT / relative_path).read_text(encoding="utf-8")


# ---------------------------------------------------------------------------
# Sanity: the files under test exist where we expect them
# ---------------------------------------------------------------------------


@pytest.mark.parametrize("path", [SIDEBAR, EMPTY_STATE, APP_LAYOUT, ORCHESTRATOR])
def test_source_files_exist(path):
    """Regression: the source files this change touches/depends on are present."""
    assert (REPO_ROOT / path).is_file(), f"missing {path}"


# ---------------------------------------------------------------------------
# Fix — sidebar New Task becomes a pure selection reset (criteria 1, 2)
# ---------------------------------------------------------------------------


def test_sidebar_new_task_handler_clears_selection():
    """``handleNewTask`` deselects the current task via ``onSelectTask(null)``.

    Clearing the selection is what makes ``app-layout.tsx`` render the
    ``EmptyState`` start screen instead of a chat panel.
    """
    text = _read(SIDEBAR)
    assert re.search(r"onSelectTask\(\s*null\s*\)", text), "handleNewTask must call onSelectTask(null)"


def test_sidebar_does_not_call_create_task():
    """The sidebar no longer creates a task (no ``createTask`` reference at all).

    This is the core of the bug: the old ``handleNewTask`` called
    ``createTask({ process })`` with no message, producing an "Untitled" task
    and an empty agent dispatch. Removing every reference also covers the import.
    """
    assert "createTask" not in _read(SIDEBAR)


def test_sidebar_new_task_handler_is_not_async():
    """``handleNewTask`` is a synchronous reset — no ``async`` / no awaited API call."""
    text = _read(SIDEBAR)
    assert not re.search(r"handleNewTask\s*=\s*async", text), (
        "handleNewTask should be synchronous (pure selection reset)"
    )


# ---------------------------------------------------------------------------
# Fix — redundant footer ProcessPicker and dead state removed (criterion 5)
# ---------------------------------------------------------------------------


def test_sidebar_does_not_render_or_import_process_picker():
    """The redundant footer ``ProcessPicker`` (and its import) are gone.

    Process selection lives solely in ``EmptyState``'s own picker now.
    """
    assert "ProcessPicker" not in _read(SIDEBAR)


def test_sidebar_drops_dead_create_error_state():
    """The ``createError`` state and its retry/timeout machinery are removed."""
    text = _read(SIDEBAR)
    assert "createError" not in text
    assert "setCreateError" not in text


def test_sidebar_drops_dead_process_state():
    """The footer-picker ``process`` state setter is removed."""
    assert "setProcess" not in _read(SIDEBAR)


def test_sidebar_holds_no_new_task_creation_state():
    """The dead new-task creation state (``process``/``createError``) is gone.

    The sidebar previously held no local state at all, but ADR-029 added a
    project filter (``setProjectFilter``), so the blanket "no ``useState``"
    guard no longer holds. The thing this test really protects — that the
    sidebar is not a task-creation surface — is asserted directly: any
    ``useState`` present is the project filter, never new-task creation state.
    """
    text = _read(SIDEBAR)
    assert "setProcess" not in text
    assert "createError" not in text
    if "useState" in text:
        assert "setProjectFilter" in text, "the only sidebar local state is the ADR-029 project filter"


def test_sidebar_button_label_is_static_new_task():
    """The button shows a static "New Task" label — no conditional retry text."""
    text = _read(SIDEBAR)
    assert "New Task" in text
    # The old conditional label was 'Failed — retry?'. No retry/failure label
    # should survive once the create-and-fail path is removed.
    assert "retry" not in text.lower()


# ---------------------------------------------------------------------------
# Regression — sidebar is not over-trimmed (still a working New Task button +
# task list)
# ---------------------------------------------------------------------------


def test_sidebar_still_defines_new_task_handler():
    """``handleNewTask`` still exists and is still wired to the button."""
    text = _read(SIDEBAR)
    assert "handleNewTask" in text
    assert "onClick={handleNewTask}" in text


def test_sidebar_still_renders_task_list():
    """Regression: the sidebar still lists tasks (useTasks + TaskItem survive)."""
    text = _read(SIDEBAR)
    assert "useTasks" in text
    assert "TaskItem" in text


# ---------------------------------------------------------------------------
# Regression — EmptyState remains the sole task-creation surface (criterion 3)
# ---------------------------------------------------------------------------


def test_empty_state_still_creates_task():
    """Regression: ``EmptyState`` still calls ``createTask`` on submit."""
    assert "createTask" in _read(EMPTY_STATE)


def test_empty_state_still_renders_process_picker():
    """Regression: process selection still lives in ``EmptyState``."""
    assert "ProcessPicker" in _read(EMPTY_STATE)


def test_empty_state_requires_non_empty_message():
    """Regression: ``EmptyState`` only creates on a trimmed, non-empty message.

    This is what guarantees a dashboard-created task always has a real message
    (and therefore an auto-derived title — never the "Untitled" fallback).
    """
    assert "message.trim()" in _read(EMPTY_STATE)


# ---------------------------------------------------------------------------
# Regression — app-layout renders EmptyState when no task is selected
# (this is the wiring the fix relies on; criteria 1 & 2)
# ---------------------------------------------------------------------------


def test_app_layout_renders_empty_state_when_no_task_selected():
    """``app-layout.tsx`` renders ``EmptyState`` when ``selectedTaskId`` is falsy."""
    text = _read(APP_LAYOUT)
    assert "EmptyState" in text
    # The render branch keys off the selected-task id.
    assert re.search(r"selectedTaskId\s*\?", text), (
        "app-layout should branch on selectedTaskId to render EmptyState vs ChatPanel"
    )


# ---------------------------------------------------------------------------
# Out of scope — backend orchestrator is untouched (criterion: "Untitled"
# fallback left intact as a defensive default for title-less API callers)
# ---------------------------------------------------------------------------


def test_orchestrator_retains_untitled_fallback():
    """The orchestrator's defensive ``"Untitled"`` default is deliberately kept.

    The fix is frontend-only: the UI stops calling the empty-create path, so no
    dashboard task is ever titled "Untitled" — but the backend fallback stays
    for legitimate title-less API callers (spec: out of scope).
    """
    assert '"Untitled"' in _read(ORCHESTRATOR)
