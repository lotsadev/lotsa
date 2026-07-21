"""RED spec for ADR-044 Phase 6 (v1) — read-only workflow graph viewer.

Phase 6 exposes a workflow's *agent graph* so the dashboard can render it
read-only (the substrate for a later editor). The substance is backend:

* A source-agnostic serializer, ``lotsa.flows.serialize_process_graph``, that
  turns a *resolved* ``Process`` into per-flow nodes (jobs → resolved agent +
  ``class``/``outcomes``/hooks) and edges (the desugared ``routes:``/``rules:``
  targets, the implicit forward edge, and the ``blocked``/``needs_input``/
  ``complete`` terminal targets).
* Orchestrator service methods ``workflow_graph(name, project_id=...)`` and
  ``agent_detail(name, prompt_name, project_id=...)`` that resolve the process
  (project catalog → bundled), derive provenance (``source``: ``repo`` vs
  ``bundled``), and serialize.
* ``GET /api/workflows/{name}/graph`` and
  ``GET /api/workflows/{name}/agents/{prompt_name}`` routes (project-scoped),
  plus ``source``/``project`` provenance on the ``/api/processes`` summary.

Every new symbol / signature is imported or exercised *inside* a test so a
missing piece reds that behaviour independently. Each assertion is written to
fail against today's tree (no ``serialize_process_graph`` in ``flows.py``, no
``workflow_graph``/``agent_detail``/``WorkflowNotFound`` on the orchestrator,
no ``/api/workflows/...`` routes, no ``source`` on the process summary).
"""

from __future__ import annotations

import pytest
from httpx import ASGITransport, AsyncClient

from lotsa.tests.test_adr029_multi_project import _build_and_start, _stop

# Reuse the ADR-044 Phase-5 repo-provenance harness verbatim for the repo path.
from lotsa.tests.test_adr044_phase5_repo_provenance import (
    _MYFLOW_YAML,
    _alpha_beta_yaml,
    _init_git_repo,
    _write_repo_agent,
    _write_repo_workflow,
)

# ───────────────────────────────────────────────────────────────────────────
# graph-shape helpers (operate on the plain-dict serializer output)
# ───────────────────────────────────────────────────────────────────────────


def _flow(graph: dict, name: str) -> dict:
    return next(f for f in graph["flows"] if f["name"] == name)


def _node(flow: dict, node_id: str) -> dict | None:
    return next((n for n in flow["nodes"] if n["id"] == node_id), None)


def _edges_from(flow: dict, source: str) -> list[dict]:
    return [e for e in flow["edges"] if e["source"] == source]


def _edge(flow: dict, source: str, outcome: str) -> dict | None:
    return next(
        (e for e in flow["edges"] if e["source"] == source and e.get("outcome") == outcome),
        None,
    )


# ═══════════════════════════════════════════════════════════════════════════
# A. serialize_process_graph — the source-agnostic serializer (flows.py)
# ═══════════════════════════════════════════════════════════════════════════


