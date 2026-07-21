"""RED tests for the git-native repo-provenance helper (ADR-044, Phase 5).

Phase 5 lets a project's own repo ship agents and workflows under a
convention directory ``<repo>/.lotsa/{agents,workflows}``. Discovery reads
those from the **project root** (Decision 1 in the plan — project-scoped,
build-time), and a hardened security layer keeps a malicious/careless repo
from escaping its ``.lotsa`` tree (path traversal, symlink escape) or shipping
a name that isn't a safe filesystem/registry key.

This module pins down the ``lotsa.provenance`` helper the coding step
implements:

* ``repo_lotsa_root(project_path) -> Path | None`` — the real ``.lotsa``
  directory (never a file, never a symlink), else ``None``.
* ``discover_repo_agents(project_path) -> dict[str, Path]`` — direct-child
  agent dirs carrying ``agent.yaml`` whose names match ``[a-z0-9_-]{1,64}``
  and that do not escape the ``.lotsa`` tree.
* ``discover_repo_workflows(project_path) -> dict[str, Path]`` — symmetric,
  keyed to each ``process.yaml`` path.
* ``is_contained(path, root) -> bool`` / ``assert_contained(path, root)`` —
  the symlink-escape guard every repo-content read passes through.

They fail RED today because ``lotsa.provenance`` does not exist. Imports are
inside each test so a missing symbol reds that behaviour independently rather
than erroring collection for the whole module.
"""

from __future__ import annotations

from pathlib import Path

import pytest
import yaml

# ───────────────────────────────────────────────────────────────────────────
# fixture helpers
# ───────────────────────────────────────────────────────────────────────────


def _write_repo_agent(repo: Path, name: str, *, klass: str = "worker", outcomes=("COMPLETED", "INPUT")) -> Path:
    """Write ``<repo>/.lotsa/agents/<name>/{agent.yaml,system.md,user.md}``."""
    agent_dir = repo / ".lotsa" / "agents" / name
    agent_dir.mkdir(parents=True, exist_ok=True)
    (agent_dir / "system.md").write_text(f"system body for {name}\n")
    (agent_dir / "user.md").write_text(f"user body for {name}\n")
    (agent_dir / "agent.yaml").write_text(yaml.safe_dump({"name": name, "class": klass, "outcomes": list(outcomes)}))
    return agent_dir


def _write_repo_workflow(repo: Path, name: str, body: str) -> Path:
    """Write ``<repo>/.lotsa/workflows/<name>/process.yaml`` and return its path."""
    wf_dir = repo / ".lotsa" / "workflows" / name
    wf_dir.mkdir(parents=True, exist_ok=True)
    proc = wf_dir / "process.yaml"
    proc.write_text(body)
    return proc


# ───────────────────────────────────────────────────────────────────────────
# repo_lotsa_root
# ───────────────────────────────────────────────────────────────────────────


def test_repo_lotsa_root_found(tmp_path):
    from lotsa.provenance import repo_lotsa_root

    (tmp_path / ".lotsa").mkdir()
    root = repo_lotsa_root(tmp_path)
    assert root == tmp_path / ".lotsa"


def test_repo_lotsa_root_absent_returns_none(tmp_path):
    from lotsa.provenance import repo_lotsa_root

    assert repo_lotsa_root(tmp_path) is None


def test_repo_lotsa_root_rejects_a_file(tmp_path):
    """A ``.lotsa`` file (not a directory) is not a provenance root."""
    from lotsa.provenance import repo_lotsa_root

    (tmp_path / ".lotsa").write_text("not a dir\n")
    assert repo_lotsa_root(tmp_path) is None


def test_repo_lotsa_root_rejects_a_symlink(tmp_path):
    """A symlinked ``.lotsa`` is rejected — the top-level symlink-escape guard.

    Otherwise a repo could point ``.lotsa`` at an operator-owned directory
    outside the repo and have its content injected into agent prompts.
    """
    from lotsa.provenance import repo_lotsa_root

    real = tmp_path / "elsewhere"
    real.mkdir()
    link = tmp_path / ".lotsa"
    link.symlink_to(real, target_is_directory=True)
    assert repo_lotsa_root(tmp_path) is None


