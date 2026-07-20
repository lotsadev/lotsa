"""RED spec for ADR-044 Phase 5 — in-repo agents *and* workflows.

Phase 5 lets a project's own repo ship agents (``<repo>/.lotsa/agents/<name>/``)
and workflows (``<repo>/.lotsa/workflows/<name>/process.yaml``), joined by the
``lotsa:``/``repo:`` namespace model and the mandatory rails. Per the plan:

* **Decision 1 — project-scoped, build-time.** Repo ``.lotsa/`` is read from the
  project root at ``start()`` and repo workflows are built into a new
  ``self._project_processes`` map, dispatchable/listable only within their
  owning project.
* **Decision 2 — precedence ``override → lotsa: → repo:``.** Unqualified names
  resolve override-first, then bundled, then repo (repo lowest trust, can shadow
  neither). ``lotsa:``/``repo:`` qualifiers bind explicitly.
* **Decision 3 — rails.** Repo definitions may only reference *bundled* tools /
  hooks (the existing ``build_process`` validators are the structural rail); repo
  agent ``produces_changes`` / ``needs_worktree`` derive the same orchestrator-
  owned ``commit`` / ``worktree`` hooks.

Every new symbol / signature is imported or exercised inside a test so a missing
piece reds that behaviour independently. Each assertion is written to fail
against today's tree (no ``repo_agents_dir`` on the registry / ``build_process``,
no ``_project_processes`` on the orchestrator, no project arg on
``list_processes_summary``).
"""

from __future__ import annotations

import subprocess
from pathlib import Path

import pytest
import yaml

from lotsa.tests.conftest import wait_for_status  # noqa: F401 — available for dispatch-y tests
from lotsa.tests.test_adr029_multi_project import _build_and_start, _stop  # reuse ADR-029 harness

# ───────────────────────────────────────────────────────────────────────────
# fixture helpers
# ───────────────────────────────────────────────────────────────────────────


def _init_git_repo(path: Path) -> Path:
    """A real git repo with one commit (mirrors test_adr029_multi_project)."""
    path.mkdir(parents=True, exist_ok=True)
    subprocess.run(["git", "init", str(path)], capture_output=True, check=True)
    subprocess.run(["git", "-C", str(path), "config", "user.email", "test@test.com"], capture_output=True, check=True)
    subprocess.run(["git", "-C", str(path), "config", "user.name", "Test"], capture_output=True, check=True)
    (path / "README.md").write_text("# Test repo")
    subprocess.run(["git", "-C", str(path), "add", "."], capture_output=True, check=True)
    subprocess.run(["git", "-C", str(path), "commit", "-m", "init"], capture_output=True, check=True)
    return path


def _write_repo_agent(
    repo: Path,
    name: str,
    *,
    klass: str = "worker",
    outcomes=("COMPLETED", "INPUT"),
    needs_worktree: bool = True,
    produces_changes: bool = False,
    system_text: str | None = None,
) -> Path:
    agent_dir = repo / ".lotsa" / "agents" / name
    agent_dir.mkdir(parents=True, exist_ok=True)
    (agent_dir / "system.md").write_text(system_text if system_text is not None else f"repo system body for {name}\n")
    (agent_dir / "user.md").write_text(f"repo user body for {name}\n")
    (agent_dir / "agent.yaml").write_text(
        yaml.safe_dump(
            {
                "name": name,
                "class": klass,
                "outcomes": list(outcomes),
                "needs_worktree": needs_worktree,
                "produces_changes": produces_changes,
            }
        )
    )
    return agent_dir


def _write_repo_workflow(repo: Path, name: str, body: str) -> Path:
    wf_dir = repo / ".lotsa" / "workflows" / name
    wf_dir.mkdir(parents=True, exist_ok=True)
    proc = wf_dir / "process.yaml"
    proc.write_text(body)
    return proc


def _repo_agents_dir(repo: Path) -> Path:
    return repo / ".lotsa" / "agents"


# A minimal, buildable repo workflow that references a repo agent (``mycoder``,
# unqualified — resolves to repo since no bundled agent shadows it).
_MYFLOW_YAML = (
    "name: myflow\n"
    "description: A repo-shipped workflow for this project.\n"
    "jobs:\n"
    "  - name: work\n"
    "    type: agent\n"
    "    prompt: mycoder\n"
    "    queue_state: working\n"
    "    active_state: working\n"
)


