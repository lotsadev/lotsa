"""Failing tests for the ADR-044 Phase 3 ``needs_worktree`` prehook.

Phase 3 moves worktree creation off an unconditional dispatch-time side effect
and onto a **property-derived prehook**, structurally mirroring Phase 2's
``produces_changes → commit`` posthook derivation. Every agent in the catalog
already declares ``needs_worktree``; only ``chat`` declares ``false``. The
payoff: chat tasks stop creating a git worktree they never use.

Five concerns, five sections (mirrors ``test_posthooks.py``):

1. **Registry surface** — ``lotsa.registry`` grows a prehook registry
   symmetric with the posthook one (``register_prehook`` / ``get_prehook`` /
   ``is_prehook_registered`` / ``list_prehooks``), covered by
   ``snapshot``/``restore``.
2. **TaskContext** — carries an optional ``worktree_manager`` so the built-in
   ``worktree`` prehook (which *creates* the worktree) can reach the manager.
3. **Built-in ``worktree`` prehook** — ``lotsa.prehooks`` registers a
   ``worktree`` prehook that invokes the injected ``WorktreeManager``.
4. **Flow model** — ``flows.py`` parses a per-step ``prehooks`` field, derives
   ``worktree`` from the agent's ``needs_worktree`` property (opt-OUT: worktree
   is the universal default; only a ``needs_worktree: false`` agent opts out),
   preserves the binding override seam, and validates references + property
   consistency at build time.
5. **Orchestrator** — a dispatched ``chat`` task creates no worktree; a
   ``needs_worktree`` agent still does.

None of this exists yet; every test is expected to fail (``AttributeError`` /
``ImportError`` / ``ModuleNotFoundError`` / a missing-``pytest.raises`` /
assertion) until Phase 3 lands.

NOTE for the implementation step: ``conftest.py``'s ``_isolated_registry``
autouse fixture must add ``import lotsa.prehooks  # noqa: F401`` to its baseline
(mirroring the existing ``import lotsa.posthooks`` line) so the built-in
``worktree`` prehook is part of the snapshot and ``restore()`` doesn't strip it
between tests — exactly as the posthook baseline import guarantees today. That
line can only be added once ``lotsa/prehooks`` exists (adding it now would break
the whole suite at collection time), so it is the coder's, not the tester's.
"""

from __future__ import annotations

from pathlib import Path

import pytest

# ===========================================================================
# 1. Registry surface — prehook registry symmetric with the posthook one
# ===========================================================================


def test_registry_exports_prehook_api():
    from lotsa import registry

    assert hasattr(registry, "register_prehook")
    assert hasattr(registry, "get_prehook")
    assert hasattr(registry, "is_prehook_registered")
    assert hasattr(registry, "list_prehooks")


def test_register_prehook_makes_it_retrievable():
    from lotsa.registry import get_prehook, register_prehook

    async def my_hook(ctx, config):
        from lotsa.tools import ToolResult

        return ToolResult(success=True, output="ok")

    register_prehook("my_prehook", my_hook)
    assert get_prehook("my_prehook") is my_hook


def test_register_prehook_rejects_name_collision():
    from lotsa.registry import register_prehook

    async def h1(ctx, config): ...

    async def h2(ctx, config): ...

    register_prehook("dup_prehook", h1)
    with pytest.raises(ValueError, match="dup_prehook"):
        register_prehook("dup_prehook", h2)


def test_get_prehook_unknown_name_raises_with_registered_list():
    from lotsa.registry import get_prehook, register_prehook

    async def known(ctx, config): ...

    register_prehook("known_prehook", known)

    with pytest.raises(KeyError) as exc_info:
        get_prehook("nope_prehook")
    msg = str(exc_info.value)
    assert "nope_prehook" in msg
    assert "known_prehook" in msg


def test_builtin_worktree_prehook_registered_on_import():
    """Importing ``lotsa.prehooks`` registers the built-in ``worktree`` prehook."""
    import lotsa.prehooks  # noqa: F401 — import side effect registers built-ins
    from lotsa.registry import get_prehook

    fn = get_prehook("worktree")
    assert callable(fn)


