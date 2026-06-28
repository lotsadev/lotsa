"""Test spec for ADR-029 — multi-project support.

These tests were written as the "red" specification for ADR-029 — a failing
spec authored before the implementation landed. The implementation has since
shipped, so they now pass; they remain the behavioural spec for the new
surfaces (each was written to fail against the pre-ADR-029 tree, where the
symbols/fields/behaviours below did not yet exist):

* ``lotsa.config`` — a ``projects:`` field, ``ProjectSpec`` + ``resolve_project_specs``
  (id slug validation, path normalization, exists/is-git checks, ``work_dir``
  seeding + precedence, unknown-key / deprecation warnings).
* ``lotsa.db`` — ``ProjectRow``, ``upsert_project`` / ``get_project`` /
  ``list_projects``, and ``project_id`` on ``TaskRow`` / ``create_task``.
* ``lotsa.migrations`` — the clean-break ``tasks`` recreate + ``projects`` table.
* ``lotsa.orchestrator`` — per-project ``WorktreeManager`` resolution
  (``_worktree_manager_for``), the deleted singleton, ``project_id`` on
  ``create_task`` with create-time validation (``ProjectNotFound``), startup
  project sync (upsert, path-change reset, legacy-worktree cleanup,
  removed-from-YAML persistence), ``list_projects_summary``, and the
  ``{lotsa_prompts_dir}`` prompt injection.
* ``lotsa/prompts/full/review-system.md`` — repo-relative paths replaced by
  the ``{lotsa_prompts_dir}`` template.

New symbols are imported INSIDE each test so a missing symbol fails that one
test (ImportError) rather than breaking module collection for the whole file.

Per the repo's regression-test discipline, every behavioural assertion is
written so it would fail against the pre-ADR-029 code — see the module
docstring of each section for the specific pre-fix failure mode.
"""

from __future__ import annotations

import logging
import subprocess

import pytest
import yaml

from lotsa.config import LotsaConfig
from lotsa.db import TaskDB
from lotsa.flows import BUNDLED_PROMPTS
from lotsa.orchestrator import OrchestratorService
from lotsa.tests.conftest import wait_for_status
from lotsa.tests.test_orchestrator import FakeRunner

# ── helpers ────────────────────────────────────────────────────────────


def _init_git_repo(path):
    """Create a real git repo with one commit (mirrors test_orchestrator)."""
    path.mkdir(parents=True, exist_ok=True)
    subprocess.run(["git", "init", str(path)], capture_output=True, check=True)
    subprocess.run(["git", "-C", str(path), "config", "user.email", "test@test.com"], capture_output=True, check=True)
    subprocess.run(["git", "-C", str(path), "config", "user.name", "Test"], capture_output=True, check=True)
    (path / "README.md").write_text("# Test repo")
    subprocess.run(["git", "-C", str(path), "add", "."], capture_output=True, check=True)
    subprocess.run(["git", "-C", str(path), "commit", "-m", "init"], capture_output=True, check=True)
    return path


_GATED_FLOW = "name: gated\njobs:\n  - name: coding\n    evaluate: true\n"


def _write_gated_flow(path):
    path.write_text(_GATED_FLOW)
    return path


def _build_and_start(run, data_dir, yaml_text):
    """Write a lotsa.yaml, load config, start a service against it."""
    data_dir.mkdir(parents=True, exist_ok=True)
    cfg = data_dir / "lotsa.yaml"
    cfg.write_text(yaml_text)
    config = LotsaConfig.load(config_path=cfg, data_dir=data_dir)
    db = TaskDB(data_dir / "lotsa.db")
    run(db.initialize())
    svc = OrchestratorService(config, db)
    svc.runner = FakeRunner()
    run(svc.start())
    return svc, db


def _stop(run, svc, db):
    run(svc.shutdown())
    run(db.close())


def _two_project_yaml(repo_a, repo_b, flow_file):
    return yaml.dump(
        {
            "projects": {
                "alpha": {"path": str(repo_a)},
                "beta": {"path": str(repo_b)},
            },
            "flow": "custom",
            "flow_file": str(flow_file),
            "model": "sonnet",
            "budget": 5.0,
        }
    )


# ── config parsing & validation ────────────────────────────────────────