def _alpha_beta_yaml(repo_a: Path, repo_b: Path) -> str:
    return yaml.dump(
        {
            "projects": {"alpha": {"path": str(repo_a)}, "beta": {"path": str(repo_b)}},
            "flow": "chat",
            "model": "sonnet",
            "budget": 5.0,
        }
    )


# ═══════════════════════════════════════════════════════════════════════════
# A. Namespace-aware AgentPromptRegistry (Decision 2)
# ═══════════════════════════════════════════════════════════════════════════


class TestNamespacedRegistry:
    """``AgentPromptRegistry`` gains a ``repo_agents_dir`` and the
    ``override → lotsa: → repo:`` resolution chain. Pre-Phase-5 the constructor
    takes only ``(override_dir, catalog_dir)`` — passing ``repo_agents_dir=``
    reds with ``TypeError``.
    """

    def test_registry_accepts_repo_agents_dir(self, tmp_path):
        from lotsa.agents import AGENTS_DIR
        from lotsa.flows import AgentPromptRegistry

        _write_repo_agent(tmp_path, "mycoder")
        # Must not raise — the constructor learns the third resolution root.
        AgentPromptRegistry(None, AGENTS_DIR, repo_agents_dir=_repo_agents_dir(tmp_path))

    def test_unqualified_name_falls_through_to_repo(self, tmp_path):
        """A repo-only agent (no bundled/override entry) resolves via the
        unqualified fall-through to the repo namespace."""
        from lotsa.agents import AGENTS_DIR
        from lotsa.flows import AgentPromptRegistry

        _write_repo_agent(tmp_path, "mycoder", system_text="REPO-MYCODER-SENTINEL\n")
        reg = AgentPromptRegistry(None, AGENTS_DIR, repo_agents_dir=_repo_agents_dir(tmp_path))
        assert "REPO-MYCODER-SENTINEL" in reg.load("mycoder-system")

    def test_repo_cannot_shadow_a_bundled_agent(self, tmp_path):
        """A repo agent named ``review`` must NOT shadow the bundled ``review``
        (repo is lowest trust). Unqualified resolution returns the bundled text.
        """
        from lotsa.agents import AGENTS_DIR
        from lotsa.flows import AgentPromptRegistry

        _write_repo_agent(
            tmp_path, "review", klass="gate", outcomes=("PASSED", "FAILED"), system_text="REPO-REVIEW-SENTINEL\n"
        )
        reg = AgentPromptRegistry(None, AGENTS_DIR, repo_agents_dir=_repo_agents_dir(tmp_path))
        resolved = reg.load("review-system")
        bundled = (AGENTS_DIR / "review" / "system.md").read_text()
        assert resolved == bundled
        assert "REPO-REVIEW-SENTINEL" not in resolved

    def test_repo_qualifier_binds_the_repo_namespace(self, tmp_path):
        """``repo:review`` binds the repo's review even though a bundled one exists."""
        from lotsa.agents import AGENTS_DIR
        from lotsa.flows import AgentPromptRegistry

        _write_repo_agent(
            tmp_path, "review", klass="gate", outcomes=("PASSED", "FAILED"), system_text="REPO-REVIEW-SENTINEL\n"
        )
        reg = AgentPromptRegistry(None, AGENTS_DIR, repo_agents_dir=_repo_agents_dir(tmp_path))
        assert "REPO-REVIEW-SENTINEL" in reg.load("repo:review-system")

    def test_lotsa_qualifier_never_hits_the_repo_namespace(self, tmp_path):
        """``lotsa:review`` binds the bundled review, ignoring any repo shadow."""
        from lotsa.agents import AGENTS_DIR
        from lotsa.flows import AgentPromptRegistry

        _write_repo_agent(
            tmp_path, "review", klass="gate", outcomes=("PASSED", "FAILED"), system_text="REPO-REVIEW-SENTINEL\n"
        )
        reg = AgentPromptRegistry(None, AGENTS_DIR, repo_agents_dir=_repo_agents_dir(tmp_path))
        resolved = reg.load("lotsa:review-system")
        assert resolved == (AGENTS_DIR / "review" / "system.md").read_text()
        assert "REPO-REVIEW-SENTINEL" not in resolved

    def test_operator_override_outranks_both_namespaces(self, tmp_path):
        """Decision 2: the operator ``--prompts-dir`` override is above both
        namespaces — it wins even for an explicit ``lotsa:`` reference."""
        from lotsa.agents import AGENTS_DIR
        from lotsa.flows import AgentPromptRegistry

        override = tmp_path / "override"
        (override / "review").mkdir(parents=True)
        (override / "review" / "system.md").write_text("OVERRIDE-REVIEW-SENTINEL\n")
        _write_repo_agent(
            tmp_path, "review", klass="gate", outcomes=("PASSED", "FAILED"), system_text="REPO-REVIEW-SENTINEL\n"
        )
        reg = AgentPromptRegistry(override, AGENTS_DIR, repo_agents_dir=_repo_agents_dir(tmp_path))
        assert "OVERRIDE-REVIEW-SENTINEL" in reg.load("lotsa:review-system")

    def test_load_agent_optional_reads_repo_agent_properties(self, tmp_path):
        """Property derivation must see a repo agent's declared properties (so a
        repo coder derives ``commit``, a repo chat opts out of ``worktree``)."""
        from lotsa.agents import AGENTS_DIR
        from lotsa.flows import AgentPromptRegistry

        _write_repo_agent(tmp_path, "mycoder", needs_worktree=True, produces_changes=True)
        reg = AgentPromptRegistry(None, AGENTS_DIR, repo_agents_dir=_repo_agents_dir(tmp_path))
        agent = reg.load_agent_optional("mycoder")
        assert agent is not None
        assert agent.produces_changes is True
        assert agent.needs_worktree is True

    def test_load_agent_optional_repo_qualifier(self, tmp_path):
        from lotsa.agents import AGENTS_DIR
        from lotsa.flows import AgentPromptRegistry

        _write_repo_agent(tmp_path, "mycoder", produces_changes=True)
        reg = AgentPromptRegistry(None, AGENTS_DIR, repo_agents_dir=_repo_agents_dir(tmp_path))
        agent = reg.load_agent_optional("repo:mycoder")
        assert agent is not None
        assert agent.produces_changes is True


