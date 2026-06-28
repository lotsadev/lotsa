"""Shared reader for Claude Code session activity (ADR-017).

Claude Code persists every event — tool use, tool result, thinking block,
assistant text — incrementally to a per-session JSONL file under
``~/.claude/projects/<encoded-cwd>/<session-id>.jsonl``, even under
``--print --output-format json``. This module is the single source of truth
for projecting those JSONL records into :class:`~rigg.models.ActivityEvent`
objects, applying the truncation policy, and supporting incremental polling.

It is consumed by ``ClaudeCodeRunner`` (and, when a host ``~/.claude`` mount
lands, ``DockerAgentRunner``), the dashboard's ``agent-activity`` endpoint, and
the ``lotsa inspect`` CLI — so the JSONL→event mapping lives in exactly one
place.

The session file's location and schema are an internal contract of the Claude
Code CLI, not a public API. The reader is defensive: a single malformed record
never raises, unknown top-level record types are skipped, and a missing file
degrades to an empty (but ``supported``) result. The breakage mode is "the
Activity tab is empty", never corruption.
"""

from __future__ import annotations

import asyncio
import json
import logging
from datetime import datetime
from pathlib import Path
from typing import Any, Literal

from rigg.models import ActivityEvent, ActivityResult
from rigg.scrub import scrub_secrets

logger = logging.getLogger(__name__)

# Mirrors ``ActivityEvent.kind`` (kept local so the projection helpers below can
# return the narrowed Literal that ``ActivityEvent`` expects).
ActivityKind = Literal["thinking", "tool_use", "tool_result", "text", "system"]

# Truncation caps (ADR-017 §2). Full content stays in the on-disk JSONL; the
# API response carries only enough to triage.
_TOOL_INPUT_CAP = 200
_TOOL_RESULT_CAP = 500
_TEXT_CAP = 1000
# Summary caps — one-line, scannable labels.
_BASH_SUMMARY_CAP = 80


def encode_cwd(work_dir: Path) -> str:
    """Encode *work_dir* the way Claude Code names its project directory.

    Claude Code replaces every ``/`` and ``.`` in the absolute working
    directory with ``-`` (so ``/Users/x/.lotsa/wt`` becomes
    ``-Users-x--lotsa-wt`` — note the ``--`` from ``/.``). Verified against a
    live ``~/.claude/projects`` tree; a ``/``-only rule is wrong for any path
    under a dotted directory such as ``~/.lotsa``.
    """
    s = str(work_dir.resolve())
    return "".join("-" if c in "/." else c for c in s)


def _projects_root() -> Path:
    return Path.home() / ".claude" / "projects"


def session_jsonl_path(work_dir: Path, session_id: str, projects_root: Path | None = None) -> Path | None:
    """Resolve the session JSONL for *session_id*, or ``None`` if absent.

    Primary lookup uses :func:`encode_cwd`. As a guard against Claude Code
    changing its encoding scheme out from under us, a fallback globs every
    project directory for ``<session_id>.jsonl`` — session ids are globally
    unique UUIDs, so the match is unambiguous.

    ``projects_root`` overrides the default ``~/.claude/projects`` — the Docker
    runner passes the per-task mounted home, where the container's cwd encoding
    differs, so the glob fallback (not the primary) does the resolving there.
    """
    root = projects_root or _projects_root()
    primary = root / encode_cwd(work_dir) / f"{session_id}.jsonl"
    if primary.exists():
        return primary
    matches = sorted(root.glob(f"*/{session_id}.jsonl"))
    return matches[0] if matches else None


def _truncate(text: str, cap: int) -> tuple[str, bool]:
    """Return *text* capped to *cap* chars and whether it was truncated."""
    if len(text) <= cap:
        return text, False
    return text[:cap], True


def _parse_timestamp(raw: Any) -> datetime | None:
    if not isinstance(raw, str) or not raw:
        return None
    try:
        # Claude Code writes RFC-3339 with a trailing ``Z``; ``fromisoformat``
        # handles the offset form, so normalise ``Z`` → ``+00:00``.
        return datetime.fromisoformat(raw.replace("Z", "+00:00"))
    except ValueError:
        return None