class TestProjectConfig:
    """``projects:`` block parsing + ``resolve_project_specs`` validation.

    Pre-ADR-029 the loader has no ``projects`` field (silently dropped) and no
    ``resolve_project_specs``; these tests fail on the missing field/symbol.
    """

    def test_projects_block_loads_from_yaml(self, tmp_path):
        cfg = tmp_path / "lotsa.yaml"
        cfg.write_text(yaml.dump({"projects": {"alpha": {"path": "~/code/alpha"}}}))
        config = LotsaConfig.load(config_path=cfg)
        # The raw block must survive onto the config (today it's an unknown key).
        assert config.projects == {"alpha": {"path": "~/code/alpha"}}

    def test_projects_defaults_to_empty_dict(self):
        config = LotsaConfig()
        assert config.projects == {}

    def test_resolve_specs_normalizes_and_validates_git(self, tmp_path):
        from lotsa.config import resolve_project_specs

        repo = _init_git_repo(tmp_path / "alpha")
        # A non-normalized path (with a ``..`` segment) must resolve to the
        # canonical absolute repo path.
        messy = str(repo.parent / "alpha" / ".." / "alpha")
        cfg = tmp_path / "lotsa.yaml"
        cfg.write_text(yaml.dump({"projects": {"alpha": {"path": messy}}}))
        config = LotsaConfig.load(config_path=cfg)

        specs = resolve_project_specs(config)
        by_id = {s.id: s for s in specs}
        assert "alpha" in by_id
        assert by_id["alpha"].path == repo.resolve()
        # name defaults to the id when omitted.
        assert by_id["alpha"].name == "alpha"

    def test_resolve_specs_rejects_invalid_id(self, tmp_path):
        from lotsa.config import resolve_project_specs

        repo = _init_git_repo(tmp_path / "repo")
        cfg = tmp_path / "lotsa.yaml"
        # Uppercase + slash violate ``[a-z0-9_-]{1,64}`` and would be a
        # filesystem-path / PK hazard.
        cfg.write_text(yaml.dump({"projects": {"Bad/Id": {"path": str(repo)}}}))
        config = LotsaConfig.load(config_path=cfg)

        with pytest.raises(ValueError) as exc:
            resolve_project_specs(config)
        # The error must name the offending key so the operator can fix it.
        assert "Bad/Id" in str(exc.value)

    def test_resolve_specs_rejects_missing_path(self, tmp_path):
        from lotsa.config import resolve_project_specs

        cfg = tmp_path / "lotsa.yaml"
        cfg.write_text(yaml.dump({"projects": {"ghost": {"path": str(tmp_path / "does_not_exist")}}}))
        config = LotsaConfig.load(config_path=cfg)
        with pytest.raises(ValueError):
            resolve_project_specs(config)

    def test_resolve_specs_rejects_non_git_path(self, tmp_path):
        from lotsa.config import resolve_project_specs

        plain = tmp_path / "plain"
        plain.mkdir()  # exists but is not a git repo
        cfg = tmp_path / "lotsa.yaml"
        cfg.write_text(yaml.dump({"projects": {"plain": {"path": str(plain)}}}))
        config = LotsaConfig.load(config_path=cfg)
        with pytest.raises(ValueError):
            resolve_project_specs(config)

    def test_work_dir_seeds_default_project(self, tmp_path):
        from lotsa.config import resolve_project_specs

        repo = _init_git_repo(tmp_path / "repo")
        cfg = tmp_path / "lotsa.yaml"
        cfg.write_text(yaml.dump({"work_dir": str(repo)}))
        config = LotsaConfig.load(config_path=cfg)

        specs = resolve_project_specs(config)
        by_id = {s.id: s for s in specs}
        # A single-project ``work_dir:`` config keeps working: it seeds ``default``.
        assert "default" in by_id
        assert by_id["default"].path == repo.resolve()

    def test_bare_dot_work_dir_seeds_default_from_cwd(self, tmp_path, monkeypatch):
        """The scaffolded zero-config case: ``lotsa init`` writes ``work_dir: "."``
        with no ``projects:`` block (cli.py), and ``lotsa serve`` leaves it ``.``.

        Regression (review): ``resolve_project_specs`` skipped seeding whenever
        ``work_dir == "."``, so a fresh ``lotsa init && lotsa serve`` registered
        zero projects and every ``create_task`` raised ``ProjectNotFound`` —
        breaking the out-of-box flow (ADR-029 §2 / acceptance criterion 6). The
        bare ``.`` must resolve to the launch CWD, exactly as the former
        singleton ``WorktreeManager(work_dir, …)`` did. Against the pre-fix code
        ``specs`` is empty and this assertion fails.
        """
        from lotsa.config import resolve_project_specs

        repo = _init_git_repo(tmp_path / "repo")
        cfg = repo / "lotsa.yaml"
        cfg.write_text(yaml.dump({"work_dir": ".", "model": "sonnet", "flow": "standard"}))
        config = LotsaConfig.load(config_path=cfg, data_dir=tmp_path / "data")
        # ``.`` resolves against the CWD, so run the resolution from inside the repo.
        monkeypatch.chdir(repo)

        by_id = {s.id: s for s in resolve_project_specs(config)}
        assert "default" in by_id, "a bare work_dir: '.' config must still seed a default project"
        assert by_id["default"].path == repo.resolve()

    def test_multi_project_block_without_work_dir_seeds_no_default(self, tmp_path):
        """The case the ``.``-skip was protecting must stay protected: a
        ``projects:`` block that omits ``work_dir`` (so ``work_dir`` is the
        unconfigured ``.`` sentinel) must NOT pick up a spurious CWD-rooted
        ``default`` alongside the declared projects."""
        from lotsa.config import resolve_project_specs

        repo_a = _init_git_repo(tmp_path / "a")
        repo_b = _init_git_repo(tmp_path / "b")
        cfg = tmp_path / "lotsa.yaml"
        cfg.write_text(yaml.dump({"projects": {"alpha": {"path": str(repo_a)}, "beta": {"path": str(repo_b)}}}))
        config = LotsaConfig.load(config_path=cfg, data_dir=tmp_path / "data")

        ids = {s.id for s in resolve_project_specs(config)}
        assert ids == {"alpha", "beta"}, "a multi-project block (no work_dir) must not seed a spurious default"

    def test_explicit_default_entry_wins_over_work_dir(self, tmp_path):
        from lotsa.config import resolve_project_specs

        work_repo = _init_git_repo(tmp_path / "workdir_repo")
        declared_repo = _init_git_repo(tmp_path / "declared_repo")
        cfg = tmp_path / "lotsa.yaml"
        # Both present: an explicit ``projects: default:`` is authoritative,
        # ``work_dir:`` seeding is a no-op for it regardless of order.
        cfg.write_text(
            yaml.dump(
                {
                    "work_dir": str(work_repo),
                    "projects": {"default": {"path": str(declared_repo)}},
                }
            )
        )
        config = LotsaConfig.load(config_path=cfg)
        by_id = {s.id: s for s in resolve_project_specs(config)}
        assert by_id["default"].path == declared_repo.resolve()

    def test_unknown_top_level_key_warns(self, tmp_path, caplog):
        cfg = tmp_path / "lotsa.yaml"
        # ``project_dir:`` is the real-world stale key the ADR calls out.
        cfg.write_text(yaml.dump({"project_dir": "/some/where", "model": "opus"}))
        with caplog.at_level(logging.WARNING):
            config = LotsaConfig.load(config_path=cfg)
        assert config.model == "opus"  # known keys still applied
        assert any("project_dir" in r.message for r in caplog.records), (
            "an unknown top-level key must produce a warning (no more silent no-op)"
        )

    def test_work_dir_carries_deprecation_warning(self, tmp_path, caplog):
        repo = _init_git_repo(tmp_path / "repo")
        cfg = tmp_path / "lotsa.yaml"
        cfg.write_text(yaml.dump({"work_dir": str(repo)}))
        with caplog.at_level(logging.WARNING):
            LotsaConfig.load(config_path=cfg)
        assert any("work_dir" in r.message.lower() for r in caplog.records), (
            "work_dir: must carry a deprecation warning"
        )