# ───────────────────────────────────────────────────────────────────────────
# discover_repo_agents
# ───────────────────────────────────────────────────────────────────────────


def test_discover_repo_agents_finds_valid(tmp_path):
    from lotsa.provenance import discover_repo_agents

    _write_repo_agent(tmp_path, "mycoder")
    found = discover_repo_agents(tmp_path)
    assert "mycoder" in found
    assert found["mycoder"] == tmp_path / ".lotsa" / "agents" / "mycoder"


def test_discover_repo_agents_absent_dir_is_empty(tmp_path):
    from lotsa.provenance import discover_repo_agents

    # ``.lotsa`` exists but has no agents/ subdir.
    (tmp_path / ".lotsa").mkdir()
    assert discover_repo_agents(tmp_path) == {}


def test_discover_repo_agents_requires_agent_yaml(tmp_path):
    from lotsa.provenance import discover_repo_agents

    stray = tmp_path / ".lotsa" / "agents" / "nostructure"
    stray.mkdir(parents=True)
    (stray / "system.md").write_text("orphan prompt\n")  # no agent.yaml
    assert "nostructure" not in discover_repo_agents(tmp_path)


def test_discover_repo_agents_rejects_bad_charset_name(tmp_path):
    """A repo agent name must be a safe filesystem/registry key ``[a-z0-9_-]{1,64}``."""
    from lotsa.provenance import discover_repo_agents

    _write_repo_agent(tmp_path, "good")
    # Uppercase + space violate the charset; the dir carries a valid agent.yaml
    # but must still be excluded by the name rail.
    bad = tmp_path / ".lotsa" / "agents" / "Bad Name"
    bad.mkdir(parents=True)
    (bad / "agent.yaml").write_text(yaml.safe_dump({"class": "worker", "outcomes": ["COMPLETED"]}))
    found = discover_repo_agents(tmp_path)
    assert "good" in found
    assert "Bad Name" not in found


def test_discover_repo_agents_excludes_symlink_escape(tmp_path):
    """An agent dir that is a symlink pointing OUTSIDE ``.lotsa`` is excluded.

    ``child.is_dir()`` follows the symlink (True), so without the containment
    guard the escaping dir would be discovered and its ``system.md`` (which
    could symlink to a secret) injected. The containment rail excludes it.
    """
    from lotsa.provenance import discover_repo_agents

    # A valid agent living entirely outside the repo tree.
    outside = tmp_path / "outside" / "secretagent"
    outside.mkdir(parents=True)
    (outside / "agent.yaml").write_text(yaml.safe_dump({"class": "worker", "outcomes": ["COMPLETED"]}))

    agents_dir = tmp_path / "repo" / ".lotsa" / "agents"
    agents_dir.mkdir(parents=True)
    (agents_dir / "evil").symlink_to(outside, target_is_directory=True)

    assert "evil" not in discover_repo_agents(tmp_path / "repo")


# ───────────────────────────────────────────────────────────────────────────
# discover_repo_workflows
# ───────────────────────────────────────────────────────────────────────────


def test_discover_repo_workflows_finds_valid(tmp_path):
    from lotsa.provenance import discover_repo_workflows

    proc = _write_repo_workflow(tmp_path, "myflow", "name: myflow\njobs:\n  - name: work\n")
    found = discover_repo_workflows(tmp_path)
    assert "myflow" in found
    # Keyed to the process.yaml path so the builder can load it directly.
    assert found["myflow"] == proc


def test_discover_repo_workflows_requires_process_yaml(tmp_path):
    from lotsa.provenance import discover_repo_workflows

    stray = tmp_path / ".lotsa" / "workflows" / "empty"
    stray.mkdir(parents=True)
    (stray / "README.md").write_text("no process.yaml here\n")
    assert "empty" not in discover_repo_workflows(tmp_path)


