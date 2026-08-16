"""Deterministic push step — git push and GitHub PR creation.

This module is intentionally free of agent invocations.  It handles
the mechanical work of pushing a branch and opening (or updating) a
pull request using the GitHub REST API via :mod:`lotsa.github_client`.

All git commands use ``asyncio.create_subprocess_exec`` — never
``subprocess.run`` and never ``shell=True``.
"""

from __future__ import annotations

import asyncio
import logging
import os
import re
import tempfile
from pathlib import Path

# ``_resolve_base_ref`` is the canonical local (no-fetch) base-ref resolver,
# now shared with the diff path (``lotsa/diff.py``). Imported here so existing
# call sites — and the tests that patch ``lotsa.push_step._resolve_base_ref`` —
# keep working unchanged.
from lotsa.diff import _resolve_base_ref
from lotsa.github_client import GitHubClient, parse_github_remote

logger = logging.getLogger(__name__)

# ---------------------------------------------------------------------------
# Exceptions
# ---------------------------------------------------------------------------


class PushError(Exception):
    """Raised when the push step cannot proceed.

    The message is safe to surface to the user and should be used as the
    task's block reason.
    """


class ReconcileConflict(Exception):
    """Integrating the advanced remote branch into the worktree hit real conflicts.

    Signals that automatic reconciliation can't proceed. ``conflicting_files``
    lists the unmerged paths, and the conflict is left in the worktree as merge
    markers (identical shape to ``_sync_branch_to_main``'s origin/main path) so
    the caller can dispatch the ``resolve_conflicts`` agent to edit them. The
    message is token-scrubbed and safe to surface.
    """

    def __init__(self, message: str, conflicting_files: tuple[str, ...] = ()) -> None:
        super().__init__(message)
        self.conflicting_files = conflicting_files


# ---------------------------------------------------------------------------
# Low-level git helpers
# ---------------------------------------------------------------------------


async def _get_remote_url(work_dir: Path) -> str:
    """Return the URL of the ``origin`` remote for the repository at *work_dir*.

    Raises:
        PushError: If the command fails or the remote is not configured.
    """
    proc = await asyncio.create_subprocess_exec(
        "git",
        "remote",
        "get-url",
        "origin",
        cwd=work_dir,
        stdout=asyncio.subprocess.PIPE,
        stderr=asyncio.subprocess.PIPE,
    )
    stdout, stderr = await proc.communicate()
    if proc.returncode != 0:
        msg = stderr.decode().strip() or "git remote get-url origin failed"
        raise PushError(f"Could not read remote URL: {msg}")
    return stdout.decode().strip()


async def _has_uncommitted_changes(work_dir: Path) -> bool:
    """Return ``True`` if the working tree has any staged or unstaged changes."""
    proc = await asyncio.create_subprocess_exec(
        "git",
        "status",
        "--porcelain",
        cwd=work_dir,
        stdout=asyncio.subprocess.PIPE,
        stderr=asyncio.subprocess.PIPE,
    )
    stdout, _ = await proc.communicate()
    return bool(stdout.decode().strip())


async def _get_head_sha(work_dir: Path) -> str:
    """Resolve the worktree's current ``HEAD`` to a full commit SHA.

    The push step needs the SHA (not a branch name) so it can push the
    agent's actual commit to the canonical remote ref regardless of which
    local branch the agent left the worktree on.
    """
    proc = await asyncio.create_subprocess_exec(
        "git",
        "rev-parse",
        "HEAD",
        cwd=work_dir,
        stdout=asyncio.subprocess.PIPE,
        stderr=asyncio.subprocess.PIPE,
    )
    stdout, stderr = await proc.communicate()
    if proc.returncode != 0:
        err = stderr.decode().strip()
        raise PushError(f"git rev-parse HEAD failed: {err}")
    sha = stdout.decode().strip()
    if not sha:
        raise PushError("git rev-parse HEAD returned empty output")
    return sha


