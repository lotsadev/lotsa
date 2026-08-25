"""Prehook runtime (ADR-044 Phase 3).

Prehooks are orchestrator-run operations that fire *before* an agent/action
step dispatches — they set up the dispatch environment. Each prehook is an
``async`` callable matching the ``PrehookCallable`` signature::

    async def my_hook(ctx: TaskContext, config: dict) -> ToolResult: ...

They register themselves via ``lotsa.registry.register_prehook`` at import
time. The orchestrator runs a step's resolved prehooks (declared in the
process YAML's ``prehooks:`` field, or derived from the agent's
``needs_worktree`` property) through ``get_prehook(name)``.

There are two built-ins. The ``worktree`` prehook ensures the task's git
worktree exists; ``sync_root`` (its counterpart for steps that opt *out* of a
worktree) fast-forwards the project root so those steps read current code.

``worktree`` differs from the ``commit`` posthook in two ways worth calling
out:

* **It *creates* the worktree rather than acting on an existing one** — so it
  can't read ``ctx.worktree`` (that path is what it's producing). It invokes
  the ``WorktreeManager`` the orchestrator injects into the prehook
  ``TaskContext`` as ``ctx.worktree_manager``. Worktree creation stays
  orchestrator-owned (ADR-013); the hook only invokes the manager.
* **Its failure is non-fatal.** Unlike a posthook failure (which blocks the
  task), a ``worktree`` prehook failure degrades to the project work_dir with
  a warning — preserving the pre-Phase-3 best-effort behaviour. The
  orchestrator's prehook runner is responsible for that fallback; this hook
  only reports ``success=False``.

``sync_root`` closes the gap Phase 3 opened. A ``needs_worktree: false`` agent
(only ``chat``) derives no worktree, so ``_run_step_prehooks`` falls through to
the project's own checkout — and nothing else in the codebase ever updates that
checkout's working tree (``WorktreeManager._resolve_default_base_ref`` fetches
in the project root but only moves remote-tracking refs; ADR-018's
``_sync_branch_to_main`` bails outright when there is no worktree). The result
was a chat agent reading a tree frozen at whatever commit the clone was last
left on, confidently reporting that files added upstream "don't exist".
``sync_root`` fetches and fast-forwards that checkout, under rails that make
losing operator work impossible (see the function docstring).

Future improvement (deliberately not taken here): a single shared read-only
worktree per project, pinned to ``origin/<default_branch>`` and reused by every
worktree-less task. That would guarantee freshness without touching the
operator's checkout at all, at the cost of exactly one extra checkout per
project rather than one per task. It needs its own lifecycle (creation,
staleness, cleanup), so it is tracked in ``docs/post-launch-plan.md`` rather
than bolted on here.
"""

from __future__ import annotations

import asyncio
import logging
import os
from pathlib import Path
from typing import TYPE_CHECKING, Any

if TYPE_CHECKING:
    from lotsa.tools import TaskContext, ToolResult

logger = logging.getLogger(__name__)

# Bound on the network fetch, mirroring ``WorktreeManager._resolve_default_base_ref``:
# a slow or unreachable origin must never hold up a dispatch.
_FETCH_TIMEOUT_SECONDS = 30

# One lock per project root, so two chat tasks on the same project can't run
# ``git merge`` in the same checkout at the same time and collide on
# ``index.lock``. The collision isn't destructive (the loser's merge just
# fails), but it would surface as a spurious "couldn't be fast-forwarded"
# notice to the operator. Mirrors ``WorktreeManager``'s per-task locking; a
# cache, not state of record (ADR-040).
_REPO_LOCKS: dict[str, asyncio.Lock] = {}


def _repo_lock(repo: Path) -> asyncio.Lock:
    key = str(repo)
    if key not in _REPO_LOCKS:
        _REPO_LOCKS[key] = asyncio.Lock()
    return _REPO_LOCKS[key]


async def worktree_prehook(ctx: TaskContext, config: dict[str, Any]) -> ToolResult:
    """Ensure the task's git worktree exists via the injected ``WorktreeManager``.

    Returns ``success=True`` with the created path in ``metadata['worktree']``
    (informational — the orchestrator re-resolves the work_dir independently via
    ``get_path``). Returns ``success=False`` when no manager was injected or the
    create fails; the orchestrator's prehook runner treats that as non-fatal and
    falls back to the project work_dir.
    """
    from lotsa.tools import ToolResult

    manager = ctx.worktree_manager
    if manager is None:
        return ToolResult(
            success=False,
            output="worktree prehook: no WorktreeManager in context",
            metadata={"error_kind": "no_worktree_manager"},
        )
    try:
        path = await manager.create(ctx.task_id)
    except Exception as exc:  # noqa: BLE001 — any create failure degrades to the project work_dir
        return ToolResult(
            success=False,
            output=f"worktree prehook: create failed: {exc}",
            metadata={"error_kind": "worktree_create_failed"},
        )
    return ToolResult(success=True, output=f"worktree ready at {path}", metadata={"worktree": str(path)})