# ═══════════════════════════════════════════════════════════════════════════
# B. build_process with repo definitions (Decision 3 rails + hook derivation)
# ═══════════════════════════════════════════════════════════════════════════


class TestBuildRepoWorkflow:
    """``build_process`` gains a ``repo_agents_dir`` so a repo ``process.yaml``
    can resolve ``repo:``/unqualified repo agents alongside bundled ones. Pre-
    Phase-5 there is no such parameter — every test reds with ``TypeError`` on
    the unexpected keyword.
    """

    def test_repo_workflow_resolves_repo_and_bundled_agents(self, tmp_path):
        from lotsa.flows import build_process

        _write_repo_agent(tmp_path, "mycoder", produces_changes=False)
        proc = _write_repo_workflow(
            tmp_path,
            "myrepoflow",
            "name: myrepoflow\n"
            "jobs:\n"
            "  - name: code\n"
            "    type: agent\n"
            "    prompt: repo:mycoder\n"
            "    queue_state: coding\n"
            "    active_state: coding\n"
            "  - name: review\n"
            "    type: agent\n"
            "    prompt: review\n"
            "    queue_state: reviewing\n"
            "    active_state: reviewing\n"
            "    routes: { PASSED: next, FAILED: code }\n",
        )
        process = build_process("myrepoflow", process_file=proc, repo_agents_dir=_repo_agents_dir(tmp_path))
        job_names = {j.name for j in process.jobs}
        assert {"code", "review"}.issubset(job_names)
        assert process.flows["main"].state_machine is not None

    def test_repo_agent_produces_changes_derives_commit_posthook(self, tmp_path):
        """A repo agent declaring ``produces_changes: true`` derives the built-in
        ``commit`` posthook at build time — same mechanism as a bundled agent."""
        from lotsa.flows import build_process

        _write_repo_agent(tmp_path, "mycoder", produces_changes=True)
        proc = _write_repo_workflow(tmp_path, "myrepoflow", _MYFLOW_YAML.replace("prompt: mycoder", "prompt: mycoder"))
        # myflow's single ``work`` step references the repo ``mycoder`` agent.
        proc.write_text(_MYFLOW_YAML)
        process = build_process("myflow", process_file=proc, repo_agents_dir=_repo_agents_dir(tmp_path))
        work = {j.name: j for j in process.flows["main"].jobs}["work"]
        assert "commit" in work.posthooks

    def test_repo_agent_needs_worktree_false_opts_out_of_worktree_prehook(self, tmp_path):
        from lotsa.flows import build_process

        _write_repo_agent(tmp_path, "noworktree", needs_worktree=False, produces_changes=False)
        proc = _write_repo_workflow(
            tmp_path,
            "wtflow",
            "name: wtflow\n"
            "jobs:\n"
            "  - name: work\n"
            "    type: agent\n"
            "    prompt: noworktree\n"
            "    queue_state: working\n"
            "    active_state: working\n",
        )
        process = build_process("wtflow", process_file=proc, repo_agents_dir=_repo_agents_dir(tmp_path))
        work = {j.name: j for j in process.flows["main"].jobs}["work"]
        assert "worktree" not in work.prehooks

    def test_repo_workflow_referencing_unknown_tool_fails_the_build(self, tmp_path):
        """Structural rail: a repo workflow can only wire *bundled* tools. An
        unknown ``tool:`` must fail the build via the existing registry validator.
        """
        from lotsa.flows import build_process

        proc = _write_repo_workflow(
            tmp_path,
            "evilflow",
            "name: evilflow\n"
            "jobs:\n"
            "  - name: sneak\n"
            "    type: action\n"
            "    tool: no_such_tool\n"
            "    queue_state: sneaking\n"
            "    active_state: sneaking\n",
        )
        with pytest.raises(ValueError):
            build_process("evilflow", process_file=proc, repo_agents_dir=_repo_agents_dir(tmp_path))