async def _normalize_worktree_branch(work_dir: Path, branch: str, head_sha: str) -> None:
    """Point the local ``branch`` ref at ``head_sha`` and check it out.

    After a successful push the worktree may still be on whatever local
    branch the agent created (e.g. ``feature/...``).  Force the local
    ``lotsa/<task_id>`` ref to track the pushed SHA and switch the
    worktree onto it so the next dispatch round starts on the
    orchestrator's expected branch rather than compounding the drift.

    Uses ``git checkout -B <branch> <head_sha>`` which atomically creates
    or resets the branch and checks it out in a single command.  This
    works equally well in the side-branch case (agent committed on
    ``feature/...``, ``lotsa/<task_id>`` must be moved and re-checked-out)
    and the happy-path case (agent committed directly on
    ``lotsa/<task_id>``, which is the current branch — a separate
    ``git branch -f`` would fail with "Cannot force update the current
    branch", but ``checkout -B`` handles it as a no-op).

    Failures here are logged but non-fatal — the remote is already in
    the right state, which is the load-bearing guarantee.
    """
    proc = await asyncio.create_subprocess_exec(
        "git",
        "checkout",
        "-B",
        branch,
        head_sha,
        cwd=work_dir,
        stdout=asyncio.subprocess.PIPE,
        stderr=asyncio.subprocess.PIPE,
    )
    _, stderr = await proc.communicate()
    if proc.returncode != 0:
        logger.warning(
            "Could not normalize worktree to %s @ %s after push: %s",
            branch,
            head_sha,
            stderr.decode().strip(),
        )


# ---------------------------------------------------------------------------
# Precondition validation
# ---------------------------------------------------------------------------


async def check_preconditions(work_dir: Path) -> tuple[str, str, str]:
    """Validate that a push can proceed and return the required credentials.

    Checks performed in order:
    1. ``GITHUB_TOKEN`` environment variable is set.
    2. The ``origin`` remote uses HTTPS (not SSH).
    3. The working tree is clean — defence-in-depth only (ADR-024): a dirty
       tree logs a warning and proceeds rather than raising, since the commit
       posthook should have run first and the push is by HEAD SHA.
    4. The remote URL parses as a valid GitHub repository.

    Args:
        work_dir: Root of the git repository to push from.

    Returns:
        ``(token, owner, repo)`` ready for use with :class:`GitHubClient`.

    Raises:
        PushError: If any precondition is not met.
    """
    token = os.environ.get("GITHUB_TOKEN")
    if not token:
        # ``NO_GITHUB:`` prefix is the machine-detectable contract the push
        # callers read to route a GitHub-less setup to ``awaiting_operator``
        # (the ADR-043 escape hatch) rather than the generic ``blocked``
        # failure — mirrors the ``NON_FAST_FORWARD:`` prefix used for rebase
        # routing. Keep the two prefixes in sync with the callers in
        # ``lotsa/tools/push_pr.py`` and ``OrchestratorService._execute_push``.
        raise PushError(
            "NO_GITHUB: GITHUB_TOKEN environment variable is not set. "
            "Export a personal access token with repo scope before pushing, "
            "or Mark complete to close the task after reviewing the worktree."
        )

    remote_url = await _get_remote_url(work_dir)

    if remote_url.startswith("git@") or remote_url.startswith("ssh://"):
        # Redact any user:pass embedded in scp-style or ssh:// URLs.  The
        # `git@host:path` form does not embed credentials (auth happens via
        # SSH keys), so the regex is a deliberate no-op for that case; the
        # `ssh://user:pass@host/path` form is handled by the same pattern
        # as the HTTPS case (see github_client.parse_github_remote).
        safe_url = re.sub(r"://[^@]+@", "://***@", remote_url)
        raise PushError(
            f"Remote origin uses SSH ({safe_url!r}). "
            "The push step requires an HTTPS remote so that the token can be "
            "supplied via GIT_ASKPASS. "
            "Switch to HTTPS with: "
            "git remote set-url origin https://github.com/<owner>/<repo>.git"
        )

    if await _has_uncommitted_changes(work_dir):
        # ADR-024: commit is now an orchestrator-owned posthook that runs on
        # every producer step before push, so a dirty worktree here should be
        # unreachable. Downgraded from a hard error to defence-in-depth: log
        # and proceed. The push is by HEAD SHA (see ``execute_push``), so any
        # uncommitted working-tree changes simply aren't part of the pushed
        # commit. The warning is how a future bug that bypasses the commit
        # posthook becomes visible instead of silently shipping a partial diff.
        logger.warning(
            "Uncommitted changes detected in the working tree at push time. The commit "
            "posthook should have run first (ADR-024); pushing the committed HEAD anyway. "
            "Any uncommitted changes are NOT included in the pushed commit."
        )

    try:
        owner, repo = parse_github_remote(remote_url)
    except ValueError as exc:
        raise PushError(f"Cannot parse GitHub repository from remote URL: {exc}") from exc

    return token, owner, repo