async def _git(repo: Path, *args: str, timeout: float | None = None) -> tuple[int, str, str]:
    """Run ``git -C <repo> <args>``; return ``(returncode, stdout, stderr)``.

    Async subprocess API with argv tokens (never a shell string) per the CE git
    discipline. Never raises: a timeout, a missing git binary, or a
    non-existent repo all come back as a non-zero code with the reason in
    stderr, because every caller here is best-effort. Stderr is scrubbed at the
    source — it can carry a tokenized remote URL, and these strings reach audit
    rows and logs (Constitution §1.2).
    """
    from rigg.scrub import scrub_secrets

    try:
        proc = await asyncio.create_subprocess_exec(
            "git",
            "-C",
            str(repo),
            *args,
            stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.PIPE,
            env=_git_env(),
        )
    except (OSError, ValueError) as exc:  # git absent, repo path unusable
        return 1, "", str(exc)
    try:
        stdout, stderr = await asyncio.wait_for(proc.communicate(), timeout=timeout)
    except TimeoutError:
        proc.kill()
        await proc.wait()
        return 1, "", f"git {args[0] if args else ''} timed out after {timeout}s"
    return proc.returncode or 0, stdout.decode(), scrub_secrets(stderr.decode())


def _git_env() -> dict[str, str]:
    """Environment for the fetch: no TTY prompt, token auth when configured.

    Same shape as the orchestrator's ``_sync_branch_to_main``: a private or
    auth'd remote otherwise dies on "could not read Username" with no TTY.
    Local/file remotes ignore the helper, so tests are unaffected.
    """
    env = {**os.environ, "GIT_TERMINAL_PROMPT": "0"}
    token = os.environ.get("GITHUB_TOKEN")
    if token:
        from rigg.git import TokenCredentialStrategy

        env.update(TokenCredentialStrategy(token).env())
    return env


async def sync_root_prehook(ctx: TaskContext, config: dict[str, Any]) -> ToolResult:
    """Fast-forward the project root to ``origin/<default_branch>``.

    The counterpart to the ``worktree`` prehook, derived for exactly the steps
    that opt out of a worktree (today: ``chat``). Those steps run in the
    project's own checkout, so this is the only place their view of the code
    can be brought up to date.

    Two rails make this non-destructive, both checked before any tree-touching
    verb runs:

    * **HEAD must be on the project's default branch.** An operator sitting on
      a feature branch keeps it — we never check anything out.
    * **Tracked files must be clean.** Untracked files are fine (a
      fast-forward preserves them); a modified tracked file means uncommitted
      work we refuse to risk.

    The merge verb is ``--ff-only``, which cannot rewrite or discard a commit
    by construction — so even a diverged root (local commits on the default
    branch) fails safely rather than being reset.

    A skip is reported as ``success=False``, which the orchestrator's prehook
    runner already treats as non-fatal (log + continue in the project
    work_dir); the step still dispatches. When the skip *mattered* — the root
    is genuinely behind — an operator-visible message is also written, because
    the alternative is an agent reading a stale tree with nobody told. A
    healthy sync stays silent: a chat REPL re-dispatches on every turn, and a
    per-turn "all good" note would be pure noise.

    Never reports ``metadata['worktree']`` — it creates no worktree, and the
    orchestrator reads that key to set the step's work_dir.
    """
    from lotsa.tools import ToolResult

    manager = ctx.worktree_manager
    if manager is None:
        return ToolResult(
            success=False,
            output="sync_root prehook: no WorktreeManager in context",
            metadata={"error_kind": "no_worktree_manager"},
        )

    repo = manager.repo
    branch = manager.default_branch
    target = f"origin/{branch}"

    async with _repo_lock(repo):
        return await _sync(ctx, repo, branch, target)


