"""Read-only, PR-style git diff for a task's worktree.

The dashboard's Changes tab renders the full set of changes a task's
``lotsa/<task_id>`` branch introduces relative to the branch it was created
from — committed, staged, unstaged, *and* untracked/new files — as a single
unified-diff string.

The diff is computed PR-style using merge-base semantics so upstream commits
that land after the branch diverged don't pollute it. Base resolution is
strictly local (no ``git fetch``) because this path is polled by the UI; the
network-fetching ``WorktreeManager._resolve_default_base_ref`` is deliberately
not used here.

The path performs **zero git mutations** (ADR-013): no ``add``, no ``commit``,
no index writes. Untracked files are surfaced via ``git diff --no-index``
against ``/dev/null`` rather than ``git add -N``. Every git call uses
``asyncio.create_subprocess_exec`` with positional args — no shell strings
(Constitution §1.1, §2.1).
"""

from __future__ import annotations

import asyncio
import logging
from pathlib import Path

logger = logging.getLogger(__name__)


async def _run_git(work_dir: Path, *args: str) -> tuple[int | None, str]:
    """Run a read-only git command and return ``(returncode, stdout)``.

    Tolerant of non-zero exits — callers decide what a failure means. Never
    raises for an unhappy git (only an absent ``git`` binary would, which is
    caught by ``compute_branch_diff``'s guard).
    """
    proc = await asyncio.create_subprocess_exec(
        "git",
        *args,
        cwd=str(work_dir),
        stdout=asyncio.subprocess.PIPE,
        stderr=asyncio.subprocess.PIPE,
    )
    stdout, _ = await proc.communicate()
    return proc.returncode, stdout.decode("utf-8", errors="replace")


async def _resolve_base_ref(work_dir: Path, base_branch: str | None) -> str:
    """Resolve a git ref to diff the branch against.

    Prefers ``origin/<base_branch>`` (then the bare ``base_branch``) when a
    base is configured; otherwise asks git for the remote's default branch,
    falling back to ``origin/main`` then ``main``.  Best-effort and local-only
    (no fetch) — the result is consumed by ``compute_branch_diff`` (and by
    ``push_step``'s fallback PR text), both of which tolerate an empty diff.
    """

    async def _ref_exists(ref: str) -> bool:
        proc = await asyncio.create_subprocess_exec(
            "git",
            "rev-parse",
            "--verify",
            "--quiet",
            f"{ref}^{{commit}}",
            cwd=work_dir,
            stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.PIPE,
        )
        await proc.communicate()
        return proc.returncode == 0

    if base_branch:
        for candidate in (f"origin/{base_branch}", base_branch):
            if await _ref_exists(candidate):
                return candidate
        return base_branch

    proc = await asyncio.create_subprocess_exec(
        "git",
        "rev-parse",
        "--abbrev-ref",
        "origin/HEAD",
        cwd=work_dir,
        stdout=asyncio.subprocess.PIPE,
        stderr=asyncio.subprocess.PIPE,
    )
    stdout, _ = await proc.communicate()
    if proc.returncode == 0 and stdout.decode().strip():
        return stdout.decode().strip()
    return "origin/main" if await _ref_exists("origin/main") else "main"


async def _merge_base(work_dir: Path, base_ref: str) -> str | None:
    """Return ``merge-base(base_ref, HEAD)`` or ``None`` when none exists."""
    rc, out = await _run_git(work_dir, "merge-base", base_ref, "HEAD")
    if rc == 0 and out.strip():
        return out.strip()
    return None


async def _tracked_diff(work_dir: Path, base_branch: str | None) -> str:
    """Unified diff of committed + staged + unstaged tracked changes.

    ``git diff <ref>`` (single ref, no ``..``) compares ``<ref>`` to the
    working tree, so a merge-base ref captures committed-since-divergence
    changes alongside uncommitted ones in one pass. Falls back through the
    resolved base ref, then ``HEAD``, then the plain working-tree diff so a
    repo with no resolvable base (no remote, detached, deleted base branch)
    still produces a diff rather than erroring.

    ``--src-prefix=a/ --dst-prefix=b/`` pins the standard header regardless
    of operator gitconfig — ``diff.mnemonicPrefix`` emits ``c/``/``w/``
    (commit/worktree) prefixes that the frontend patch parser rejects
    ("invalid git diff header"), which silently dropped every file header
    in the Changes tab. The untracked-file helper below already pins the
    prefixes for the same reason.
    """
    base_ref = await _resolve_base_ref(work_dir, base_branch)
    mb = await _merge_base(work_dir, base_ref)

    for ref in (mb, base_ref, "HEAD"):
        if ref is None:
            continue
        rc, out = await _run_git(work_dir, "diff", "--no-color", "--src-prefix=a/", "--dst-prefix=b/", ref)
        if rc == 0:
            return out

    # Last resort — working-tree diff against the index/HEAD with no explicit
    # ref. Never errors even on a fresh repo with no commits.
    _, out = await _run_git(work_dir, "diff", "--no-color", "--src-prefix=a/", "--dst-prefix=b/")
    return out


async def _untracked_diff(work_dir: Path) -> str:
    """New-file patches for untracked files, concatenated.

    ``git diff`` omits untracked files, so we list them
    (``--exclude-standard`` keeps ``.gitignore``d files hidden) and render each
    as a new-file addition via ``--no-index`` against ``/dev/null``. This stays
    read-only — no ``git add -N``, no temp index. ``--no-index`` exits non-zero
    when the files differ (always true for a new file), so the return code is
    ignored and only stdout is used.

    ``--src-prefix=a/ --dst-prefix=b/`` forces the standard git header
    (``diff --git a/… b/…``, ``+++ b/…``). Without it ``--no-index`` emits
    ``1/``/``2/`` prefixes, which the frontend patch parser (``@pierre/diffs``)
    doesn't recognise as a git path — the file would render under an ugly
    ``2/<name>`` header instead of its real name.
    """
    rc, out = await _run_git(work_dir, "ls-files", "--others", "--exclude-standard")
    if rc != 0 or not out.strip():
        return ""

    patches: list[str] = []
    for line in out.splitlines():
        path = line.strip()
        if not path:
            continue
        _, patch = await _run_git(
            work_dir,
            "diff",
            "--no-index",
            "--no-color",
            "--src-prefix=a/",
            "--dst-prefix=b/",
            "--",
            "/dev/null",
            path,
        )
        if patch:
            patches.append(patch)
    return "".join(patches)


async def compute_branch_diff(work_dir: Path, base_branch: str | None) -> str:
    """Return the task worktree's PR-style unified diff (empty when clean).

    Concatenates the tracked-changes diff (committed + staged + unstaged,
    merge-base relative to *base_branch*) with new-file patches for untracked
    files. Read-only and best-effort: any failure yields an empty string rather
    than an exception, so the diff endpoint never errors.
    """
    work_dir = Path(work_dir)
    # Worktrees carry a ``.git`` file; plain repos a ``.git`` directory. Either
    # way, its absence means there's nothing to diff (no worktree yet, or a
    # path that isn't a repo).
    if not (work_dir / ".git").exists():
        return ""

    try:
        tracked = await _tracked_diff(work_dir, base_branch)
        untracked = await _untracked_diff(work_dir)
        return tracked + untracked
    except Exception:
        logger.exception("Failed to compute branch diff for %s", work_dir)
        return ""
