"""Deterministic commit step — stage the worktree and create one commit.

ADR-024: commit joins push as an orchestrator-owned, mechanical step. This
module is the reusable, unit-testable core that the ``commit`` posthook
(``lotsa.posthooks``) wraps; it is intentionally free of agent invocations
and of any orchestrator/DB coupling.

It mirrors ``lotsa.push_step`` discipline exactly: every git command goes
through ``asyncio.create_subprocess_exec`` with arguments as separate
positional tokens — never the blocking subprocess API, never a shell string,
never an f-string interpolated into a command line (Constitution §1.1, §2.1).
"""

from __future__ import annotations

import asyncio
import fnmatch
from dataclasses import dataclass
from pathlib import Path

# Belt-and-braces deny-list: basenames (matched with fnmatch) that the
# orchestrator never wants in a commit even if an agent staged them. Anything
# already covered by ``.gitignore`` is excluded by git before we ever see it;
# this list catches the accidental-emit cases (secrets/keys) that are NOT
# typically gitignored in a fresh worktree.
_DENY_PATTERNS: tuple[str, ...] = (
    ".env",
    ".env.*",
    "*.pem",
    "*.key",
    "id_rsa",
    "id_rsa.*",
    "id_dsa",
    "id_ecdsa",
    "id_ed25519",
)

# Keep commit subjects to a sane single-line length. Mirrors push_step's
# 72-char PR-title convention so the audit trail reads consistently.
_SUBJECT_MAX = 72


class CommitError(Exception):
    """Raised when the commit step cannot proceed.

    The message is safe to surface to the user and is intended to be used as
    the task's block reason.
    """


@dataclass
class CommitResult:
    """Outcome of a commit attempt.

    ``committed`` is ``False`` for the legitimate clean-worktree no-op (after
    a gate step or a pure-read agent), in which case ``sha`` / ``message`` are
    ``None``. When ``committed`` is ``True``, ``sha`` is the new HEAD SHA and
    ``message`` is the deterministic subject that was used.
    """

    committed: bool
    sha: str | None = None
    message: str | None = None


# ---------------------------------------------------------------------------
# Low-level git helpers
# ---------------------------------------------------------------------------


async def _run_git(work_dir: Path, *args: str) -> tuple[int, str, str]:
    """Run ``git <args>`` in *work_dir*; return ``(returncode, stdout, stderr)``."""
    try:
        proc = await asyncio.create_subprocess_exec(
            "git",
            *args,
            cwd=work_dir,
            stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.PIPE,
        )
    except (FileNotFoundError, OSError) as exc:
        raise CommitError(f"Could not run git {args[0] if args else ''}: {exc}") from exc
    stdout, stderr = await proc.communicate()
    assert proc.returncode is not None  # set after communicate()
    return proc.returncode, stdout.decode(errors="replace"), stderr.decode(errors="replace")


async def _git_or_raise(work_dir: Path, *args: str) -> str:
    """Run a git command and return stdout, raising ``CommitError`` on failure."""
    rc, out, err = await _run_git(work_dir, *args)
    if rc != 0:
        detail = err.strip() or out.strip() or f"git {args[0] if args else ''} failed"
        raise CommitError(f"git {args[0] if args else ''} failed: {detail}")
    return out


def _build_subject(task_title: str, step_name: str, commit_prefix: str, task_id: str) -> str:
    """Build the deterministic ``<prefix>: <title> (<step>)`` commit subject.

    Falls back to ``task_id`` when the title is empty so the subject never
    degenerates to ``<prefix>:  (<step>)``. Truncated at a word boundary to
    keep the audit trail tidy (mirrors push_step's title rule).
    """
    title = (task_title or "").strip() or task_id
    subject = f"{commit_prefix}: {title} ({step_name})"
    if len(subject) <= _SUBJECT_MAX:
        return subject
    cut = subject[:_SUBJECT_MAX].rsplit(" ", 1)[0]
    return cut or subject[:_SUBJECT_MAX]


async def _staged_paths(work_dir: Path) -> list[str]:
    """Return the worktree-relative paths currently staged in the index."""
    out = await _git_or_raise(work_dir, "diff", "--cached", "--name-only", "-z")
    return [p for p in out.split("\0") if p]


def _is_denied(path: str) -> bool:
    """True if *path*'s basename matches any deny-list pattern."""
    name = Path(path).name
    return any(fnmatch.fnmatch(name, pat) for pat in _DENY_PATTERNS)


# ---------------------------------------------------------------------------
# Public entry point
# ---------------------------------------------------------------------------


async def execute_commit(
    work_dir: Path,
    task_id: str,
    task_title: str,
    step_name: str,
    commit_prefix: str = "chore",
) -> CommitResult:
    """Stage all changes (minus a deny-list) and create one deterministic commit.

    Sequence:
      1. ``git add -A`` — stage tracked changes and new files.
      2. Unstage any deny-listed paths (``.env*``, key material) so they never
         enter the commit. The files stay on disk (exclusion, not deletion).
      3. If nothing is staged after that, return a no-op success.
      4. ``git commit -m "<prefix>: <title> (<step>)"``.
      5. Return the new HEAD SHA.

    Raises:
        CommitError: On any git failure (message safe as a block reason).
    """
    await _git_or_raise(work_dir, "add", "-A")

    # Exclude deny-listed paths from the commit (non-destructive unstage).
    for path in await _staged_paths(work_dir):
        if _is_denied(path):
            # ``--`` guards against a path that looks like an option.
            await _git_or_raise(work_dir, "reset", "-q", "--", path)

    # No-op when nothing remains staged (clean worktree, gate step, or a
    # worktree whose only changes were all deny-listed).
    rc, _out, _err = await _run_git(work_dir, "diff", "--cached", "--quiet")
    if rc == 0:
        return CommitResult(committed=False, sha=None, message=None)

    subject = _build_subject(task_title, step_name, commit_prefix, task_id)
    await _git_or_raise(work_dir, "commit", "-m", subject)

    sha = (await _git_or_raise(work_dir, "rev-parse", "HEAD")).strip()
    if not sha:
        raise CommitError("git rev-parse HEAD returned empty output after commit")
    return CommitResult(committed=True, sha=sha, message=subject)