# ── DB: projects table + project_id on tasks ───────────────────────────


class TestProjectDB:
    """``ProjectRow`` + project queries + ``project_id`` on tasks.

    Pre-ADR-029 ``lotsa.db`` has no project surface and ``create_task`` takes
    no ``project_id`` — these fail on the missing symbol/keyword.
    """

    @pytest.fixture()
    def db(self, tmp_path, run):
        database = TaskDB(tmp_path / "lotsa.db")
        run(database.initialize())
        yield database
        run(database.close())

    def test_upsert_and_get_project(self, db, run):
        from lotsa.db import ProjectRow

        row = run(db.upsert_project("alpha", "Alpha", "/repos/alpha"))
        assert isinstance(row, ProjectRow)
        fetched = run(db.get_project("alpha"))
        assert fetched is not None
        assert fetched.id == "alpha"
        assert fetched.name == "Alpha"
        assert fetched.path == "/repos/alpha"

    def test_upsert_updates_name_and_path_preserving_created_at(self, db, run):
        run(db.upsert_project("alpha", "Alpha", "/repos/alpha"))
        first = run(db.get_project("alpha"))
        run(db.upsert_project("alpha", "Alpha Renamed", "/repos/moved"))
        second = run(db.get_project("alpha"))
        assert second.name == "Alpha Renamed"
        assert second.path == "/repos/moved"
        # created_at is immutable on upsert; updated_at moves forward.
        assert second.created_at == first.created_at

    def test_list_projects(self, db, run):
        run(db.upsert_project("alpha", "Alpha", "/repos/alpha"))
        run(db.upsert_project("beta", "Beta", "/repos/beta"))
        ids = {p.id for p in run(db.list_projects())}
        assert ids == {"alpha", "beta"}

    def test_create_task_records_project_id(self, db, run):
        run(db.upsert_project("alpha", "Alpha", "/repos/alpha"))
        task = run(db.create_task("My Task", project_id="alpha"))
        assert task.project_id == "alpha"
        fetched = run(db.get_task(task.id))
        assert fetched.project_id == "alpha"