def test_discover_repo_workflows_rejects_bad_charset_name(tmp_path):
    from lotsa.provenance import discover_repo_workflows

    _write_repo_workflow(tmp_path, "ok", "name: ok\njobs:\n  - name: work\n")
    bad = tmp_path / ".lotsa" / "workflows" / "UPPER"
    bad.mkdir(parents=True)
    (bad / "process.yaml").write_text("name: UPPER\njobs:\n  - name: work\n")
    found = discover_repo_workflows(tmp_path)
    assert "ok" in found
    assert "UPPER" not in found


def test_discover_repo_workflows_rejects_symlinked_manifest(tmp_path):
    """A REAL, contained workflow dir whose ``process.yaml`` is a symlink to a
    file OUTSIDE the repo must be excluded (manifest-file symlink escape).

    Discovery only containment-checked the workflow *directory*, not the
    ``process.yaml`` it returns — and unlike repo agents (re-checked at read
    time by ``AgentPromptRegistry._repo_candidate_ok``), the workflow build path
    has no second gate, so ``_build_repo_processes`` would ``read_text`` an
    operator secret. Pre-fix: ``"leaky"`` IS in the result (the symlinked
    manifest is a file and the dir is contained); post-fix: excluded.
    """
    from lotsa.provenance import discover_repo_workflows

    outside = tmp_path / "operator_secret.yaml"
    outside.write_text("name: SECRET\njobs:\n  - name: exfil\n")

    wf_dir = tmp_path / "repo" / ".lotsa" / "workflows" / "leaky"
    wf_dir.mkdir(parents=True)
    (wf_dir / "process.yaml").symlink_to(outside)

    assert "leaky" not in discover_repo_workflows(tmp_path / "repo")


def test_discover_repo_agents_rejects_symlinked_manifest(tmp_path):
    """Same manifest-file symlink-escape guard for repo agents' ``agent.yaml``
    (the class fix in ``_discover`` covers both agents and workflows)."""
    from lotsa.provenance import discover_repo_agents

    outside = tmp_path / "operator_secret.yaml"
    outside.write_text(yaml.safe_dump({"class": "worker", "outcomes": ["COMPLETED"]}))

    agent_dir = tmp_path / "repo" / ".lotsa" / "agents" / "leaky"
    agent_dir.mkdir(parents=True)
    (agent_dir / "agent.yaml").symlink_to(outside)

    assert "leaky" not in discover_repo_agents(tmp_path / "repo")


# ───────────────────────────────────────────────────────────────────────────
# containment guard (symlink escape)
# ───────────────────────────────────────────────────────────────────────────


def test_is_contained_true_for_child(tmp_path):
    from lotsa.provenance import is_contained

    root = tmp_path / ".lotsa"
    root.mkdir()
    child = root / "agents" / "x" / "system.md"
    child.parent.mkdir(parents=True)
    child.write_text("body\n")
    assert is_contained(child, root) is True


def test_is_contained_false_for_outside_path(tmp_path):
    from lotsa.provenance import is_contained

    root = tmp_path / "repo" / ".lotsa"
    root.mkdir(parents=True)
    outside = tmp_path / "secret.txt"
    outside.write_text("secret\n")
    assert is_contained(outside, root) is False


def test_is_contained_false_for_symlinked_secret(tmp_path):
    """A file that is a symlink resolving outside ``root`` is not contained."""
    from lotsa.provenance import is_contained

    root = tmp_path / "repo" / ".lotsa"
    root.mkdir(parents=True)
    secret = tmp_path / "id_rsa"
    secret.write_text("PRIVATE KEY\n")
    link = root / "agents" / "x" / "system.md"
    link.parent.mkdir(parents=True)
    link.symlink_to(secret)
    assert is_contained(link, root) is False


def test_assert_contained_raises_on_escape(tmp_path):
    from lotsa.provenance import assert_contained

    root = tmp_path / "repo" / ".lotsa"
    root.mkdir(parents=True)
    outside = tmp_path / "escape.txt"
    outside.write_text("nope\n")
    # Fail-loud on escape, matching the module's ValueError convention.
    with pytest.raises(ValueError):
        assert_contained(outside, root)