def _tool_use_summary(name: str, tool_input: dict[str, Any]) -> str:
    """One-line label for a ``tool_use`` block (ADR-017 §3 table)."""
    if name == "Bash":
        command = str(tool_input.get("command", ""))
        head, _ = _truncate(command, _BASH_SUMMARY_CAP)
        return f"Bash: {head}"
    if name == "Read":
        return f"Read: {tool_input.get('file_path', '')}"
    # Best-effort: the first scalar input value.
    for value in tool_input.values():
        if isinstance(value, (str, int, float, bool)):
            head, _ = _truncate(str(value), _BASH_SUMMARY_CAP)
            return f"{name}: {head}"
    return name


def _first_line(text: str) -> str:
    return text.strip().split("\n", 1)[0].strip()


def _blocks_to_events(record: dict[str, Any]) -> list[tuple[ActivityKind, str, dict[str, Any] | None]]:
    """Project one assistant/user record's content blocks to (kind, summary, detail).

    The per-event timestamp is applied by the caller from the record envelope,
    so the blocks themselves only carry kind/summary/detail.
    """
    message = record.get("message") or {}
    content = message.get("content")
    if not isinstance(content, list):
        return []

    # §1.2: tool input/result and assistant text can echo a credential the agent
    # holds in its env — scrub every operator-facing string before it leaves here.
    out: list[tuple[ActivityKind, str, dict[str, Any] | None]] = []
    for block in content:
        if not isinstance(block, dict):
            continue
        btype = block.get("type")
        if btype == "thinking":
            text = str(block.get("thinking", ""))
            head, truncated = _truncate(text, _TEXT_CAP)
            out.append(
                ("thinking", scrub_secrets(_first_line(text)), {"text": scrub_secrets(head), "truncated": truncated})
            )
        elif btype == "text":
            text = str(block.get("text", ""))
            head, truncated = _truncate(text, _TEXT_CAP)
            out.append(
                ("text", scrub_secrets(_first_line(text)), {"text": scrub_secrets(head), "truncated": truncated})
            )
        elif btype == "tool_use":
            name = str(block.get("name", "")).strip() or "tool"
            raw_input = block.get("input")
            tool_input: dict[str, Any] = raw_input if isinstance(raw_input, dict) else {}
            rendered = json.dumps(tool_input, ensure_ascii=False)
            head, truncated = _truncate(rendered, _TOOL_INPUT_CAP)
            out.append(
                (
                    "tool_use",
                    scrub_secrets(_tool_use_summary(name, tool_input)),
                    {"name": name, "input": scrub_secrets(head), "truncated": truncated},
                )
            )
        elif btype == "tool_result":
            is_error = bool(block.get("is_error"))
            rendered = str(block.get("content", ""))
            head, truncated = _truncate(rendered, _TOOL_RESULT_CAP)
            summary = "← error" if is_error else "← ok"
            out.append(
                ("tool_result", summary, {"ok": not is_error, "content": scrub_secrets(head), "truncated": truncated})
            )
    return out


def _record_to_events(record: dict[str, Any]) -> list[tuple[ActivityKind, str, dict[str, Any] | None]]:
    """Project a single JSONL record into zero or more (kind, summary, detail) tuples.

    Unknown top-level record types (``queue-operation``, ``attachment``,
    ``ai-title``, ``last-prompt``, …) yield nothing — they carry no
    operator-facing activity.
    """
    rtype = record.get("type")
    if rtype in ("assistant", "user"):
        return _blocks_to_events(record)
    if rtype == "summary":
        return [("system", str(record.get("summary", "")), None)]
    return []