# ── migration: clean break ─────────────────────────────────────────────


class TestMultiProjectMigration:
    """The schema PR recreates ``tasks`` with ``project_id NOT NULL`` and adds
    a ``projects`` table — a pre-alpha clean break that drops old tasks.

    Pre-ADR-029 there is no such migration: the legacy task row survives and
    ``project_id`` never appears, so each assertion below fails.
    """

    def _legacy_db(self, tmp_path):
        import sqlite3

        conn = sqlite3.connect(str(tmp_path / "t.db"), isolation_level=None)
        conn.row_factory = sqlite3.Row
        # Pre-_m001 original schema + one legacy task row.
        conn.executescript(
            "CREATE TABLE tasks (id TEXT PRIMARY KEY, title TEXT NOT NULL, body TEXT,"
            "  state TEXT NOT NULL, priority INTEGER, flow_name TEXT, metadata TEXT,"
            "  created_at TEXT, updated_at TEXT);"
            "CREATE TABLE messages (id INTEGER PRIMARY KEY AUTOINCREMENT, task_id TEXT,"
            "  role TEXT, step_name TEXT, content TEXT, type TEXT, metadata TEXT,"
            "  created_at TEXT);"
        )
        conn.execute("INSERT INTO tasks VALUES ('old1','Legacy','','coding',0,'simple','{}','2026-01-01','2026-01-01')")
        return conn

    def test_migration_creates_projects_table(self, tmp_path):
        from lotsa.migrations import apply_migrations

        conn = self._legacy_db(tmp_path)
        apply_migrations(conn)
        tables = {r["name"] for r in conn.execute("SELECT name FROM sqlite_master WHERE type='table'")}
        assert "projects" in tables
        conn.close()

    def test_migration_adds_not_null_project_id(self, tmp_path):
        from lotsa.migrations import apply_migrations

        conn = self._legacy_db(tmp_path)
        apply_migrations(conn)
        cols = {r["name"]: r for r in conn.execute("PRAGMA table_info(tasks)")}
        assert "project_id" in cols
        assert cols["project_id"]["notnull"] == 1, "project_id must be NOT NULL"
        conn.close()

    def test_migration_indexes_project_id(self, tmp_path):
        """``project_id`` is a filterable FK dimension (dashboard project
        filter, ``_relocate_project``'s per-project sweep), so _m004 must index
        it alongside ``status``/``state``. Against the pre-fix _m004 (which
        recreated only the status/state indexes) the index is absent and this
        assertion fails.
        """
        from lotsa.migrations import apply_migrations

        conn = self._legacy_db(tmp_path)
        apply_migrations(conn)
        indexes = {r["name"] for r in conn.execute("PRAGMA index_list(tasks)")}
        assert "idx_tasks_project_id" in indexes
        conn.close()

    def test_migration_clean_break_drops_pre_multi_project_tasks(self, tmp_path):
        from lotsa.migrations import apply_migrations

        conn = self._legacy_db(tmp_path)
        apply_migrations(conn)
        count = conn.execute("SELECT COUNT(*) AS c FROM tasks").fetchone()["c"]
        assert count == 0, "clean break recreates tasks — pre-multi-project rows are dropped"
        conn.close()

    def test_migration_clean_break_clears_orphaned_messages(self, tmp_path):
        """The clean break drops ``tasks``, so any message bound to a pre-break
        task becomes an orphan referencing a nonexistent row. _m004 clears the
        ``messages`` log too. Against the pre-fix code (DROP tasks but no
        ``DELETE FROM messages``) the orphaned row survives and this fails with
        ``count == 1``.
        """
        from lotsa.migrations import apply_migrations

        conn = self._legacy_db(tmp_path)
        conn.execute(
            "INSERT INTO messages (task_id, role, step_name, content, type, metadata, created_at) "
            "VALUES ('old1', 'system', '', 'legacy note', 'status_change', '{}', '2026-01-01')"
        )
        apply_migrations(conn)
        count = conn.execute("SELECT COUNT(*) AS c FROM messages").fetchone()["c"]
        assert count == 0, "clean break clears messages orphaned by the dropped tasks"
        conn.close()