# ---------------------------------------------------------------------------
# PR text generation
# ---------------------------------------------------------------------------
#
# The PR title and body are produced by the ``pr_summary`` agent step (a
# diff-driven Conventional Commits summary written to the ``pr_description``
# artifact).  This module is purely mechanical: ``build_pr_text`` parses that
# artifact (or runs a deterministic, diff/commit-driven fallback when it is
# missing/unparsable) and ``execute_push`` receives a ready ``title``/``body``
# and does only the git push + PR open — no heuristic synthesis on the push
# path.  The fallback fixes the historical ``feat: ---`` bug: a spec whose
# first line was a Markdown separator used to leak straight into the title.

_HEADER_RE = re.compile(r"^#+\s*")

# A title/description that is only punctuation/separators (e.g. ``---``,
# ``***``, ``___``) is never a usable summary.  Used both to reject such a
# first line in ``parse_pr_description`` and to guard the fallback so it can
# never emit ``feat: ---``.
_SEPARATOR_RE = re.compile(r"^[\W_]+$")

# How much of the rendered body we keep before truncating (GitHub rejects PR
# bodies above 65,536 bytes; keep headroom for Markdown overhead).
_PR_BODY_MAX = 60_000
_PR_BODY_TRUNCATION_NOTICE = "\n\n_…body truncated._"

# Conventional Commits types we may emit.
_COMMIT_TYPES = ("feat", "fix", "docs", "style", "refactor", "perf", "test", "build", "ci", "chore", "revert")

# A leading Conventional Commits prefix on a commit subject — stripped before
# reusing the subject as a fallback description so we never double-prefix
# (e.g. ``feat: feat(x): …``).
_CONVENTIONAL_PREFIX_RE = re.compile(r"^[a-z]+(?:\([^)]*\))?!?:\s*", re.IGNORECASE)

# A line that IS a Conventional Commits title (one of our known types, an
# optional scope, optional ``!``, a colon, and a non-empty description).
# ``parse_pr_description`` scans for the first such line so that agent
# preamble ahead of the title (e.g. "I have enough to write the PR
# description.") is discarded instead of becoming the PR headline — the
# prose-tier sibling of the ``feat: ---`` separator bug fixed below.
# Public: the orchestrator's artifact capture uses the same anchor to strip
# narration at the source (``_strip_artifact_narration``).
CC_TITLE_RE = re.compile(rf"^(?:{'|'.join(_COMMIT_TYPES)})(?:\([^)]*\))?!?:\s+\S", re.IGNORECASE)