def _read_activity_sync(
    session_id: str, work_dir: Path, since_index: int, limit: int, projects_root: Path | None = None
) -> ActivityResult:
    # Clamp the cursor's lower bound at the single source of truth, so every
    # caller (the API route, the ``lotsa inspect`` CLI, the orchestrator) is
    # covered, not just whichever one remembered to guard. A negative
    # ``since_index`` makes the ``index >= since_index`` filter below always
    # true — silently returning the whole session from index 0 — and would let
    # a negative value leak back out through the early-return ``next_index``
    # paths. Floor it here next to the ``limit`` clamp.
    since_index = max(since_index, 0)
    path = session_jsonl_path(work_dir, session_id, projects_root)
    if path is None:
        # Runner supports activity; the session file just doesn't exist yet
        # (agent not dispatched, or JSONL deleted out-of-band).
        return ActivityResult(events=[], supported=True, session_complete=False, next_index=since_index)

    try:
        raw = path.read_text(errors="ignore")
    except OSError:
        logger.debug("Could not read session JSONL at %s", path, exc_info=True)
        return ActivityResult(events=[], supported=True, session_complete=False, next_index=since_index)

    # Indices are assigned by counting every emitted event from the start of the
    # file, so the file is always parsed from byte 0 — but we keep only events at
    # or after ``since_index`` and stop once ``limit`` of them are in hand. A
    # caught-up poller's slice is smaller than ``limit``, so it never short-
    # circuits and still observes the trailing ``summary`` record; only a large
    # backlog early-exits, which is exactly the O(total_events) read we want to
    # avoid on a 2s poll. (A byte-offset seek keyed on ``since_index`` would drop
    # the from-zero parse entirely; deferred as a follow-up optimization.)
    # Clamp the lower bound: a caller passing ``limit <= 0`` would otherwise hit
    # ``selected[:0] == []`` below — every event silently discarded and
    # ``next_index`` frozen at ``since_index``, wedging incremental polling. The
    # API route caps the upper bound (``min(limit, 500)``) but not the lower.
    limit = max(limit, 1)

    selected: list[ActivityEvent] = []
    session_complete = False
    index = 0
    # Carry-forward the most recent parseable timestamp. Some records carry no
    # timestamp of their own — notably the trailing ``summary`` record — and a
    # few may have a garbled one. Stamping those with ``datetime.min`` surfaces
    # as "year 1" / "~2025 years ago" once serialised to the API and rendered
    # by the dashboard's relative-time formatter. Inheriting the prior event's
    # time keeps the timeline monotonic and the contract non-nullable. The file
    # is always parsed from byte 0, so this accumulates correctly on every poll.
    last_timestamp = datetime.min
    for line in raw.splitlines():
        line = line.strip()
        if not line:
            continue
        try:
            record = json.loads(line)
        except (json.JSONDecodeError, TypeError):
            # A single malformed record must never abort the read.
            continue
        if not isinstance(record, dict):
            continue
        if record.get("type") == "summary":
            session_complete = True
        parsed = _parse_timestamp(record.get("timestamp"))
        if parsed is not None:
            last_timestamp = parsed
        timestamp = last_timestamp
        for kind, summary, detail in _record_to_events(record):
            if index >= since_index:
                selected.append(
                    ActivityEvent(index=index, timestamp=timestamp, kind=kind, summary=summary, detail=detail)
                )
            index += 1
        if len(selected) >= limit:
            break

    # The break fires only *after* a record's blocks are all appended, so a
    # multi-block record in the final iteration can push ``selected`` past
    # ``limit``. Trim the overshoot; ``next_index`` below still points at the
    # first trimmed event, so the next poll recovers it — nothing is lost.
    selected = selected[:limit]
    next_index = selected[-1].index + 1 if selected else since_index
    return ActivityResult(
        events=selected,
        supported=True,
        session_complete=session_complete,
        next_index=next_index,
    )


async def read_activity(
    session_id: str,
    work_dir: Path,
    since_index: int = 0,
    limit: int = 200,
    projects_root: Path | None = None,
) -> ActivityResult:
    """Read recent activity events for *session_id* (read-only, never raises).

    The file read is offloaded to a thread (consistent with how
    ``ClaudeCodeRunner.run`` offloads ``subprocess.run``) so it stays off the
    event loop. Safe against an in-flight session — the JSONL is append-only.

    ``projects_root`` overrides the default ``~/.claude/projects`` (the Docker
    runner passes its per-task mounted home).
    """
    loop = asyncio.get_running_loop()
    try:
        return await loop.run_in_executor(
            None, _read_activity_sync, session_id, work_dir, since_index, limit, projects_root
        )
    except Exception:  # pragma: no cover - defensive; _read_activity_sync swallows its own
        logger.debug("read_activity failed for session %s", session_id, exc_info=True)
        return ActivityResult(events=[], supported=True, session_complete=False, next_index=since_index)