# ── per-project WorktreeManager resolution ─────────────────────────────


class TestPerProjectWorktreeResolution:
    """The singleton ``self.worktree_manager`` becomes per-project, cached.

    Pre-ADR-029 the singleton exists and ``_worktree_manager_for`` /
    ``_projects`` / namespacing do not.
    """

    def test_singleton_worktree_manager_removed(self, tmp_path, run):
        repo_a = _init_git_repo(tmp_path / "repo_a")
        repo_b = _init_git_repo(tmp_path / "repo_b")
        flow = _write_gated_flow(tmp_path / "gated.yaml")
        svc, db = _build_and_start(run, tmp_path / "data", _two_project_yaml(repo_a, repo_b, flow))
        try:
            # Deleting the attribute is the mitigation: a missed call site must
            # fail loudly, not silently dispatch into the wrong repo.
            assert not hasattr(svc, "worktree_manager")
        finally:
            _stop(run, svc, db)

    def test_worktree_manager_for_builds_namespaced_manager(self, tmp_path, run):
        from lotsa.db import ProjectRow

        repo_a = _init_git_repo(tmp_path / "repo_a")
        repo_b = _init_git_repo(tmp_path / "repo_b")
        flow = _write_gated_flow(tmp_path / "gated.yaml")
        data_dir = tmp_path / "data"
        svc, db = _build_and_start(run, data_dir, _two_project_yaml(repo_a, repo_b, flow))
        try:
            project = ProjectRow(
                id="alpha",
                name="alpha",
                path=str(repo_a),
                created_at="2026-01-01",
                updated_at="2026-01-01",
            )
            wtm = svc._worktree_manager_for(project)
            # Worktrees are namespaced under the project id.
            assert wtm.dir == data_dir / "worktrees" / "alpha"
            assert wtm.repo == repo_a.resolve()
            # Cached by project id — same instance on the second call.
            assert svc._worktree_manager_for(project) is wtm
        finally:
            _stop(run, svc, db)

    def test_worktree_created_under_project_namespace(self, tmp_path, run):
        repo_a = _init_git_repo(tmp_path / "repo_a")
        repo_b = _init_git_repo(tmp_path / "repo_b")
        flow = _write_gated_flow(tmp_path / "gated.yaml")
        data_dir = tmp_path / "data"
        svc, db = _build_and_start(run, data_dir, _two_project_yaml(repo_a, repo_b, flow))
        try:
            task = run(svc.create_task("Namespaced", project_id="beta"))
            run(wait_for_status(svc, task.id, "waiting"))
            # The on-disk worktree lands under worktrees/<project_id>/<task_id>.
            wt = data_dir / "worktrees" / "beta" / task.id
            assert wt.exists()
            assert (wt / ".git").exists()
        finally:
            _stop(run, svc, db)

    def test_explicit_default_project_path_overrides_work_dir_seed(self, tmp_path, run):
        """An explicit ``projects: default: {path: X}`` must resolve the
        default project's WorktreeManager to ``X`` — not the pre-seeded
        ``config.work_dir`` (which defaults to ``"."`` → the launch CWD).

        Regression (review): ``_worktree_managers['default']`` is pre-seeded
        from ``config.work_dir`` at ``__init__`` time and was never reconciled
        against the resolved project path, so tasks in the ``default`` project
        silently branched off ``work_dir``/CWD instead of ``X``. Against the
        pre-fix code ``wtm.repo`` is the CWD (the worktree root running the
        test), not ``X``, so this assertion fails.
        """
        real = _init_git_repo(tmp_path / "real_default_repo")
        flow = _write_gated_flow(tmp_path / "gated.yaml")
        # Recommended migration style: declare projects, omit deprecated work_dir.
        yaml_text = yaml.dump(
            {
                "projects": {"default": {"path": str(real)}},
                "flow": "custom",
                "flow_file": str(flow),
                "model": "sonnet",
                "budget": 5.0,
            }
        )
        svc, db = _build_and_start(run, tmp_path / "data", yaml_text)
        try:
            wtm = svc._worktree_manager_for(svc._projects["default"])
            assert wtm.repo == real.resolve()
            # Dispatch-path resolution (Item with no mirrored metadata → falls
            # back to the ``default`` project) must land on the same repo.
            item = run(svc.create_task("Default repo task"))
            run(wait_for_status(svc, item.id, "waiting"))
            assert svc._worktree_manager_for_task(item).repo == real.resolve()
        finally:
            _stop(run, svc, db)