# Patterns matched (case-insensitive) against commit subjects to choose the
# Conventional Commits type when the changed-file heuristic is inconclusive.
# Order matters — first match wins.
_TYPE_PATTERNS: list[tuple[re.Pattern[str], str]] = [
    (re.compile(r"\bfix(?:e[sd])?\b|\bbug\b|\bpatch\b|\bhotfix\b|\bresolve[sd]?\b|\brepair\b", re.IGNORECASE), "fix"),
    (re.compile(r"\brefactor\b|\brestructure\b|\breorganize\b|\bclean\s*up\b", re.IGNORECASE), "refactor"),
    (re.compile(r"\bdoc(?:s|ument(?:s|ation|ed|ing)?)?\b|\breadme\b|\bchangelog\b", re.IGNORECASE), "docs"),
    (re.compile(r"\btest(?:s|ing)?\b|\bcoverage\b", re.IGNORECASE), "test"),
    (re.compile(r"\bci\b|\bpipeline\b|\bgithub.actions?\b|\bworkflow\b", re.IGNORECASE), "ci"),
    (re.compile(r"\bchore\b|\bdep(?:endenc(?:y|ies))?\b|\bupgrade\b|\bbump\b", re.IGNORECASE), "chore"),
    (re.compile(r"\bperf(?:ormance)?\b|\boptimize\b|\bspeed\b", re.IGNORECASE), "perf"),
    (re.compile(r"\bstyle\b|\bformat\b|\blint\b", re.IGNORECASE), "style"),
]


def parse_pr_description(text: str) -> tuple[str, str] | None:
    """Parse the ``pr_description`` artifact into ``(title, body)``.

    Contract: the first line is the Conventional Commits title; the remainder
    (after a blank line) is the Markdown body.  Agents sometimes narrate
    before delivering ("I have enough to write the PR description.") despite
    the prompt forbidding preamble — so the parser scans for the first line
    that *is* a Conventional Commits title and discards anything before it
    (task ``c94e3ed9``: the narration line became the PR headline).  When no
    CC-shaped line exists, the first line is used as-is (legacy behaviour for
    free-form artifacts).  Returns ``None`` when the artifact is empty or the
    chosen title is unusable (blank or only punctuation/separators such as
    ``---``), so the caller can fall back to the deterministic generator.
    The body may legitimately be empty.
    """
    if not text or not text.strip():
        return None

    lines = text.strip().splitlines()

    # Prefer the first Conventional Commits title line; preamble before it is
    # agent narration, never the title.
    title_idx = 0
    for i, line in enumerate(lines):
        if CC_TITLE_RE.match(_HEADER_RE.sub("", line).strip()):
            title_idx = i
            break

    title = _HEADER_RE.sub("", lines[title_idx]).strip()
    if not title or _SEPARATOR_RE.match(title):
        return None

    body = "\n".join(lines[title_idx + 1 :]).lstrip("\n").strip()
    return title, body


def append_lotsa_trailer(body: str, task_id: str, flow_name: str = "", process_name: str = "") -> str:
    """Append the ``_Generated by Lotsa · task <id> · process <name> · flow <name>_`` trailer.

    Owned by the push path (not the agent) so ``task_id`` / ``process_name`` /
    ``flow_name`` stay authoritative and the trailer is appended exactly once
    regardless of whether the body came from the agent artifact or the fallback
    generator. ``process_name`` (``build`` / ``fix``) is the discriminating
    field — ``flow_name`` is always ``main`` / ``pr_fix`` and can't tell the
    processes apart, so it reads before the flow. Both segments are omitted when
    empty.
    """
    process_part = f" · process {process_name}" if process_name else ""
    flow_part = f" · flow {flow_name}" if flow_name else ""
    trailer = f"_Generated by Lotsa · task {task_id}{process_part}{flow_part}_"
    body = (body or "").rstrip()
    if not body:
        return trailer
    return f"{body}\n\n---\n\n{trailer}"


# ---------------------------------------------------------------------------
# Deterministic fallback — diff/commit-driven (no spec-first-line heuristic)
# ---------------------------------------------------------------------------

_DOCS_RE = re.compile(r"(^|/)docs/|\.md$|(^|/)CHANGELOG", re.IGNORECASE)
_TEST_RE = re.compile(r"(^|/)tests?/|(^|/)test_[^/]+\.py$|_test\.py$|\.test\.[jt]sx?$", re.IGNORECASE)
_CI_RE = re.compile(r"(^|/)\.github/|(^|/)\.gitlab-ci|(^|/)\.circleci/", re.IGNORECASE)