def test_snapshot_restore_covers_prehooks():
    """``snapshot``/``restore`` round-trips the prehook registry too.

    A prehook registered after a snapshot must be dropped by restore, while
    the built-in baseline (``worktree``) survives — the same isolation contract
    the posthook registry already has.
    """
    import lotsa.prehooks  # noqa: F401 — ensures the built-in baseline exists
    from lotsa import registry as reg

    snap = reg.snapshot()

    async def temp_prehook(ctx, config): ...

    reg.register_prehook("temp_prehook", temp_prehook)
    assert reg.is_prehook_registered("temp_prehook")

    reg.restore(snap)
    assert not reg.is_prehook_registered("temp_prehook")
    assert reg.is_prehook_registered("worktree")


def test_old_snapshot_without_prehooks_key_restores_cleanly():
    """A snapshot captured before Phase 3 (no ``prehooks`` key) restores without
    raising — mirrors the posthook back-compat guard in ``restore()``."""
    import lotsa.prehooks  # noqa: F401
    from lotsa import registry as reg

    legacy_snap = {"tools": {}, "engines": {}, "posthooks": {}}  # no "prehooks" key
    reg.restore(legacy_snap)  # must not KeyError


# ===========================================================================
# 2. TaskContext carries the worktree manager (so a prehook can CREATE one)
# ===========================================================================


def _plain_ctx_kwargs():
    return dict(
        task_id="t",
        worktree=Path("/tmp/fallback"),
        metadata={},
        db=None,
        process_name="build",
        flow_name="main",
        current_flow="main",
        last_run_step="plan",
    )


def test_task_context_worktree_manager_defaults_to_none():
    from lotsa.tools import TaskContext

    ctx = TaskContext(**_plain_ctx_kwargs())
    assert ctx.worktree_manager is None


def test_task_context_accepts_worktree_manager():
    from lotsa.tools import TaskContext

    sentinel = object()
    ctx = TaskContext(**_plain_ctx_kwargs(), worktree_manager=sentinel)
    assert ctx.worktree_manager is sentinel


# ===========================================================================
# 3. Built-in ``worktree`` prehook — invokes the injected WorktreeManager
# ===========================================================================


class _FakeWTM:
    """Minimal WorktreeManager stand-in for prehook unit tests."""

    def __init__(self, *, path: Path | None = None, boom: bool = False):
        self._path = path or Path("/tmp/wt/task-1")
        self._boom = boom
        self.created: list[str] = []

    async def create(self, task_id: str, base_ref: str | None = None) -> Path:
        if self._boom:
            raise RuntimeError("git worktree add failed")
        self.created.append(task_id)
        return self._path


def _prehook_ctx(worktree_manager, *, task_id: str = "task-1"):
    from lotsa.tools import TaskContext

    return TaskContext(
        task_id=task_id,
        worktree=Path("/tmp/fallback"),
        metadata={},
        db=None,
        process_name="build",
        flow_name="main",
        current_flow="main",
        last_run_step="plan",
        worktree_manager=worktree_manager,
    )


async def test_worktree_prehook_creates_via_manager():
    from lotsa.prehooks import worktree_prehook

    wtm = _FakeWTM(path=Path("/tmp/wt/abc"))
    ctx = _prehook_ctx(wtm, task_id="abc")
    result = await worktree_prehook(ctx, {})

    assert result.success is True
    assert wtm.created == ["abc"], "the prehook must create the task's worktree"
    # The created path rides along in metadata (informational).
    assert result.metadata.get("worktree") == "/tmp/wt/abc"


async def test_worktree_prehook_without_manager_returns_unsuccessful():
    """No manager injected → the prehook reports failure rather than crashing;
    the orchestrator degrades to the project work_dir."""
    from lotsa.prehooks import worktree_prehook

    ctx = _prehook_ctx(None)
    result = await worktree_prehook(ctx, {})

    assert result.success is False
    assert result.output  # a non-empty, operator-safe message


