"""Git operations abstracted from authentication strategy.

Extracted from: bot/orchestrator.py _git_env() (lines 511-525),
setup_workspace() (lines 539-567).
"""

from __future__ import annotations

import asyncio
import contextlib
import logging
import os
import shutil
import subprocess
from datetime import UTC, datetime
from pathlib import Path
from typing import Protocol

from rigg.scrub import scrub_secrets

logger = logging.getLogger(__name__)


class CredentialStrategy(Protocol):
    """Provide environment variables for git authentication."""

    def env(self) -> dict[str, str]: ...


class TokenCredentialStrategy:
    """Inject a token via a git credential helper.

    Security note (audit finding #4): the token is placed in ``GIT_CONFIG_VALUE_0``,
    which the child git process — and every process git itself spawns — inherits
    in its environment. ``GitRunner.run`` scrubs the token from any
    ``CalledProcessError`` output, but for a hardened setup prefer a
    ``GIT_ASKPASS`` script that reads the token from a private env var (see
    ``lotsa/push_step.py``). These classes are part of the shared SDK's public
    API but are not used by the CE orchestrator, which owns its own push path.
    """

    def __init__(self, token: str, host: str = "github.com") -> None:
        self._token = token
        self._host = host

    def env(self) -> dict[str, str]:
        credential_helper = (
            f"!printf 'protocol=https\\nhost={self._host}\\nusername=x-access-token\\npassword={self._token}\\n'"
        )
        return {
            "GIT_TERMINAL_PROMPT": "0",
            "GIT_CONFIG_COUNT": "1",
            "GIT_CONFIG_KEY_0": "credential.helper",
            "GIT_CONFIG_VALUE_0": credential_helper,
        }


class GitRunner:
    """Git operations with pluggable credential injection."""

    def __init__(self, repo_url: str, credentials: CredentialStrategy) -> None:
        self._repo_url = repo_url
        self._credentials = credentials

    async def run(self, cmd: list[str], cwd: Path) -> subprocess.CompletedProcess:
        """Run a git command with credentials injected.

        Scrubs any injected token from a failed command's captured output before
        the ``CalledProcessError`` propagates, so callers that log ``exc.stderr``
        cannot leak the credential (§1.2; finding #4).
        """
        env = {**os.environ, **self._credentials.env()}
        try:
            return subprocess.run(cmd, cwd=cwd, capture_output=True, text=True, check=True, env=env)
        except subprocess.CalledProcessError as exc:
            exc.stderr = scrub_secrets(exc.stderr or "")
            exc.stdout = scrub_secrets(exc.stdout or "")
            raise

    async def setup_new_branch(self, work_dir: Path, base: str = "main") -> tuple[Path, str]:
        """Clone the repo and create a new branch."""
        branch = f"rigg/{datetime.now(UTC).strftime('%Y%m%d-%H%M%S')}"

        if not (work_dir / ".git").exists():
            await self.run(["git", "clone", self._repo_url, str(work_dir)], cwd=work_dir.parent)
        else:
            await self.run(["git", "fetch", "origin"], cwd=work_dir)
            await self.run(["git", "checkout", base], cwd=work_dir)
            await self.run(["git", "reset", "--hard", f"origin/{base}"], cwd=work_dir)

        await self.run(["git", "checkout", "-b", branch], cwd=work_dir)
        return work_dir, branch

    async def setup_existing_branch(self, work_dir: Path, branch: str) -> Path:
        """Check out an existing branch and pull latest."""
        if not (work_dir / ".git").exists():
            await self.run(["git", "clone", self._repo_url, str(work_dir)], cwd=work_dir.parent)

        await self.run(["git", "fetch", "origin"], cwd=work_dir)
        await self.run(["git", "checkout", branch], cwd=work_dir)

        with contextlib.suppress(subprocess.CalledProcessError):
            await self.run(["git", "pull", "origin", branch], cwd=work_dir)

        return work_dir