async def _sync(ctx: TaskContext, repo: Path, branch: str, target: str) -> ToolResult:
    """The body of :func:`sync_root_prehook`, under the project's repo lock."""
    from lotsa.tools import ToolResult

    # 1. Best-effort fetch. A failure is not fatal on its own — an already-
    #    fetched ref may still let us fast-forward — so only log it.
    rc, _out, err = await _git(repo, "fetch", "origin", branch, timeout=_FETCH_TIMEOUT_SECONDS)
    if rc != 0:
        logger.warning("sync_root: fetch of %s in %s failed: %s", branch, repo, err.strip()[:200])

    # 2. No upstream ref (no origin, never fetched, or not a git repo at all)
    #    → nothing to sync against. Quiet: this is the documented no-remote
    #    case, not a problem to escalate to the operator.
    rc, _out, _err = await _git(repo, "rev-parse", "--verify", "--quiet", target)
    if rc != 0:
        return ToolResult(
            success=False,
            output=f"sync_root: {target} not found locally — leaving the project root as-is",
            metadata={"error_kind": "no_upstream_ref"},
        )

    # 3. How far behind is the checkout? Computed before the rails so a skip
    #    can say whether it actually mattered.
    rc, out, err = await _git(repo, "rev-list", "--count", f"HEAD..{target}")
    if rc != 0:
        return ToolResult(
            success=False,
            output=f"sync_root: could not measure divergence from {target}: {err.strip()}",
            metadata={"error_kind": "git_error"},
        )
    behind = int(out.strip() or "0")
    if behind == 0:
        return ToolResult(success=True, output=f"sync_root: project root already current with {target}")

    # 4. Rail one — HEAD is on the default branch (``symbolic-ref`` also fails
    #    on a detached HEAD, which is equally not-our-branch).
    rc, head, _err = await _git(repo, "symbolic-ref", "--quiet", "--short", "HEAD")
    if rc != 0 or head.strip() != branch:
        on = head.strip() or "a detached HEAD"
        return await _skip(ctx, repo, branch, behind, f"the checkout is on {on}, not {branch}", "not_on_default_branch")

    # 5. Rail two — no uncommitted work in tracked files. Untracked files
    #    survive a fast-forward untouched, so they don't veto it.
    rc, status, _err = await _git(repo, "status", "--porcelain", "--untracked-files=no")
    if rc != 0 or status.strip():
        return await _skip(ctx, repo, branch, behind, "the checkout has uncommitted changes", "dirty_worktree")

    # 6. Fast-forward. Cannot clobber: a diverged root fails here instead.
    rc, _out, err = await _git(repo, "merge", "--ff-only", target)
    if rc != 0:
        return await _skip(ctx, repo, branch, behind, f"it could not be fast-forwarded ({err.strip()})", "ff_failed")

    return ToolResult(
        success=True,
        output=f"sync_root: fast-forwarded project root to {target} ({behind} commit(s))",
        metadata={"synced_to": target, "commits_applied": behind},
    )


async def _skip(
    ctx: TaskContext,
    repo: Path,
    branch: str,
    behind: int,
    reason: str,
    error_kind: str,
) -> ToolResult:
    """Report a skipped sync that mattered — and tell the operator.

    Reached only when the root is known to be behind, so the message is always
    actionable: the step is about to run against stale code and the operator is
    the only one who can fix the cause (commit/stash, or switch back to the
    default branch).
    """
    from lotsa.tools import ToolResult

    message = (
        f"Project root `{repo}` is {behind} commit(s) behind `origin/{branch}` and was not synced "
        f"because {reason}. This step is reading the older tree — files added upstream since then "
        f"will look missing."
    )
    logger.warning("sync_root: %s", message)
    if ctx.db is not None:
        try:
            await ctx.db.add_message(ctx.task_id, "system", ctx.last_run_step, message, "status_change")
        except Exception:  # noqa: BLE001 — a prehook must never break a dispatch
            logger.warning("sync_root: could not record the stale-root notice", exc_info=True)
    return ToolResult(success=False, output=message, metadata={"error_kind": error_kind, "behind": behind})


# ---------------------------------------------------------------------------
# Built-in prehook registration — side effect on import
# ---------------------------------------------------------------------------

from lotsa.registry import is_prehook_registered, register_prehook  # noqa: E402

# Idempotent on re-import within the same process (test isolation, hot
# reload): only register when absent so we don't trip register_prehook's
# collision guard. Uses the public membership probe rather than catching
# ValueError so genuine validation failures still surface.
if not is_prehook_registered("worktree"):
    register_prehook("worktree", worktree_prehook)
if not is_prehook_registered("sync_root"):
    register_prehook("sync_root", sync_root_prehook)

__all__ = ["sync_root_prehook", "worktree_prehook"]