def _detect_commit_type(changed_files: list[str], subjects: list[str]) -> str:
    """Return a Conventional Commits type from the actual diff.

    File-based tiers take precedence (a wholly docs/test/CI changeset is
    unambiguous); otherwise fall back to keyword detection over the branch's
    commit subjects, defaulting to ``feat``.

    The result is always a member of ``_COMMIT_TYPES`` — the keyword tier is
    guarded against a ``_TYPE_PATTERNS`` entry drifting out of the allowed
    Conventional Commits set, so a typo there degrades to ``feat`` rather than
    emitting a non-standard type.
    """
    if changed_files:
        if all(_DOCS_RE.search(f) for f in changed_files):
            return "docs"
        if all(_TEST_RE.search(f) for f in changed_files):
            return "test"
        if all(_CI_RE.search(f) for f in changed_files):
            return "ci"

    joined = "\n".join(subjects)
    for pattern, commit_type in _TYPE_PATTERNS:
        if pattern.search(joined) and commit_type in _COMMIT_TYPES:
            return commit_type
    return "feat"


def _truncate_title(title: str, limit: int = 72) -> str:
    """Truncate *title* to *limit* chars at a word boundary where possible."""
    if len(title) <= limit:
        return title
    cut = title[:limit].rsplit(" ", 1)[0]
    return cut if cut else title[:limit]


def _fallback_description(changed_files: list[str], subjects: list[str], spec: str) -> str:
    """Pick a short imperative description from the diff/commit ground truth.

    Priority: the first informative commit subject (with any existing
    Conventional Commits prefix stripped to avoid double-prefixing) → a
    file-based summary → the spec's first line (guarded against separators) →
    a generic stand-in.  Never returns a separator/empty string.
    """
    for subject in subjects:
        stripped = _CONVENTIONAL_PREFIX_RE.sub("", subject).strip()
        if stripped and not _SEPARATOR_RE.match(stripped):
            return stripped

    if changed_files:
        if len(changed_files) == 1:
            return f"update {Path(changed_files[0]).name}"
        return f"update {len(changed_files)} files"

    if spec and spec.strip():
        first_line = _HEADER_RE.sub("", spec.strip().splitlines()[0]).strip()
        if first_line and not _SEPARATOR_RE.match(first_line):
            return first_line

    return "update project files"


def _build_fallback_title(changed_files: list[str], subjects: list[str], spec: str) -> str:
    """Build a valid Conventional Commits title from the diff — never ``feat: ---``."""
    commit_type = _detect_commit_type(changed_files, subjects)
    description = _fallback_description(changed_files, subjects, spec)
    return _truncate_title(f"{commit_type}: {description}")


def _build_fallback_body(changed_files: list[str], subjects: list[str], spec: str) -> str:
    """Build a concise PR body (no plan dump, no trailer — caller appends it)."""
    paragraphs: list[str] = []

    summary = ""
    if spec and spec.strip():
        first_para = re.split(r"\n\s*\n", spec.strip(), maxsplit=1)[0].strip()
        if first_para and not _SEPARATOR_RE.match(first_para):
            summary = first_para
    if not summary:
        summary = _fallback_description(changed_files, subjects, spec).capitalize() + "."
    paragraphs.append(summary)

    # A short bulleted list of the most relevant changed files (capped).
    if changed_files:
        shown = changed_files[:10]
        bullets = "\n".join(f"- `{f}`" for f in shown)
        if len(changed_files) > len(shown):
            bullets += f"\n- …and {len(changed_files) - len(shown)} more"
        paragraphs.append("Changed files:\n" + bullets)

    body = "\n\n".join(paragraphs)
    if len(body) > _PR_BODY_MAX:
        body = body[: _PR_BODY_MAX - len(_PR_BODY_TRUNCATION_NOTICE)].rstrip() + _PR_BODY_TRUNCATION_NOTICE
    return body


async def _collect_changed_files(work_dir: Path, base_ref: str) -> list[str]:
    """Return the paths changed on the branch vs *base_ref* (best-effort)."""
    proc = await asyncio.create_subprocess_exec(
        "git",
        "diff",
        "--name-only",
        f"{base_ref}...HEAD",
        cwd=work_dir,
        stdout=asyncio.subprocess.PIPE,
        stderr=asyncio.subprocess.PIPE,
    )
    stdout, _ = await proc.communicate()
    if proc.returncode != 0:
        return []
    return [line for line in stdout.decode().splitlines() if line.strip()]