async def test_worktree_prehook_create_failure_returns_unsuccessful():
    """A ``WorktreeManager.create`` failure surfaces as ``success=False`` (the
    orchestrator falls back to the project work_dir — non-fatal)."""
    from lotsa.prehooks import worktree_prehook

    wtm = _FakeWTM(boom=True)
    ctx = _prehook_ctx(wtm)
    result = await worktree_prehook(ctx, {})

    assert result.success is False
    assert result.output


# ===========================================================================
# 4. Flow model — per-step prehooks parsing / derivation / override / guards
# ===========================================================================


def _build(tmp_path: Path, yaml_text: str):
    """Write a process.yaml and build it (no prompt files needed at build)."""
    from lotsa.flows import build_process

    path = tmp_path / "process.yaml"
    path.write_text(yaml_text)
    return build_process("custom", process_file=path)


def _job(process, name: str):
    return next(rj for rj in process.flows["main"].jobs if rj.name == name)


def test_explicit_worktree_prehook_parsed_and_dedups(tmp_path: Path):
    """An explicit ``prehooks: [worktree]`` on a ``needs_worktree: true`` agent
    (``coding``) is read AND deduped against the derived worktree — a single
    ``worktree``, not two."""
    import lotsa.prehooks  # noqa: F401 — the built-in ``worktree`` must be registered

    process = _build(
        tmp_path,
        """
process: prehook_parse
jobs:
  - name: code
    type: agent
    prompt: coding
    queue_state: coding
    active_state: coding
    prehooks: [worktree]
flows:
  main:
    steps:
      - code
""",
    )
    assert _job(process, "code").prehooks == ["worktree"]


def test_chat_agent_opts_out_of_worktree_derivation(tmp_path: Path):
    """The sole opt-out: a ``needs_worktree: false`` agent (``chat``) derives NO
    worktree prehook.

    RED pre-Phase-3: ``ResolvedJob`` has no ``prehooks`` attribute, so the
    access raises ``AttributeError``; more fundamentally, worktree creation is
    unconditional today, so nothing gates chat out.
    """
    process = _build(
        tmp_path,
        """
process: chat_optout
jobs:
  - name: talk
    type: agent
    prompt: chat
    conversational: true
    queue_state: chat
    active_state: chat
flows:
  main:
    steps:
      - talk
""",
    )
    assert _job(process, "talk").prehooks == []


def test_needs_worktree_agent_derives_worktree(tmp_path: Path):
    """A ``needs_worktree: true`` agent (``coding``) folds ``worktree`` into its
    step's prehooks even though the YAML declares none."""
    import lotsa.prehooks  # noqa: F401

    process = _build(
        tmp_path,
        """
process: derive_worktree
jobs:
  - name: code
    type: agent
    prompt: coding
    queue_state: coding
    active_state: coding
flows:
  main:
    steps:
      - code
""",
    )
    assert _job(process, "code").prehooks == ["worktree"]


def test_action_job_derives_worktree_by_default(tmp_path: Path):
    """An ``action`` step (``push_pr``) still gets a worktree by default — the
    derivation is opt-OUT, so non-agent steps (which have no ``agent.yaml``)
    keep today's always-create behaviour. Deriving opt-in would strip push_pr's
    worktree (a regression)."""
    import lotsa.prehooks  # noqa: F401

    process = _build(
        tmp_path,
        """
process: action_worktree
jobs:
  - name: code
    type: agent
    prompt: coding
    queue_state: coding
    active_state: coding
  - name: pushit
    type: action
    tool: push_pr
flows:
  main:
    steps:
      - code
      - pushit
""",
    )
    assert _job(process, "pushit").prehooks == ["worktree"]