class TestSerializeProcessGraph:
    """``serialize_process_graph(process)`` returns ``{"flows": [...]}`` where
    each flow carries ``nodes`` and ``edges``. Reds today — the symbol does not
    exist in ``lotsa.flows``.
    """

    def _build_graph(self) -> dict:
        from lotsa.flows import build_process, serialize_process_graph

        return serialize_process_graph(build_process("build"))

    def test_serializes_every_flow_of_the_process(self):
        graph = self._build_graph()
        names = {f["name"] for f in graph["flows"]}
        # build ships both the main pipeline and the pr_fix sub-flow.
        assert {"main", "pr_fix"} <= names

    def test_main_flow_has_a_node_per_step(self):
        main = _flow(self._build_graph(), "main")
        ids = {n["id"] for n in main["nodes"]}
        assert {
            "plan",
            "test",
            "code",
            "review",
            "verify",
            "pr_summary",
            "push_pr",
            "wait_for_pr_signal",
        } <= ids

    def test_worker_node_resolves_its_catalog_agent(self):
        main = _flow(self._build_graph(), "main")
        plan = _node(main, "plan")
        assert plan is not None
        assert plan["type"] == "agent"
        assert plan["agent"] is not None
        assert plan["agent"]["name"] == "planning"
        assert plan["agent"]["agent_class"] == "worker"
        assert plan["is_gate"] is False

    def test_gate_node_is_flagged_and_carries_outcomes(self):
        main = _flow(self._build_graph(), "main")
        review = _node(main, "review")
        assert review is not None
        assert review["agent"]["agent_class"] == "gate"
        assert review["is_gate"] is True
        assert set(review["agent"]["outcomes"]) >= {"PASSED", "FAILED"}

    def test_action_node_has_no_agent(self):
        main = _flow(self._build_graph(), "main")
        push = _node(main, "push_pr")
        assert push is not None
        assert push["type"] == "action"
        assert push["agent"] is None

    def test_monitor_node_type(self):
        main = _flow(self._build_graph(), "main")
        wait = _node(main, "wait_for_pr_signal")
        assert wait is not None
        assert wait["type"] == "monitor"

    def test_explicit_route_edges_resolve_next_and_sibling_targets(self):
        main = _flow(self._build_graph(), "main")
        # review PASSED → next (== verify, the following step); FAILED → code.
        assert _edge(main, "review", "PASSED")["target"] == "verify"
        assert _edge(main, "review", "FAILED")["target"] == "code"

    def test_implicit_forward_edge_for_a_ruleless_worker(self):
        """``code`` declares no rules — it advances to the next step on
        ``COMPLETED``. The graph must show that implicit forward edge so the
        happy path is not invisible."""
        main = _flow(self._build_graph(), "main")
        fwd = next((e for e in _edges_from(main, "code") if e["target"] == "review"), None)
        assert fwd is not None
        assert fwd["kind"] == "implicit"
        assert fwd["outcome"] == "COMPLETED"

    def test_per_flow_routing_differs_between_main_and_pr_fix(self):
        """The whole "routing lives on the edge" thesis: ``review`` routes
        ``FAILED → code`` in ``main`` but ``FAILED → pr-fix`` in ``pr_fix``.
        Proves the serializer resolves per-binding effective rules, not the
        job-level defaults."""
        graph = self._build_graph()
        assert _edge(_flow(graph, "main"), "review", "FAILED")["target"] == "code"
        assert _edge(_flow(graph, "pr_fix"), "review", "FAILED")["target"] == "pr-fix"

    def test_terminal_targets_are_materialized_as_nodes(self):
        """pr-fix routes ``INPUT → needs_input`` and ``FAILED → blocked``. Both
        terminal targets must exist as nodes so the canvas can draw the edge to
        them (they are not sibling steps)."""
        pr_fix = _flow(self._build_graph(), "pr_fix")
        assert _edge(pr_fix, "pr-fix", "INPUT")["target"] == "needs_input"
        assert _edge(pr_fix, "pr-fix", "FAILED")["target"] == "blocked"
        assert _node(pr_fix, "needs_input") is not None
        assert _node(pr_fix, "blocked") is not None

    def test_back_edge_to_a_sibling_does_not_crash(self):
        """``resolve_conflicts COMPLETED → pr-fix`` is a cycle; the serializer
        must emit it as an ordinary sibling edge."""
        pr_fix = _flow(self._build_graph(), "pr_fix")
        assert _edge(pr_fix, "resolve_conflicts", "COMPLETED")["target"] == "pr-fix"


# ═══════════════════════════════════════════════════════════════════════════
# B. Orchestrator service methods — workflow_graph / agent_detail / provenance
# ═══════════════════════════════════════════════════════════════════════════


class TestWorkflowGraphService:
    """``OrchestratorService.workflow_graph`` / ``agent_detail`` and the
    ``source`` provenance field on ``list_processes_summary``. Reds today —
    none of these exist."""

    def test_workflow_graph_returns_bundled_provenance(self, app_with_service):
        _app, service = app_with_service
        graph = service.workflow_graph("build")
        assert graph["name"] == "build"
        assert graph["source"] == "bundled"
        # A bundled workflow is not project-owned.
        assert graph.get("project") is None
        assert {f["name"] for f in graph["flows"]} >= {"main", "pr_fix"}

    def test_unknown_workflow_raises_workflow_not_found(self, app_with_service):
        from lotsa.orchestrator import WorkflowNotFound

        _app, service = app_with_service
        with pytest.raises(WorkflowNotFound):
            service.workflow_graph("does-not-exist")

    def test_agent_detail_returns_properties_and_prompt_text(self, app_with_service):
        _app, service = app_with_service
        detail = service.agent_detail("build", "planning")
        assert detail["name"] == "planning"
        assert detail["agent_class"] == "worker"
        assert set(detail["outcomes"]) >= {"COMPLETED"}
        # Prompt bodies are resolved through the process registry (source-
        # agnostic) so the inspector can render them.
        assert detail["system_prompt"]
        assert detail["user_prompt"] is not None

    def test_list_processes_summary_tags_bundled_source(self, app_with_service):
        _app, service = app_with_service
        summaries = service.list_processes_summary()
        assert summaries, "expected at least the bundled catalog"
        by_name = {s["name"]: s for s in summaries}
        assert by_name["build"]["source"] == "bundled"
        # Every entry carries the new provenance field.
        assert all("source" in s for s in summaries)


# ═══════════════════════════════════════════════════════════════════════════
# C. HTTP routes — /api/workflows/{name}/graph and /agents/{prompt_name}
# ═══════════════════════════════════════════════════════════════════════════


