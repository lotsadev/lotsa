"""Git-native repo provenance discovery + security rails (ADR-044, Phase 5).

A project's own repo may ship agents and workflows under a convention
directory ``<repo>/.lotsa/{agents,workflows}``. This module is the single home
for *reading* that content from the project root and for the path-safety rails
that keep a malicious or careless repo from escaping its ``.lotsa`` tree.

Discovery is deliberately conservative:

* ``repo_lotsa_root`` accepts only a **real directory** named ``.lotsa`` — never
  a file, never a symlink (the top-level symlink-escape guard).
* ``discover_repo_agents`` / ``discover_repo_workflows`` return only direct-child
  subdirectories whose names match ``[a-z0-9_-]{1,64}`` (a safe filesystem /
  registry key), that carry the required manifest (``agent.yaml`` /
  ``process.yaml``), and that do not resolve outside the ``.lotsa`` tree.
* ``is_contained`` / ``assert_contained`` are the containment guard every read
  of repo content passes through, so a symlinked ``system.md`` pointing at an
  operator secret never lands in an agent prompt.

Resolution is project-scoped (against the project root), matching the plan's
Decision 1 — build-time, not per-task-worktree. Cross-repo resolution stays
ADR-035's problem.
"""

from __future__ import annotations

import re
from pathlib import Path

# Convention directory a repo ships its agents/workflows under.
REPO_LOTSA_DIRNAME = ".lotsa"

# A repo agent/workflow name must be a safe filesystem-path segment and registry
# key. Mirrors ``config._PROJECT_ID_RE`` (ADR-029) — bundled names can't be
# silently shadowed by an unsafe repo name either.
_REPO_NAME_RE = re.compile(r"[a-z0-9_-]{1,64}")


def is_valid_repo_name(name: str) -> bool:
    """Return whether *name* is a safe repo agent/workflow name (charset rail).

    The single home for the ``[a-z0-9_-]{1,64}`` rail: both eager discovery
    (:func:`_discover`) and the lazy, by-name resolution path
    (``AgentPromptRegistry`` in :mod:`lotsa.flows`) gate on it, so the documented
    charset rail is enforced wherever repo content is read — not just where a
    directory is enumerated.
    """
    return bool(_REPO_NAME_RE.fullmatch(name))


def is_contained(path: Path, root: Path) -> bool:
    """Return whether *path* resolves to *root* or a descendant of it.

    Both sides are ``resolve()``-d so a symlink whose target escapes *root* is
    caught: a ``system.md`` symlinked to ``~/.ssh/id_rsa`` resolves outside the
    ``.lotsa`` tree and is reported as not contained.
    """
    resolved = Path(path).resolve()
    root_resolved = Path(root).resolve()
    return resolved == root_resolved or root_resolved in resolved.parents


def assert_contained(path: Path, root: Path) -> None:
    """Raise ``ValueError`` if *path* escapes *root* (fail-loud convention)."""
    if not is_contained(path, root):
        raise ValueError(f"Path {path} escapes the repo provenance root {root}")


def repo_lotsa_root(project_path: Path | str) -> Path | None:
    """Return ``<project_path>/.lotsa`` iff it is a real directory, else ``None``.

    A ``.lotsa`` that is a plain file or a symlink is rejected — the latter is
    the top-level symlink-escape guard (a repo could otherwise point ``.lotsa``
    at operator-owned content outside the repo).
    """
    root = Path(project_path) / REPO_LOTSA_DIRNAME
    if root.is_dir() and not root.is_symlink():
        return root
    return None


def _discover(project_path: Path | str, subdir: str, manifest: str) -> dict[str, Path]:
    """Shared scan for ``agents``/``workflows`` under ``.lotsa``.

    Returns ``{name: child_dir}`` for every direct-child subdirectory that has a
    safe-charset name, carries *manifest*, and stays inside the ``.lotsa`` tree.
    """
    root = repo_lotsa_root(project_path)
    out: dict[str, Path] = {}
    if root is None:
        return out
    base = root / subdir
    if not base.is_dir():
        return out
    for child in sorted(base.iterdir()):
        if not child.is_dir():
            continue
        if not is_valid_repo_name(child.name):
            continue  # rail: name charset
        if not (child / manifest).is_file():
            continue
        if not is_contained(child, root):
            continue  # rail: symlink escape
        out[child.name] = child
    return out


def discover_repo_agents(project_path: Path | str) -> dict[str, Path]:
    """Discover repo-shipped agents: ``{name: agent_dir}`` under ``.lotsa/agents/``."""
    return _discover(project_path, "agents", "agent.yaml")


def discover_repo_workflows(project_path: Path | str) -> dict[str, Path]:
    """Discover repo-shipped workflows: ``{name: process_yaml_path}``.

    Symmetric with :func:`discover_repo_agents`, but keyed to each workflow's
    ``process.yaml`` path so the process builder can load it directly.
    """
    found = _discover(project_path, "workflows", "process.yaml")
    return {name: wf_dir / "process.yaml" for name, wf_dir in found.items()}


__all__ = [
    "REPO_LOTSA_DIRNAME",
    "assert_contained",
    "discover_repo_agents",
    "discover_repo_workflows",
    "is_contained",
    "is_valid_repo_name",
    "repo_lotsa_root",
]