def test_monitor_job_derives_no_worktree(tmp_path: Path):
    """A ``monitor`` step never created a worktree at dispatch (it early-returns
    before the create site), so it derives none."""
    import lotsa.prehooks  # noqa: F401

    process = _build(
        tmp_path,
        """
process: monitor_no_worktree
jobs:
  - name: gate
    type: agent
    prompt: review
    queue_state: reviewing
    active_state: reviewing
  - name: wait
    type: monitor
    engine: pr_monitor
    config:
      poll_interval_seconds: 30
      debounce_seconds: 120
flows:
  main:
    steps:
      - gate
      - wait
""",
    )
    assert _job(process, "wait").prehooks == []


def test_inline_prompt_without_agent_yaml_defaults_to_worktree(tmp_path: Path):
    """An agent step whose prompt has no ``agent.yaml`` (inline / custom) resolves
    to ``None`` and keeps the safe default — a worktree."""
    import lotsa.prehooks  # noqa: F401

    process = _build(
        tmp_path,
        """
process: inline_default_worktree
jobs:
  - name: work
    type: agent
    prompt: no_such_catalog_agent_xyz
    queue_state: working_state
    active_state: working_state
flows:
  main:
    steps:
      - work
""",
    )
    assert _job(process, "work").prehooks == ["worktree"]


def test_per_binding_empty_list_suppresses_derived_worktree(tmp_path: Path):
    """The override seam holds against derivation: a binding ``prehooks: []``
    fully overrides the step, suppressing even the derived worktree."""
    import lotsa.prehooks  # noqa: F401

    process = _build(
        tmp_path,
        """
process: suppress_worktree
jobs:
  - name: code
    type: agent
    prompt: coding
    queue_state: coding
    active_state: coding
flows:
  main:
    steps:
      - name: code
        prehooks: []
""",
    )
    assert _job(process, "code").prehooks == []


def test_binding_override_fully_replaces_derived_worktree(tmp_path: Path):
    """A binding ``prehooks:`` override fully replaces the base — the derived
    worktree is NOT folded into a binding override. A ``needs_worktree`` agent
    whose binding lists only ``[record_pre]`` runs exactly that, no worktree."""
    from lotsa.registry import register_prehook
    from lotsa.tools import ToolResult

    async def record_pre(ctx, config):
        return ToolResult(success=True, output="ok")

    register_prehook("record_pre", record_pre)  # _isolated_registry restores

    process = _build(
        tmp_path,
        """
process: binding_replace_worktree
jobs:
  - name: code
    type: agent
    prompt: coding
    queue_state: coding
    active_state: coding
flows:
  main:
    steps:
      - name: code
        prehooks: [record_pre]
""",
    )
    assert _job(process, "code").prehooks == ["record_pre"]


def test_unknown_prehook_name_fails_at_build_time(tmp_path: Path):
    """An unregistered prehook name in YAML fails fast in ``build_process`` —
    mirrors the posthook-reference validator."""
    import lotsa.prehooks  # noqa: F401

    with pytest.raises(ValueError, match="does_not_exist"):
        _build(
            tmp_path,
            """
process: bad_prehook
jobs:
  - name: code
    type: agent
    prompt: coding
    queue_state: coding
    active_state: coding
    prehooks: [does_not_exist]
flows:
  main:
    steps:
      - code
""",
        )


# ---------------------------------------------------------------------------
# 4b. Consistency guard — an EXPLICIT worktree on a ``needs_worktree: false``
#     agent is drift (worktree is derived), so it fails loud at build time.
# ---------------------------------------------------------------------------


def test_explicit_worktree_on_optout_agent_fails_at_build_job_level(tmp_path: Path):
    """A job-level ``prehooks: [worktree]`` on a ``needs_worktree: false`` agent
    (``chat``) is a build error — the exact drift the property-derivation removes.

    RED pre-Phase-3: no guard exists, so the process builds cleanly (the
    ``pytest.raises`` does not fire)."""
    import lotsa.prehooks  # noqa: F401 — ``worktree`` must be registered so the
    # reference validator passes and we reach the consistency guard.

    with pytest.raises(ValueError, match="needs_worktree"):
        _build(
            tmp_path,
            """
process: guard_job_level
jobs:
  - name: talk
    type: agent
    prompt: chat
    conversational: true
    queue_state: chat
    active_state: chat
    prehooks: [worktree]
flows:
  main:
    steps:
      - talk
""",
        )