# ── create_task validation ─────────────────────────────────────────────


class TestCreateTaskProjectValidation:
    """``create_task`` gains ``project_id`` with create-time validation."""

    def test_create_task_unknown_project_raises(self, tmp_path, run):
        from lotsa.orchestrator import ProjectNotFound

        repo_a = _init_git_repo(tmp_path / "repo_a")
        repo_b = _init_git_repo(tmp_path / "repo_b")
        flow = _write_gated_flow(tmp_path / "gated.yaml")
        svc, db = _build_and_start(run, tmp_path / "data", _two_project_yaml(repo_a, repo_b, flow))
        try:
            with pytest.raises(ProjectNotFound):
                run(svc.create_task("Bad project", project_id="nonexistent"))
        finally:
            _stop(run, svc, db)

    def test_create_task_records_resolved_project_id(self, tmp_path, run):
        repo_a = _init_git_repo(tmp_path / "repo_a")
        repo_b = _init_git_repo(tmp_path / "repo_b")
        flow = _write_gated_flow(tmp_path / "gated.yaml")
        svc, db = _build_and_start(run, tmp_path / "data", _two_project_yaml(repo_a, repo_b, flow))
        try:
            task = run(svc.create_task("Tagged", project_id="alpha"))
            row = run(db.get_task(task.id))
            assert row.project_id == "alpha"
            # Mirrored into metadata so dispatch sites resolve without a DB read.
            assert row.metadata.get("project_id") == "alpha"
        finally:
            _stop(run, svc, db)

    def test_omitted_project_auto_picks_sole_offered_after_yaml_removal(self, tmp_path, run):
        """Auto-pick scopes to offered (YAML-declared) projects, not every DB row.

        Regression for the ``_resolve_project_id(None)`` bug: a deployment that
        previously registered two projects, then drops one from ``lotsa.yaml``
        and restarts, keeps the removed project in ``_projects`` (ADR-029 §2
        removal policy) while ``_yaml_project_ids`` shrinks to one. Creating a
        task with no explicit project must auto-pick the sole *offered* project.
        Against the pre-fix code (which scoped on ``len(self._projects) == 1``)
        this raised ``ProjectNotFound`` because ``_projects`` still held two
        entries.
        """
        repo_a = _init_git_repo(tmp_path / "repo_a")
        repo_b = _init_git_repo(tmp_path / "repo_b")
        flow = _write_gated_flow(tmp_path / "gated.yaml")
        data_dir = tmp_path / "data"

        # First boot registers both alpha and beta.
        svc1, db1 = _build_and_start(run, data_dir, _two_project_yaml(repo_a, repo_b, flow))
        _stop(run, svc1, db1)

        # Reboot with beta dropped — alpha is now the sole offered project, but
        # beta persists in the DB (and therefore in ``_projects``).
        single = yaml.dump(
            {
                "projects": {"alpha": {"path": str(repo_a)}},
                "flow": "custom",
                "flow_file": str(flow),
                "model": "sonnet",
                "budget": 5.0,
            }
        )
        svc2, db2 = _build_and_start(run, data_dir, single)
        try:
            assert "beta" in svc2._projects  # removed project still persisted
            assert svc2._yaml_project_ids == {"alpha"}
            # No explicit project_id: must resolve to the sole offered project.
            task = run(svc2.create_task("Auto-picks alpha"))
            row = run(db2.get_task(task.id))
            assert row.project_id == "alpha"
        finally:
            _stop(run, svc2, db2)

    def test_work_dir_only_config_seeds_default_and_create_task_uses_it(self, tmp_path, run):
        """Backward compatibility (acceptance criterion 6): a ``work_dir``-only
        config seeds a ``default`` project and task creation works without an
        explicit project_id."""
        repo = _init_git_repo(tmp_path / "repo")
        flow = _write_gated_flow(tmp_path / "gated.yaml")
        yaml_text = yaml.dump(
            {
                "work_dir": str(repo),
                "flow": "custom",
                "flow_file": str(flow),
                "model": "sonnet",
                "budget": 5.0,
            }
        )
        svc, db = _build_and_start(run, tmp_path / "data", yaml_text)
        try:
            task = run(svc.create_task("No explicit project"))
            row = run(db.get_task(task.id))
            assert row.project_id == "default"
        finally:
            _stop(run, svc, db)


# ── startup project sync ───────────────────────────────────────────────