async def _collect_commit_subjects(work_dir: Path, base_ref: str) -> list[str]:
    """Return the branch's commit subjects vs *base_ref* (best-effort)."""
    proc = await asyncio.create_subprocess_exec(
        "git",
        "log",
        "--format=%s",
        f"{base_ref}..HEAD",
        cwd=work_dir,
        stdout=asyncio.subprocess.PIPE,
        stderr=asyncio.subprocess.PIPE,
    )
    stdout, _ = await proc.communicate()
    if proc.returncode != 0:
        return []
    return [line for line in stdout.decode().splitlines() if line.strip()]


async def build_pr_text(
    *,
    work_dir: Path,
    task_id: str,
    base_branch: str | None,
    flow_name: str,
    pr_description: str,
    spec: str,
    process_name: str = "",
) -> tuple[str, str]:
    """Resolve the PR ``(title, body)`` for a new PR.

    Uses the ``pr_description`` artifact (the ``pr_summary`` agent's diff-driven
    Conventional Commits summary) when it parses; otherwise runs the
    deterministic diff/commit-driven fallback.  Either way the Lotsa trailer is
    appended exactly once, naming ``process_name`` (``build`` / ``fix``) so the
    created PR is debuggable back to the process that produced it.  ``spec`` is
    last-resort context for the fallback — the diff and commit messages are the
    ground truth.
    """
    parsed = parse_pr_description(pr_description)
    if parsed is not None:
        title, body = parsed
    else:
        base_ref = await _resolve_base_ref(work_dir, base_branch)
        changed_files = await _collect_changed_files(work_dir, base_ref)
        subjects = await _collect_commit_subjects(work_dir, base_ref)
        title = _build_fallback_title(changed_files, subjects, spec)
        body = _build_fallback_body(changed_files, subjects, spec)

    return title, append_lotsa_trailer(body, task_id, flow_name, process_name)