def test_explicit_worktree_on_optout_agent_fails_at_build_binding_level(tmp_path: Path):
    """A binding-level ``prehooks: [worktree]`` on a ``needs_worktree: false``
    agent trips the same guard — the check covers binding overrides, not just
    job defaults."""
    import lotsa.prehooks  # noqa: F401

    with pytest.raises(ValueError, match="needs_worktree"):
        _build(
            tmp_path,
            """
process: guard_binding_level
jobs:
  - name: talk
    type: agent
    prompt: chat
    conversational: true
    queue_state: chat
    active_state: chat
flows:
  main:
    steps:
      - name: talk
        prehooks: [worktree]
""",
        )


def test_explicit_worktree_on_needs_worktree_agent_does_not_trip_guard(tmp_path: Path):
    """A ``needs_worktree: true`` agent explicitly listing ``worktree`` is
    redundant-but-consistent (not drift) — it must build cleanly and resolve to
    a single ``worktree``."""
    import lotsa.prehooks  # noqa: F401

    process = _build(
        tmp_path,
        """
process: guard_ok
jobs:
  - name: code
    type: agent
    prompt: coding
    queue_state: coding
    active_state: coding
    prehooks: [worktree]
flows:
  main:
    steps:
      - code
""",
    )
    assert _job(process, "code").prehooks == ["worktree"]


def test_bundled_processes_are_prehook_property_consistent():
    """The bundled ``build``/``fix`` processes build without tripping the
    prehook consistency guard or reference validator."""
    from lotsa.flows import build_process

    # Building is the assertion: a guard/reference violation would raise here.
    build_process("build")
    build_process("fix")


# ===========================================================================
# 5. Orchestrator — chat dispatches with no worktree; needs_worktree steps
#    still create one. (behavioural acceptance)
# ===========================================================================


class _RecordingRunner:
    """FakeRunner that records each dispatch (so we can prove the agent ran)."""

    def __init__(self):
        from rigg.models import AgentResult

        self.result = AgentResult(success=True, stdout="Agent output", stderr="", return_code=0, duration_ms=1)
        self.calls: list[dict] = []

    def dispatch_shape_prompt(self) -> str:
        return ""

    async def run(self, system_prompt, user_prompt, work_dir, **kwargs):
        self.calls.append({"work_dir": work_dir})
        return self.result


@pytest.fixture()
def prehook_service(tmp_path, run):
    """A started OrchestratorService whose active ``custom`` process is a single
    ``needs_worktree`` agent step (the ``coding`` catalog agent). ``start()``
    also loads the bundled catalog, so ``chat`` is dispatchable by name.

    The binding ``posthooks: []`` suppresses the derived ``commit`` (the
    work_dir is not a git repo); ``evaluate: true`` parks the task at a gate
    after the agent runs, so no auto-advance noise.
    """
    from lotsa.config import LotsaConfig
    from lotsa.db import TaskDB
    from lotsa.orchestrator import OrchestratorService

    data_dir = tmp_path / "data"
    data_dir.mkdir()
    flow_yaml = tmp_path / "flow.yaml"
    flow_yaml.write_text(
        "name: custom\njobs:\n  - name: coding\n    evaluate: true\n"
        "flows:\n  main:\n    steps:\n      - name: coding\n        posthooks: []\n"
    )
    config = LotsaConfig(
        data_dir=data_dir,
        work_dir=tmp_path,
        flow="custom",
        flow_file=flow_yaml,
        model="sonnet",
        budget=5.0,
    )
    db = TaskDB(data_dir / "lotsa.db")
    run(db.initialize())
    svc = OrchestratorService(config, db)
    svc.runner = _RecordingRunner()
    run(svc.start())
    yield svc
    run(svc.shutdown())
    run(db.close())