class TestWorkflowGraphAPI:
    def test_graph_endpoint_serves_a_bundled_workflow(self, app_with_service, run):
        app, _ = app_with_service

        async def _test():
            transport = ASGITransport(app=app)
            async with AsyncClient(transport=transport, base_url="http://test") as client:
                resp = await client.get("/api/workflows/build/graph")
                assert resp.status_code == 200
                data = resp.json()
                assert data["name"] == "build"
                assert data["source"] == "bundled"
                flow_names = {f["name"] for f in data["flows"]}
                assert {"main", "pr_fix"} <= flow_names
                main = next(f for f in data["flows"] if f["name"] == "main")
                assert any(n["id"] == "review" for n in main["nodes"])
                assert any(e["source"] == "review" for e in main["edges"])

        run(_test())

    def test_agent_detail_endpoint(self, app_with_service, run):
        app, _ = app_with_service

        async def _test():
            transport = ASGITransport(app=app)
            async with AsyncClient(transport=transport, base_url="http://test") as client:
                resp = await client.get("/api/workflows/build/agents/planning")
                assert resp.status_code == 200
                data = resp.json()
                assert data["agent_class"] == "worker"
                assert data["system_prompt"]

        run(_test())

    def test_unknown_workflow_graph_is_404(self, app_with_service, run):
        app, _ = app_with_service

        async def _test():
            transport = ASGITransport(app=app)
            async with AsyncClient(transport=transport, base_url="http://test") as client:
                resp = await client.get("/api/workflows/nope/graph")
                assert resp.status_code == 404
                assert resp.json()["detail"]["code"] == "WORKFLOW_NOT_FOUND"

        run(_test())

    def test_processes_endpoint_carries_source(self, app_with_service, run):
        app, _ = app_with_service

        async def _test():
            transport = ASGITransport(app=app)
            async with AsyncClient(transport=transport, base_url="http://test") as client:
                resp = await client.get("/api/processes")
                assert resp.status_code == 200
                data = resp.json()
                assert all("source" in p for p in data)
                build = next(p for p in data if p["name"] == "build")
                assert build["source"] == "bundled"

        run(_test())


# ═══════════════════════════════════════════════════════════════════════════
# D. Repo-shipped workflow — provenance == "repo", project-scoped (Phase 5 tie-in)
# ═══════════════════════════════════════════════════════════════════════════


class TestRepoWorkflowProvenance:
    """A repo-shipped workflow renders through the same serializer, tagged
    ``source: "repo"`` with the owning project's identity, and is project-
    isolated. Reuses the Phase-5 repo harness."""

    def _alpha_ships_myflow(self, tmp_path):
        repo_a = _init_git_repo(tmp_path / "repo_a")
        repo_b = _init_git_repo(tmp_path / "repo_b")
        _write_repo_agent(repo_a, "mycoder", produces_changes=False)
        _write_repo_workflow(repo_a, "myflow", _MYFLOW_YAML)
        return repo_a, repo_b

    def test_repo_workflow_graph_is_tagged_repo(self, tmp_path, run):
        repo_a, repo_b = self._alpha_ships_myflow(tmp_path)
        svc, db = _build_and_start(run, tmp_path / "data", _alpha_beta_yaml(repo_a, repo_b))
        try:
            graph = svc.workflow_graph("myflow", project_id="alpha")
            assert graph["source"] == "repo"
            assert graph["project"] == "alpha"
            # The repo agent resolves in the node payload.
            main = _flow(graph, "main")
            work = _node(main, "work")
            assert work is not None
            assert work["agent"]["name"] == "mycoder"
        finally:
            _stop(run, svc, db)

    def test_repo_workflow_is_project_isolated_in_the_graph(self, tmp_path, run):
        from lotsa.orchestrator import WorkflowNotFound

        repo_a, repo_b = self._alpha_ships_myflow(tmp_path)
        svc, db = _build_and_start(run, tmp_path / "data", _alpha_beta_yaml(repo_a, repo_b))
        try:
            # myflow belongs to alpha; beta must not resolve it.
            with pytest.raises(WorkflowNotFound):
                svc.workflow_graph("myflow", project_id="beta")
        finally:
            _stop(run, svc, db)

    def test_project_scoped_summary_tags_repo_source(self, tmp_path, run):
        repo_a, repo_b = self._alpha_ships_myflow(tmp_path)
        svc, db = _build_and_start(run, tmp_path / "data", _alpha_beta_yaml(repo_a, repo_b))
        try:
            by_name = {s["name"]: s for s in svc.list_processes_summary(project_id="alpha")}
            assert by_name["myflow"]["source"] == "repo"
            assert by_name["myflow"]["project"] == "alpha"
            # Bundled entries stay tagged bundled even under project scoping.
            assert by_name["build"]["source"] == "bundled"
        finally:
            _stop(run, svc, db)