# ═══════════════════════════════════════════════════════════════════════════
# C. Orchestrator: per-project repo-workflow catalog (Decision 1)
# ═══════════════════════════════════════════════════════════════════════════


class TestPerProjectRepoCatalog:
    """At ``start()`` the orchestrator discovers each project's ``.lotsa/`` and
    builds repo workflows into ``self._project_processes`` (project-scoped),
    leaving the global ``self._processes`` bundled-only. Pre-Phase-5 there is no
    ``_project_processes`` attribute and ``.lotsa/`` is never read — these red via
    ``AttributeError`` / an unknown-process ``ProcessNotFound`` at create.
    """

    def _alpha_ships_myflow(self, tmp_path):
        repo_a = _init_git_repo(tmp_path / "repo_a")
        repo_b = _init_git_repo(tmp_path / "repo_b")
        _write_repo_agent(repo_a, "mycoder", produces_changes=False)
        _write_repo_workflow(repo_a, "myflow", _MYFLOW_YAML)
        return repo_a, repo_b

    def test_repo_workflow_is_built_into_the_project_catalog(self, tmp_path, run):
        repo_a, repo_b = self._alpha_ships_myflow(tmp_path)
        svc, db = _build_and_start(run, tmp_path / "data", _alpha_beta_yaml(repo_a, repo_b))
        try:
            assert "myflow" in svc._project_processes["alpha"]
            # It stays OUT of the global bundled catalog (repo is project-scoped).
            assert "myflow" not in svc._processes
        finally:
            _stop(run, svc, db)

    def test_repo_workflow_is_project_isolated(self, tmp_path, run):
        """A repo workflow is dispatchable in its owning project only. Creating a
        task with it in a *different* project is rejected."""
        from lotsa.orchestrator import ProcessNotFound

        repo_a, repo_b = self._alpha_ships_myflow(tmp_path)
        svc, db = _build_and_start(run, tmp_path / "data", _alpha_beta_yaml(repo_a, repo_b))
        try:
            # Accepted in alpha (the owning project) — reds today (unknown process).
            task = run(
                svc.create_task("uses repo flow", process_name="myflow", project_id="alpha", defer_dispatch=True)
            )
            row = run(db.get_task(task.id))
            assert row.metadata.get("process_name") == "myflow"
            # Rejected in beta (does not ship it).
            with pytest.raises(ProcessNotFound):
                run(svc.create_task("wrong project", process_name="myflow", project_id="beta", defer_dispatch=True))
        finally:
            _stop(run, svc, db)

    def test_task_resolves_repo_workflow_via_process_for(self, tmp_path, run):
        repo_a, repo_b = self._alpha_ships_myflow(tmp_path)
        svc, db = _build_and_start(run, tmp_path / "data", _alpha_beta_yaml(repo_a, repo_b))
        try:
            task = run(svc.create_task("repo flow", process_name="myflow", project_id="alpha", defer_dispatch=True))
            row = run(db.get_task(task.id))
            assert svc._process_for(row).name == "myflow"
        finally:
            _stop(run, svc, db)

    def test_repo_workflow_shadowing_a_bundled_name_is_skipped(self, tmp_path, run):
        """A repo workflow named ``build`` cannot shadow the bundled ``build``
        (Decision 2 at the workflow level): it is skipped, the bundled one stands.
        """
        repo_a = _init_git_repo(tmp_path / "repo_a")
        repo_b = _init_git_repo(tmp_path / "repo_b")
        _write_repo_agent(repo_a, "mycoder")
        _write_repo_workflow(repo_a, "build", _MYFLOW_YAML.replace("name: myflow", "name: build"))
        svc, db = _build_and_start(run, tmp_path / "data", _alpha_beta_yaml(repo_a, repo_b))
        try:
            # The bundled build is still the global one (a single ``plan`` first step).
            assert "build" in svc._processes
            assert "plan" in {s.name for s in svc._processes["build"].flows["main"].steps}
            # The shadowing repo workflow is not adopted into the project catalog.
            assert "build" not in svc._project_processes.get("alpha", {})
        finally:
            _stop(run, svc, db)

    def test_malformed_repo_workflow_is_skipped_failsoft(self, tmp_path, run):
        """One malformed repo workflow must not abort startup — it is logged and
        skipped, other processes still load (non-fatal, mirroring Phase 3's
        best-effort prehook philosophy)."""
        repo_a = _init_git_repo(tmp_path / "repo_a")
        repo_b = _init_git_repo(tmp_path / "repo_b")
        _write_repo_agent(repo_a, "mycoder")
        _write_repo_workflow(repo_a, "myflow", _MYFLOW_YAML)  # good
        _write_repo_workflow(  # broken: references an unknown bundled tool
            repo_a,
            "broken",
            "name: broken\n"
            "jobs:\n"
            "  - name: sneak\n"
            "    type: action\n"
            "    tool: no_such_tool\n"
            "    queue_state: sneaking\n"
            "    active_state: sneaking\n",
        )
        svc, db = _build_and_start(run, tmp_path / "data", _alpha_beta_yaml(repo_a, repo_b))
        try:
            catalog = svc._project_processes["alpha"]
            assert "myflow" in catalog  # the good one loaded
            assert "broken" not in catalog  # the bad one was skipped
            # Startup still succeeded — the bundled catalog is intact.
            assert {"chat", "build", "fix"}.issubset(set(svc._processes))
        finally:
            _stop(run, svc, db)

    def test_process_list_is_project_scoped(self, tmp_path, run):
        """``list_processes_summary(project_id=...)`` merges bundled + that
        project's repo workflows; another project doesn't see them. Pre-Phase-5
        the method takes no project argument, so this reds with ``TypeError``.
        """
        repo_a, repo_b = self._alpha_ships_myflow(tmp_path)
        svc, db = _build_and_start(run, tmp_path / "data", _alpha_beta_yaml(repo_a, repo_b))
        try:
            alpha_names = {s["name"] for s in svc.list_processes_summary(project_id="alpha")}
            beta_names = {s["name"] for s in svc.list_processes_summary(project_id="beta")}
            assert "myflow" in alpha_names
            assert "myflow" not in beta_names
            # Bundled processes remain available to both projects.
            assert {"chat", "build", "fix"}.issubset(alpha_names)
            assert {"chat", "build", "fix"}.issubset(beta_names)
        finally:
            _stop(run, svc, db)


# ═══════════════════════════════════════════════════════════════════════════
# D. ADR document records Phase 5 (provenance + namespaces)
# ═══════════════════════════════════════════════════════════════════════════


def test_adr_044_marks_phase_5_implemented():
    """Phase 5 lands the provenance mechanism, so the ADR's Phasing entry for it
    must be marked Implemented (plan Step 8). Today the Phase-5 bullet carries no
    such marker (Phases 5–6 are Proposed), so this reds until the doc is updated.
    """
    import re

    repo_root = Path(__file__).resolve().parents[2]
    adr = (repo_root / "docs" / "adr" / "ADR-044-workflows-for-agents.md").read_text()
    # Provenance vocabulary must be documented (already true — invariant).
    assert ".lotsa" in adr
    assert "repo:" in adr and "lotsa:" in adr
    # Load-bearing RED assertion: the Phase-5 phasing entry (from "5." up to the
    # "6." bullet) must be marked Implemented once the mechanism ships.
    region = re.search(r"5\.\s+\*\*In-repo agents.*?(?=\n\s*6\.)", adr, re.DOTALL)
    assert region is not None, "ADR-044 must keep a Phase 5 phasing entry"
    assert "Implemented" in region.group(0), "Phase 5 must be marked Implemented once it ships"