async def reconcile_branch_with_remote(work_dir: Path, task_id: str) -> bool:
    """Integrate ``origin/lotsa/<task_id>`` into the worktree after a NON_FAST_FORWARD.

    A re-push is rejected non-fast-forward when the remote PR branch advanced
    underneath the worktree — most often because an operator pushed to it
    directly (e.g. GitHub's "resolve conflicts" button adds a merge commit).
    ADR-018 keeps the branch synced with ``origin/main`` but not with the
    branch's *own* remote, so this closes that gap: fetch the remote branch and
    rebase the worktree HEAD onto it. Local commits whose content the remote
    already has drop out as empty, so an identical-content divergence converges
    to a plain fast-forward push.

    Returns ``True`` when the worktree now incorporates the remote (retry the
    push). Returns ``False`` when there is nothing to reconcile (the remote ref
    doesn't exist). On a real content conflict the rebase can't auto-resolve, it
    falls back to ``git merge`` so the conflict is left in the worktree as merge
    markers, then raises :class:`ReconcileConflict` carrying the unmerged paths —
    the caller dispatches ``resolve_conflicts`` (whose agent edits the markers
    and whose ``commit`` posthook completes the merge, identical to the
    origin/main conflict path). :class:`PushError` on other git failures.
    Authenticates the fetch with ``GITHUB_TOKEN`` via ``GIT_ASKPASS`` when set
    (production); a local/test ``origin`` needs no auth.
    """
    branch = f"lotsa/{task_id}"
    token = os.environ.get("GITHUB_TOKEN")
    env = {**os.environ, "GIT_TERMINAL_PROMPT": "0"}
    askpass_path: str | None = None
    if token:
        fd, askpass_path = tempfile.mkstemp(suffix=".sh", prefix="lotsa-askpass-")
        with os.fdopen(fd, "w") as f:
            f.write(
                "#!/bin/sh\n"
                'case "$1" in\n'
                '  Username*) echo "x-access-token" ;;\n'
                '  *) echo "$LOTSA_GIT_TOKEN" ;;\n'
                "esac\n"
            )
        os.chmod(askpass_path, 0o700)
        env.update({"GIT_ASKPASS": askpass_path, "LOTSA_GIT_TOKEN": token})

    def _scrub(s: str) -> str:
        return s.replace(token, "***") if token else s

    async def _git(*args: str) -> tuple[int, str, str]:
        proc = await asyncio.create_subprocess_exec(
            "git",
            *args,
            cwd=work_dir,
            stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.PIPE,
            env=env,
        )
        try:
            stdout, stderr = await asyncio.wait_for(proc.communicate(), timeout=120.0)
        except TimeoutError:
            proc.kill()
            await proc.wait()
            raise PushError(f"git {args[0]} timed out after 120s") from None
        return proc.returncode or 0, _scrub(stdout.decode().strip()), _scrub(stderr.decode().strip())

    try:
        code, _out, err = await _git("fetch", "origin", branch)
        if code != 0:
            low = err.lower()
            if "couldn't find remote ref" in low or "couldn't find remote" in low or "not found" in low:
                # No remote branch to reconcile against — let the caller surface
                # the original push failure unchanged.
                return False
            raise PushError(f"reconcile fetch of origin/{branch} failed: {err}")

        # Rebase the worktree HEAD onto the freshly fetched remote tip, dropping
        # commits that become empty (the identical-content convergence case).
        code, _out, err = await _git("rebase", "--empty=drop", "FETCH_HEAD")
        if code != 0:
            # A rebase can't auto-merge overlapping edits. Abort it (restoring a
            # clean worktree), then MERGE the remote tip instead: a clean merge
            # means the rebase conflict was replay-order noise, so reconcile
            # succeeded — retry the push. A conflicting merge leaves the markers
            # in the worktree in the exact shape the ``resolve_conflicts`` agent
            # and the ``commit`` posthook already handle for the origin/main path
            # (``_sync_branch_to_main``), so the caller can dispatch that agent
            # instead of dead-ending at ``blocked``.
            await _git("rebase", "--abort")
            mcode, _mout, _merr = await _git("merge", "--no-edit", "FETCH_HEAD")
            if mcode == 0:
                return True
            _fc, files_out, _fe = await _git("diff", "--name-only", "--diff-filter=U")
            conflicting = tuple(f.strip() for f in files_out.splitlines() if f.strip())
            if not conflicting:
                # A non-zero merge with no unmerged paths is not a content
                # conflict (dirty/locked worktree, index lock). Abort the
                # half-done merge and surface the original rebase failure rather
                # than routing to resolve_conflicts on a phantom (finding #10).
                await _git("merge", "--abort")
                raise ReconcileConflict(f"rebasing onto origin/{branch} conflicted: {err}")
            raise ReconcileConflict(
                f"rebasing onto origin/{branch} conflicted: {err}",
                conflicting_files=conflicting,
            )
        return True
    finally:
        if askpass_path:
            Path(askpass_path).unlink(missing_ok=True)


# ---------------------------------------------------------------------------
# Main entry point
# ---------------------------------------------------------------------------