def _record_worktree_creates(monkeypatch) -> list[str]:
    """Patch ``WorktreeManager.create`` (class-level) to record task ids without
    touching git. Returns the list the recorder appends to."""
    from rigg.git import WorktreeManager

    created: list[str] = []

    async def rec_create(self, task_id, base_ref=None):
        created.append(task_id)
        return self.dir / task_id

    monkeypatch.setattr(WorktreeManager, "create", rec_create)
    return created


def test_chat_task_dispatches_without_creating_a_worktree(prehook_service, run, monkeypatch):
    """A dispatched ``chat`` task runs its agent but creates NO worktree.

    ``create_task`` dispatches the first step synchronously, and the create
    call (if any) happens before the agent is spawned — so the recorder is
    authoritative the moment ``create_task`` returns.

    RED pre-Phase-3: worktree creation is unconditional at dispatch, so the
    chat task's id lands in ``created`` and the ``not in`` assertion fails.
    """
    svc = prehook_service
    created = _record_worktree_creates(monkeypatch)

    before = len(svc.runner.calls)
    task = run(svc.create_task("Talk it through", message="let's discuss", process_name="chat"))

    # The chat agent DID dispatch (guards against a false-green where chat simply
    # never ran and therefore trivially created no worktree)...
    assert len(svc.runner.calls) > before, "chat step must have dispatched its agent"
    # ...yet no worktree was created for it — Phase 3's whole payoff.
    assert task.id not in created


def test_needs_worktree_agent_still_creates_a_worktree(prehook_service, run, monkeypatch):
    """Positive control / regression guard: a ``needs_worktree: true`` agent
    (the active ``custom`` process's ``coding`` step) still creates its worktree.
    Guards against an over-broad opt-out that strips every step's worktree."""
    svc = prehook_service
    created = _record_worktree_creates(monkeypatch)

    task = run(svc.create_task("Do the code", message="change a thing"))
    assert task.id in created


def test_run_step_prehooks_degrades_when_project_unresolvable(prehook_service, run, monkeypatch):
    """Regression (review): ``_run_step_prehooks`` must never let a
    ``ProjectNotFound`` escape when the task's WorktreeManager can't be resolved.

    ``_worktree_manager_for_task`` raises ``ProjectNotFound`` for an
    ADR-029 edge case — a legacy row or a task whose project was removed from
    ``lotsa.yaml``. This resolution now sits INSIDE the method's resilience:
    like the pre-Phase-3 code (which wrapped both the manager lookup and
    ``.create()`` in one ``try/except``), an unresolvable project must degrade
    to the project work_dir, not propagate. It matters because
    ``_run_step_prehooks`` runs AFTER ``_dispatch_step``'s CAS to ``working``
    has committed — a propagating exception would strand the task in
    ``working`` with no in-flight agent until the next restart's resume sweep.

    RED against the pre-fix code, where ``wtm = self._worktree_manager_for_task(item)``
    sat OUTSIDE the ``try`` block: this call raises ``ProjectNotFound`` instead
    of returning the fallback work_dir.
    """
    from lotsa.orchestrator import ProjectNotFound
    from rigg.models import Item

    svc = prehook_service
    item = Item(id="orphan-task", state="backlog", title="t", body="b", metadata={})
    step = next(s for s in svc._root_flow_for(item).jobs if s.name == "coding")
    # Precondition: the ``coding`` step derives the ``worktree`` prehook, so the
    # method actually reaches the manager resolution under test.
    assert "worktree" in step.prehooks

    def boom(_item):
        raise ProjectNotFound("task references unknown project")

    monkeypatch.setattr(svc, "_worktree_manager_for_task", boom)

    # Must NOT raise — degrades to the project work_dir.
    work_dir = run(svc._run_step_prehooks(item, step))
    assert work_dir == svc._fallback_work_dir(item)
