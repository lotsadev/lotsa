"""The persisted call stack — ADR-045 Phase 1.

A task's active workflow is the **top frame** of a call stack persisted in
``task.metadata["call_stack"]``. This replaces the single ``current_flow``
string: a workflow may *call* another and wait for its return, so the active
workflow is a stack, not a slot (the common path is two deep —
``build → pr-monitor``).

Each frame is a **record**, not a string (ADR-045 constraint 1): persisting
``{workflow, step, called_from}`` as an object means a future ``vars`` key is a
pure addition with no schema change. ``vars`` is reserved-but-absent in v1.

These helpers are pure functions over the metadata dict — no orchestrator, no
DB — so the stack semantics (``complete`` pops one frame; ``terminate`` unwinds)
are unit-testable in isolation and the orchestrator's CAS writes stay the single
place the persisted value changes.
"""

from __future__ import annotations

from typing import Any

CALL_STACK_KEY = "call_stack"

# The metadata slot a task carried before the stack existed (ADR-014/021). A row
# with this key and no ``call_stack`` is a legacy pre-ADR-045 shape: the
# orchestrator routes it to ``blocked`` (no migration — decided in the spec).
LEGACY_FLOW_KEY = "current_flow"

Frame = dict[str, Any]


def make_frame(workflow: str, step: str, called_from: str | None = None) -> Frame:
    """Build a stack frame record.

    ``vars`` is deliberately absent — reserved for the deferred metadata layer
    (ADR-045 constraint 4) so its arrival is a pure addition.
    """
    return {"workflow": workflow, "step": step, "called_from": called_from}


def get_stack(metadata: dict[str, Any] | None) -> list[Frame]:
    """Return the call stack (possibly empty) from a task's metadata."""
    if not metadata:
        return []
    stack = metadata.get(CALL_STACK_KEY)
    return list(stack) if isinstance(stack, list) else []


def stack_top(metadata: dict[str, Any] | None) -> Frame | None:
    """Return the active (top) frame, or ``None`` when the stack is empty."""
    stack = get_stack(metadata)
    return stack[-1] if stack else None


def push_frame(metadata: dict[str, Any] | None, frame: Frame) -> dict[str, Any]:
    """Return a NEW metadata dict with *frame* pushed as the active frame.

    The input is not mutated — callers persist the returned dict through a CAS
    transition (ADR-020), so leaving the source untouched keeps the CAS-loser's
    view clean.
    """
    new_meta = dict(metadata or {})
    new_meta[CALL_STACK_KEY] = [*get_stack(metadata), dict(frame)]
    return new_meta


def pop_frame(metadata: dict[str, Any] | None) -> dict[str, Any]:
    """Return a NEW metadata dict with the top frame removed (``complete`` =
    return to caller). Popping the only frame leaves an empty stack."""
    new_meta = dict(metadata or {})
    new_meta[CALL_STACK_KEY] = get_stack(metadata)[:-1]
    return new_meta


def is_legacy_flow_row(metadata: dict[str, Any] | None) -> bool:
    """True for a pre-ADR-045 row: a legacy ``current_flow`` slot and no stack.

    Every surface that reads the active workflow gives such a row one defined
    answer — route to ``blocked`` with the standard recovery message. There is
    no migration onto a synthetic stack (spec decision).
    """
    if not metadata:
        return False
    return LEGACY_FLOW_KEY in metadata and not metadata.get(CALL_STACK_KEY)