class WorktreeManager:
    """Manage per-task git worktrees for concurrent agent execution."""

    def __init__(
        self,
        repo_path: Path,
        worktrees_dir: Path,
        default_branch: str = "main",
    ) -> None:
        self._repo = repo_path.resolve()
        self._dir = worktrees_dir
        self._default_branch = default_branch
        self._locks: dict[str, asyncio.Lock] = {}

    @property
    def default_branch(self) -> str:
        """The base branch new worktrees are created from (default ``main``).

        Read-only accessor so callers (e.g. CE's diff path) can resolve the
        same base ref a worktree was created from without reaching into the
        private ``_default_branch`` field or the network-fetching
        ``_resolve_default_base_ref``.
        """
        return self._default_branch

    @property
    def repo(self) -> Path:
        """The resolved repo root this manager creates worktrees from.

        Read-only accessor so callers (e.g. CE's per-project worktree cache,
        which reconciles a cached manager against a project's resolved path)
        can compare against the repo root without reaching across the SDK
        boundary into the private ``_repo`` field.
        """
        return self._repo

    @property
    def dir(self) -> Path:
        """The directory worktrees are created under (``<dir>/<task_id>``).

        Read-only accessor so callers can assert/compare the namespaced
        worktree root without reaching into the private ``_dir`` field.
        """
        return self._dir

    @staticmethod
    def _validate_id(task_id: str) -> None:
        if not task_id or "/" in task_id or "\\" in task_id or ".." in task_id:
            raise ValueError(f"Invalid task_id for worktree: {task_id!r}")

    def _get_lock(self, task_id: str) -> asyncio.Lock:
        if task_id not in self._locks:
            self._locks[task_id] = asyncio.Lock()
        return self._locks[task_id]

    async def create(self, task_id: str, base_ref: str | None = None) -> Path:
        """Create a worktree for a task. Returns the worktree path.

        Idempotent — returns existing path if worktree already exists.
        Thread-safe via per-task asyncio.Lock.

        When ``base_ref`` is None (the default), the manager fetches
        ``self._default_branch`` from origin and bases the worktree off
        ``origin/<default_branch>``. This ensures every new task starts
        from the current upstream state instead of whatever stale commit
        the local default branch happens to be sitting on.

        When ``base_ref`` is supplied explicitly, it is used as-is — no
        fetch, no resolution. Callers wanting to base off a specific
        commit (e.g. a rebase scenario reusing a task's prior branch)
        bypass the default sync.
        """
        self._validate_id(task_id)

        if base_ref is None:
            base_ref = await self._resolve_default_base_ref()

        async with self._get_lock(task_id):
            wt_path = self._dir / task_id
            # Validate existing path is a real worktree (has .git file)
            if wt_path.exists():
                if (wt_path / ".git").exists():
                    return wt_path
                # Stale directory — clean it up
                shutil.rmtree(wt_path, ignore_errors=True)

            self._dir.mkdir(parents=True, exist_ok=True)
            branch = f"lotsa/{task_id}"
            try:
                await asyncio.to_thread(
                    subprocess.run,
                    ["git", "-C", str(self._repo), "worktree", "add", str(wt_path), "-B", branch, base_ref],
                    capture_output=True,
                    text=True,
                    check=True,
                )
            except subprocess.CalledProcessError as exc:
                logger.error("Failed to create worktree for %s: %s", task_id, exc.stderr)
                raise
            return wt_path

    async def _resolve_default_base_ref(self) -> str:
        """Fetch the default branch from origin and return the remote ref.

        Returns ``origin/<default_branch>`` when the fetch succeeds (or
        when that ref already exists locally from a prior fetch). Falls
        back to ``HEAD`` — and logs a warning — when origin isn't
        configured, the fetch fails, and the remote ref isn't cached
        locally either. The fallback preserves behaviour for repos
        without remotes (CI sandboxes, fresh local clones during
        development) while making the upstream-sync the norm everywhere
        an origin exists.
        """
        target = f"origin/{self._default_branch}"

        # Best-effort fetch. A 30s timeout keeps a slow or unreachable
        # network from blocking task dispatch indefinitely.
        try:
            await asyncio.to_thread(
                subprocess.run,
                ["git", "-C", str(self._repo), "fetch", "origin", self._default_branch],
                capture_output=True,
                text=True,
                check=True,
                timeout=30,
            )
        except (subprocess.CalledProcessError, subprocess.TimeoutExpired, FileNotFoundError) as exc:
            detail = getattr(exc, "stderr", None) or str(exc)
            if isinstance(detail, bytes):
                detail = detail.decode("utf-8", errors="replace")
            logger.warning(
                "Could not fetch %s from origin: %s. Will use whatever local state exists for that ref.",
                self._default_branch,
                detail.strip()[:200] if isinstance(detail, str) else detail,
            )

        # Verify the remote-tracking ref exists locally (either just
        # fetched, or pre-existing from a prior fetch).
        check = await asyncio.to_thread(
            subprocess.run,
            ["git", "-C", str(self._repo), "rev-parse", "--verify", "--quiet", target],
            capture_output=True,
            text=True,
        )
        if check.returncode != 0:
            logger.warning(
                "Ref %s not found locally — falling back to HEAD. Worktree may branch from a stale commit. "
                "Configure an `origin` remote with a `%s` branch to get fresh upstream snapshots.",
                target,
                self._default_branch,
            )
            return "HEAD"

        return target

    async def remove(self, task_id: str) -> None:
        """Remove a worktree and its branch. No error if it doesn't exist."""
        self._validate_id(task_id)
        async with self._get_lock(task_id):
            await self._remove_worktree(task_id)
            # Clean up lock entry while still held to prevent race
            self._locks.pop(task_id, None)

    async def _remove_worktree(self, task_id: str) -> None:
        """Internal remove — must be called under lock."""
        wt_path = self._dir / task_id
        if not wt_path.exists():
            return

        branch = f"lotsa/{task_id}"
        try:
            await asyncio.to_thread(
                subprocess.run,
                ["git", "-C", str(self._repo), "worktree", "remove", str(wt_path), "--force"],
                capture_output=True,
                text=True,
                check=True,
            )
        except subprocess.CalledProcessError as exc:
            logger.warning("Failed to remove worktree for %s: %s", task_id, exc.stderr)
            # Prune stale worktree references so create() doesn't see a stale path
            with contextlib.suppress(subprocess.CalledProcessError):
                await asyncio.to_thread(
                    subprocess.run,
                    ["git", "-C", str(self._repo), "worktree", "prune"],
                    capture_output=True,
                    text=True,
                    check=True,
                )

        # Clean up the branch
        with contextlib.suppress(subprocess.CalledProcessError):
            await asyncio.to_thread(
                subprocess.run,
                ["git", "-C", str(self._repo), "branch", "-D", branch],
                capture_output=True,
                text=True,
                check=True,
            )

        # Clean up the per-task agent HOME the Docker runner mounts as a worktree
        # sibling (``.agent-home-<task_id>``; see lotsa/docker_runner.py) so it
        # doesn't accumulate after the worktree is gone. No-op in native mode.
        agent_home = self._dir / f".agent-home-{task_id}"
        if agent_home.exists():
            await asyncio.to_thread(shutil.rmtree, agent_home, ignore_errors=True)

    def get_path(self, task_id: str) -> Path | None:
        """Return the worktree path if it exists and is valid, None otherwise."""
        self._validate_id(task_id)
        wt_path = self._dir / task_id
        if wt_path.exists() and (wt_path / ".git").exists():
            return wt_path
        return None

    def exists(self, task_id: str) -> bool:
        """Check if a valid worktree exists for this task."""
        self._validate_id(task_id)
        wt_path = self._dir / task_id
        return wt_path.exists() and (wt_path / ".git").exists()