class TestStartupProjectSync:
    """Upsert, path-change reset, legacy-worktree cleanup, removal policy."""

    def test_sync_populates_projects_and_yaml_ids(self, tmp_path, run):
        repo_a = _init_git_repo(tmp_path / "repo_a")
        repo_b = _init_git_repo(tmp_path / "repo_b")
        flow = _write_gated_flow(tmp_path / "gated.yaml")
        svc, db = _build_and_start(run, tmp_path / "data", _two_project_yaml(repo_a, repo_b, flow))
        try:
            assert set(svc._projects) == {"alpha", "beta"}
            assert svc._yaml_project_ids == {"alpha", "beta"}
            # Rows are persisted in the DB too.
            assert {p.id for p in run(db.list_projects())} == {"alpha", "beta"}
        finally:
            _stop(run, svc, db)

    def test_legacy_flat_worktrees_are_cleaned_up(self, tmp_path, run):
        repo_a = _init_git_repo(tmp_path / "repo_a")
        repo_b = _init_git_repo(tmp_path / "repo_b")
        flow = _write_gated_flow(tmp_path / "gated.yaml")
        data_dir = tmp_path / "data"
        # Pre-seed an old flat worktree dir (the pre-multi-project layout).
        legacy = data_dir / "worktrees" / "deadbeef"
        legacy.mkdir(parents=True)
        (legacy / ".git").write_text("gitdir: /old/repo/.git/worktrees/deadbeef\n")

        svc, db = _build_and_start(run, data_dir, _two_project_yaml(repo_a, repo_b, flow))
        try:
            # The old flat task worktree (a ``.git`` directly under worktrees/<id>)
            # must be removed by the clean-break startup sweep.
            assert not legacy.exists()
        finally:
            _stop(run, svc, db)

    def test_path_change_resets_non_terminal_tasks_and_removes_worktrees(self, tmp_path, run):
        repo_a = _init_git_repo(tmp_path / "repo_a")
        repo_b = _init_git_repo(tmp_path / "repo_b")
        flow = _write_gated_flow(tmp_path / "gated.yaml")
        data_dir = tmp_path / "data"

        # First boot: alpha -> repo_a. Create a task and let it reach the gate
        # (non-terminal, worktree on disk under worktrees/alpha/<task_id>).
        # svc1 is wrapped in try/finally so it is always shut down — otherwise a
        # pre-implementation ``create_task(project_id=...)`` TypeError would leak
        # the service and its completion-drainer would busy-spin on the loop.
        svc1, db1 = _build_and_start(run, data_dir, _two_project_yaml(repo_a, repo_b, flow))
        try:
            task = run(svc1.create_task("Will be relocated", project_id="alpha"))
            run(wait_for_status(svc1, task.id, "waiting"))
            wt_before = data_dir / "worktrees" / "alpha" / task.id
            assert wt_before.exists()
        finally:
            _stop(run, svc1, db1)

        # Relocate alpha to a different repo and reboot.
        repo_a_moved = _init_git_repo(tmp_path / "repo_a_moved")
        svc2, db2 = _build_and_start(run, data_dir, _two_project_yaml(repo_a_moved, repo_b, flow))
        try:
            # The stale worktree (its .git gitdir points at the old repo) is removed,
            # and the non-terminal task is reset so it rebuilds on next dispatch.
            assert not wt_before.exists()
            row = run(db2.get_task(task.id))
            assert row.status == "blocked"
        finally:
            _stop(run, svc2, db2)

    def test_relocate_isolates_per_task_failures(self, tmp_path, run):
        """A transient error resetting one task must not abort the relocate
        sweep — the remaining non-terminal tasks in the project still reset.

        Against the pre-fix code (no per-row guard) the first task's failing
        ``_set_status`` propagates out of ``_relocate_project``, the loop never
        reaches the second task, and ``_relocate_project`` itself raises — so
        exactly-one-blocked fails (zero are blocked) and the call raises.
        """
        repo_a = _init_git_repo(tmp_path / "repo_a")
        repo_b = _init_git_repo(tmp_path / "repo_b")
        flow = _write_gated_flow(tmp_path / "gated.yaml")
        data_dir = tmp_path / "data"

        svc, db = _build_and_start(run, data_dir, _two_project_yaml(repo_a, repo_b, flow))
        try:
            # Two non-terminal tasks in the same project, both parked at the gate.
            t1 = run(svc.create_task("First", project_id="alpha"))
            t2 = run(svc.create_task("Second", project_id="alpha"))
            run(wait_for_status(svc, t1.id, "waiting"))
            run(wait_for_status(svc, t2.id, "waiting"))

            # Make the first reset attempt raise (a transient DB error), the
            # rest pass through — exercises the failure from inside the sweep.
            real_set_status = svc._set_status
            calls = {"n": 0}

            async def flaky_set_status(task_id, status, current_step):
                calls["n"] += 1
                if calls["n"] == 1:
                    raise RuntimeError("transient DB error")
                return await real_set_status(task_id, status, current_step)

            svc._set_status = flaky_set_status

            # Must not propagate — the guard swallows the per-row failure.
            run(svc._relocate_project("alpha"))

            statuses = [run(db.get_task(tid)).status for tid in (t1.id, t2.id)]
            assert statuses.count("blocked") == 1, statuses
        finally:
            _stop(run, svc, db)

    def test_removed_from_yaml_project_persists_but_not_offered(self, tmp_path, run):
        repo_a = _init_git_repo(tmp_path / "repo_a")
        repo_b = _init_git_repo(tmp_path / "repo_b")
        flow = _write_gated_flow(tmp_path / "gated.yaml")
        data_dir = tmp_path / "data"

        svc1, db1 = _build_and_start(run, data_dir, _two_project_yaml(repo_a, repo_b, flow))
        _stop(run, svc1, db1)

        # Reboot with beta dropped from YAML.
        single = yaml.dump(
            {
                "projects": {"alpha": {"path": str(repo_a)}},
                "flow": "custom",
                "flow_file": str(flow),
                "model": "sonnet",
                "budget": 5.0,
            }
        )
        svc2, db2 = _build_and_start(run, data_dir, single)
        try:
            # Removal policy: the row persists for dispatch...
            assert "beta" in svc2._projects
            # ...but new-task creation only offers YAML-declared projects.
            assert svc2._yaml_project_ids == {"alpha"}
            offered = {p["id"] for p in svc2.list_projects_summary()}
            assert offered == {"alpha"}
        finally:
            _stop(run, svc2, db2)


