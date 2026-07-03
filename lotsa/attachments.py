"""Prompt file attachments — durable on-disk storage + worktree materialization.

Path A of the prompt-attachments feature: operator-uploaded files are stored on
disk (never as SQLite blobs, never in the append-only message log) and their
*paths* — not their bytes — are injected into the agent prompt at dispatch. The
agent opens them with its built-in ``Read`` tool.

Storage layout::

    {data_dir}/attachments/{project_id}/{task_id}/<sanitized-name>

This sits parallel to ``worktrees/`` and survives worktree pruning. At each
dispatch the orchestrator copies these files into
``{work_dir}/.lotsa/attachments/`` and git-excludes them (a managed
``.gitignore`` of ``*``) so operator files never enter the PR branch.

This module holds only pure filesystem/sanitization helpers — no SQLite, no
FastAPI. The DB metadata append lives in :meth:`lotsa.db.TaskDB.append_attachment`;
the HTTP endpoint and the dispatch-time injection compose these helpers.
"""

from __future__ import annotations

import logging
import shutil
from datetime import UTC, datetime
from pathlib import Path, PurePosixPath

logger = logging.getLogger(__name__)

# Per-file and per-task caps (spec §5). Enforced at the API boundary; the count
# cap is additionally enforced race-safely in the DB append (WHERE clause).
MAX_FILE_BYTES = 25 * 1024 * 1024  # 25 MB per file
MAX_FILES_PER_TASK = 10  # across a task's lifetime

# Worktree-relative directory the files are materialized into. The leading
# ``.lotsa`` is git-excluded via a managed ``.gitignore`` (see
# ``materialize_into_worktree``), so nothing here reaches a commit.
_ATTACH_SUBDIR = PurePosixPath(".lotsa/attachments")


def _now_iso() -> str:
    return datetime.now(UTC).isoformat()


def sanitize_filename(raw: str) -> str:
    """Reduce an untrusted upload name to a safe basename.

    Strips any directory components (both ``/`` and ``\\`` separators), which
    defuses ``../`` traversal and absolute paths, and rejects a name that has
    nothing usable left. Null bytes are rejected outright.

    Raises:
        ValueError: on a null byte, or when the sanitized result is empty / a
            bare ``.`` / ``..``.
    """
    if "\x00" in raw:
        raise ValueError("Filename contains a null byte")
    # Normalize backslashes to forward slashes so a Windows-style path
    # (``a\\b\\c.png``) collapses to its basename too, then take the last
    # component. PurePosixPath(...).name yields "" for a trailing slash and
    # for "." / "..".
    name = PurePosixPath(raw.replace("\\", "/")).name
    if not name or name in (".", ".."):
        raise ValueError(f"Filename is empty or invalid after sanitization: {raw!r}")
    return name


def attachments_root(data_dir: Path, project_id: str, task_id: str) -> Path:
    """Return the durable on-disk directory for a task's attachments.

    ``project_id`` is slug-validated at config load (``[a-z0-9_-]{1,64}``) and
    ``task_id`` is a hex-8 from ``create_task``; both are safe path segments.
    """
    return Path(data_dir) / "attachments" / project_id / task_id


def _dedupe_name(name: str, existing: set[str]) -> str:
    """Return ``name`` or a ``bug (1).png``-style suffixed variant if it
    collides with a name in ``existing``."""
    if name not in existing:
        return name
    stem = PurePosixPath(name).stem
    suffix = PurePosixPath(name).suffix  # includes the leading dot, or ""
    n = 1
    while True:
        candidate = f"{stem} ({n}){suffix}"
        if candidate not in existing:
            return candidate
        n += 1


def write_attachment(
    data_dir: Path,
    project_id: str,
    task_id: str,
    raw_filename: str,
    data: bytes,
    existing_names: set[str],
    mime: str | None,
) -> dict:
    """Sanitize + collision-suffix a name, write the bytes, and return the
    metadata record.

    The record shape (stored as a JSON entry under ``tasks.metadata``)::

        {filename, rel_path, mime, size_bytes, created_at}

    ``existing_names`` is the set of filenames already recorded for the task —
    a collision is suffixed rather than overwriting the earlier file. Size and
    count caps are enforced by the caller; this helper only writes.

    Raises:
        ValueError: if the filename cannot be sanitized to a usable basename.
    """
    safe = sanitize_filename(raw_filename)
    root = attachments_root(data_dir, project_id, task_id)
    root.mkdir(parents=True, exist_ok=True)

    # Dedupe against both the caller's snapshot *and* whatever is already on
    # disk, then write with exclusive-create (``"xb"``) so two concurrent
    # uploads of the same original name to the same task can't compute the same
    # deduped name and silently overwrite each other's bytes (the snapshot is
    # read before the write, so it can't see a racing sibling). If the exclusive
    # create loses the race, add the now-present name to the seen set and try the
    # next suffix.
    seen = set(existing_names)
    while True:
        final = _dedupe_name(safe, seen)
        try:
            with open(root / final, "xb") as fh:
                fh.write(data)
            break
        except FileExistsError:
            seen.add(final)

    return {
        "filename": final,
        "rel_path": str(_ATTACH_SUBDIR / final),
        "mime": mime or "application/octet-stream",
        "size_bytes": len(data),
        "created_at": _now_iso(),
    }


def remove_attachment_file(data_dir: Path, project_id: str, task_id: str, filename: str) -> None:
    """Delete one durable attachment file. No error if it's already gone.

    Used to undo a write whose metadata append lost the count-cap race, so the
    orphaned bytes don't linger on disk.
    """
    path = attachments_root(data_dir, project_id, task_id) / filename
    with_missing_ok = True
    try:
        path.unlink(missing_ok=with_missing_ok)
    except OSError as exc:  # pragma: no cover - defensive
        logger.warning("Failed to remove orphaned attachment %s: %s", path, exc)


def materialize_into_worktree(
    records: list[dict],
    data_dir: Path,
    project_id: str,
    task_id: str,
    work_dir: Path,
) -> list[str]:
    """Copy a task's durable attachments into ``{work_dir}/.lotsa/attachments/``.

    Idempotent — safe to run on every dispatch: a file is (re)copied only when
    the destination is missing or its size differs. Writes a managed
    ``.lotsa/.gitignore`` of ``*`` so ``git add -A`` (the ADR-024 commit
    posthook) never stages the operator's files. Self-contained inside the
    worktree — it does not touch the shared ``.git`` common dir.

    Returns the worktree-relative paths that are present on disk after the copy
    (a record whose durable source has vanished is skipped, not fatal).
    """
    if not records:
        return []

    lotsa_dir = Path(work_dir) / ".lotsa"
    attach_dir = lotsa_dir / "attachments"
    attach_dir.mkdir(parents=True, exist_ok=True)

    gitignore = lotsa_dir / ".gitignore"
    if not gitignore.exists():
        # ``*`` ignores everything under .lotsa/ (including this file itself),
        # so nothing here is ever committed.
        gitignore.write_text("*\n")

    root = attachments_root(data_dir, project_id, task_id)
    present: list[str] = []
    for record in records:
        filename = record["filename"]
        src = root / filename
        if not src.exists():
            logger.warning("Attachment source missing, skipping: %s", src)
            continue
        dest = attach_dir / filename
        if not dest.exists() or dest.stat().st_size != src.stat().st_size:
            shutil.copy2(src, dest)
        present.append(record["rel_path"])
    return present