async def execute_push(
    work_dir: Path,
    task_id: str,
    pr_number: int | None,
    base_branch: str | None,
    title: str | None = None,
    body: str | None = None,
) -> tuple[int, str, str, str]:
    """Push the task branch and create or locate the pull request.

    Mechanical only: this performs the git push and (when ``pr_number is
    None``) opens the PR with the ready-made ``title``/``body``.  It does no
    title/body synthesis — that is the ``pr_summary`` agent step's job, with a
    deterministic fallback in :func:`build_pr_text`.  ``title``/``body`` are
    only consulted on PR creation; a re-push (``pr_number`` set) keeps the
    existing PR and may pass ``None``.

    Args:
        work_dir: Root of the git repository.
        task_id: Task identifier; the branch is ``lotsa/<task_id>``.
        pr_number: Existing PR number to update, or ``None`` to create a new PR.
        base_branch: Target branch for the PR (e.g. ``"main"``).
        title: PR title to use when creating a new PR (already Conventional Commits).
        body: PR body to use when creating a new PR (already includes the Lotsa trailer).

    Returns:
        ``(pr_number, pr_url, owner, repo)`` after a successful push and PR creation/lookup.

    Raises:
        PushError: On any push failure including non-fast-forward rejections.
    """
    token, owner, repo = await check_preconditions(work_dir)

    branch = f"lotsa/{task_id}"
    push_url = f"https://github.com/{owner}/{repo}.git"

    # Push the worktree's actual HEAD SHA to the canonical remote ref name.
    # The agent may have left the worktree on any local branch (e.g. it
    # followed CLAUDE.md's `feature/...` contributor naming) — that's
    # invisible here because we push by SHA, not by local branch name.
    # The remote branch is always ``lotsa/<task_id>``; the orchestrator,
    # not the agent, owns that contract.
    head_sha = await _get_head_sha(work_dir)
    refspec = f"{head_sha}:refs/heads/{branch}"

    # Use GIT_ASKPASS to supply the token via a helper script so it never
    # appears in argv (visible in /proc/<pid>/cmdline and process listings).
    # The script reads the token from LOTSA_GIT_TOKEN env var at runtime.
    # Git invokes the helper twice — once with a "Username" prompt and once
    # with a "Password" prompt.  GitHub's documented HTTPS+PAT credential
    # form is username=`x-access-token`, password=<PAT>.  Returning the token
    # for both fields works today (GitHub ignores the username for PAT auth)
    # but the canonical form is more defensive against stricter configurations.
    fd, askpass_path = tempfile.mkstemp(suffix=".sh", prefix="lotsa-askpass-")
    try:
        with os.fdopen(fd, "w") as f:
            f.write(
                "#!/bin/sh\n"
                'case "$1" in\n'
                '  Username*) echo "x-access-token" ;;\n'
                '  *) echo "$LOTSA_GIT_TOKEN" ;;\n'
                "esac\n"
            )
        os.chmod(askpass_path, 0o700)

        env = {
            **os.environ,
            "GIT_ASKPASS": askpass_path,
            "GIT_TERMINAL_PROMPT": "0",
            "LOTSA_GIT_TOKEN": token,
        }

        proc = await asyncio.create_subprocess_exec(
            "git",
            "push",
            push_url,
            refspec,
            cwd=work_dir,
            stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.PIPE,
            env=env,
        )
        try:
            _, stderr = await asyncio.wait_for(proc.communicate(), timeout=120.0)
        except TimeoutError:
            proc.kill()
            await proc.wait()  # reap the process and release pipe resources
            raise PushError("Push timed out after 120s") from None
    finally:
        Path(askpass_path).unlink(missing_ok=True)

    if proc.returncode != 0:
        err = stderr.decode().strip()
        # Scrub token from error output in case git leaks it
        err_safe = err.replace(token, "***")
        if "non-fast-forward" in err_safe or "fetch first" in err_safe:
            raise PushError(
                f"NON_FAST_FORWARD: Push rejected for branch {branch!r}. "
                "The remote has commits that the worktree does not. "
                f"Details: {err_safe}"
            )
        raise PushError(f"git push failed (exit {proc.returncode}): {err_safe}")

    # Push succeeded — normalize the worktree's local branch state so the
    # next dispatch round starts on the canonical ``lotsa/<task_id>`` branch
    # rather than whatever the agent left it on.  Best-effort; failures are
    # logged but do not abort the push step (the remote is already correct).
    await _normalize_worktree_branch(work_dir, branch, head_sha)

    client = GitHubClient(token=token, owner=owner, repo=repo)
    try:
        if pr_number is None:
            if base_branch is None:
                base_branch = await client.get_default_branch()
            pr_number = await client.create_pr(
                title=title,
                body=body,
                head=branch,
                base=base_branch,
            )

        pr_url = f"https://github.com/{owner}/{repo}/pull/{pr_number}"
        return pr_number, pr_url, owner, repo
    finally:
        await client.close()