# ── prompt portability ({lotsa_prompts_dir}) ───────────────────────────


class TestPromptPortability:
    """``full/review-system.md`` must reference its workflow files via the
    ``{lotsa_prompts_dir}`` template, not a repo-relative path.

    Pre-ADR-029 the file hardcodes ``lotsa/prompts/review/SKILL.md`` and the
    orchestrator performs no substitution.
    """

    def test_review_prompt_file_uses_template(self):
        text = (BUNDLED_PROMPTS / "full" / "review-system.md").read_text()
        assert "{lotsa_prompts_dir}/review/SKILL.md" in text
        assert "{lotsa_prompts_dir}/review/checklist.md" in text
        # The old repo-relative form must be gone.
        assert "lotsa/prompts/review/SKILL.md" not in text

    def test_build_system_prompt_injects_prompts_dir(self, tmp_path, run):
        from lotsa.flows import build_process

        process = build_process("full")
        review_step = next(s for s in process.flows["main"].jobs if s.prompt_name == "review")

        db = TaskDB(tmp_path / "lotsa.db")
        run(db.initialize())
        config = LotsaConfig(data_dir=tmp_path, work_dir=tmp_path, flow="full")
        svc = OrchestratorService(config, db)
        svc.runner = FakeRunner()
        # Wire the active flow without a full start() (no project validation needed).
        svc.process = process
        svc.flow = process.flows["main"]
        try:
            rendered = svc._build_system_prompt(review_step, None)
            # The token is resolved to the absolute bundled prompts dir.
            assert f"{BUNDLED_PROMPTS}/review/SKILL.md" in rendered
            assert "{lotsa_prompts_dir}" not in rendered
        finally:
            run(db.close())


# ── project API / schema surface ───────────────────────────────────────


class TestProjectApiSurface:
    """Backend contract for the new-task project picker (PR 4)."""

    def test_create_task_request_accepts_project(self):
        from lotsa.server.api_routes import CreateTaskRequest

        req = CreateTaskRequest(message="Build X", project="alpha")
        assert req.project == "alpha"

    def test_project_summary_schema_exists(self):
        from lotsa.server.api_routes import ProjectSummary

        summary = ProjectSummary(id="alpha", name="Alpha", path="/repos/alpha")
        assert summary.id == "alpha"
        assert summary.name == "Alpha"
        assert summary.path == "/repos/alpha"

    def test_task_summary_response_exposes_project_id(self):
        from lotsa.server.schemas import TaskSummaryResponse

        # The field must exist so the task list can render a project badge/filter.
        assert "project_id" in TaskSummaryResponse.model_fields
