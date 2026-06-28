"""Acceptance tests for the 'remove dead CLI and file-based item source' change.

These tests verify the acceptance criteria from the implementation plan:

  * lotsa/runner.py, lotsa/file_item_source.py, and their test files are deleted
  * `lotsa new` and `lotsa approve` are unregistered from the Click group
  * `lotsa init` no longer writes example-task.yaml and points users at the
    dashboard in its quick-start message
  * LotsaConfig has no `loop` attribute
  * `next_dispatchable_state` is preserved in lotsa.flows (the orchestrator
    no longer imports it at module level after the gate-state refactor, but
    the function itself remains)
  * Live code (lotsa/*.py, lotsa/README.md) and CLAUDE.md contain no stale
    references to FileItemSource, lotsa.runner, or the removed CLI commands

Tests in this file fail until the removal is complete. A few regression-style
checks (e.g. "init is still registered") are included to catch over-deletion.
"""

from __future__ import annotations

import importlib
import inspect
from dataclasses import fields
from pathlib import Path

import pytest
import yaml
from click.testing import CliRunner

REPO_ROOT = Path(__file__).resolve().parents[2]


# ---------------------------------------------------------------------------
# Module-level deletions
# ---------------------------------------------------------------------------


def test_runner_module_file_is_deleted():
    """lotsa/runner.py no longer exists on disk."""
    assert not (REPO_ROOT / "lotsa" / "runner.py").exists()


def test_file_item_source_module_file_is_deleted():
    """lotsa/file_item_source.py no longer exists on disk."""
    assert not (REPO_ROOT / "lotsa" / "file_item_source.py").exists()


def test_runner_test_file_is_deleted():
    """lotsa/tests/test_runner.py no longer exists on disk."""
    assert not (REPO_ROOT / "lotsa" / "tests" / "test_runner.py").exists()


def test_file_item_source_test_file_is_deleted():
    """lotsa/tests/test_file_item_source.py no longer exists on disk."""
    assert not (REPO_ROOT / "lotsa" / "tests" / "test_file_item_source.py").exists()


def test_runner_module_cannot_be_imported():
    """`import lotsa.runner` raises ImportError / ModuleNotFoundError."""
    import sys

    sys.modules.pop("lotsa.runner", None)
    with pytest.raises(ImportError):
        importlib.import_module("lotsa.runner")


def test_file_item_source_module_cannot_be_imported():
    """`import lotsa.file_item_source` raises ImportError / ModuleNotFoundError."""
    import sys

    sys.modules.pop("lotsa.file_item_source", None)
    with pytest.raises(ImportError):
        importlib.import_module("lotsa.file_item_source")


# ---------------------------------------------------------------------------
# Click command registration
# ---------------------------------------------------------------------------


def test_lotsa_new_command_is_not_registered():
    """The `new` command is no longer attached to the CLI group."""
    from lotsa.cli import cli

    assert "new" not in cli.commands


def test_lotsa_approve_command_is_not_registered():
    """The `approve` command is no longer attached to the CLI group."""
    from lotsa.cli import cli

    assert "approve" not in cli.commands


def test_lotsa_init_command_is_still_registered():
    """Regression: `init` survives the trim."""
    from lotsa.cli import cli

    assert "init" in cli.commands


def test_lotsa_build_command_is_still_registered():
    """Regression: `build` survives the trim."""
    from lotsa.cli import cli

    assert "build" in cli.commands


def test_lotsa_serve_command_is_still_registered():
    """Regression: `serve` survives the trim."""
    from lotsa.cli import cli

    assert "serve" in cli.commands


def test_lotsa_new_invocation_fails_with_nonzero_exit():
    """Invoking `lotsa new ...` exits non-zero because the command is gone."""
    from lotsa.cli import cli

    result = CliRunner().invoke(cli, ["new", "Add auth"])
    assert result.exit_code != 0


def test_lotsa_approve_invocation_fails_with_nonzero_exit(tmp_path):
    """Invoking `lotsa approve ...` exits non-zero because the command is gone."""
    from lotsa.cli import cli

    task_file = tmp_path / "task.yaml"
    task_file.write_text(yaml.dump({"title": "X", "state": "planned"}))
    result = CliRunner().invoke(cli, ["approve", str(task_file), "--flow", "full"])
    assert result.exit_code != 0


# ---------------------------------------------------------------------------
# `lotsa init` trim
# ---------------------------------------------------------------------------


def test_init_does_not_create_example_task(tmp_path):
    """lotsa init no longer writes example-task.yaml."""
    from lotsa.cli import cli

    data_dir = tmp_path / "lotsa"
    result = CliRunner().invoke(cli, ["init", str(data_dir)])

    assert result.exit_code == 0
    assert not (data_dir / "example-task.yaml").exists()


def test_init_still_creates_lotsa_yaml(tmp_path):
    """Regression: lotsa init still scaffolds lotsa.yaml."""
    from lotsa.cli import cli

    data_dir = tmp_path / "lotsa"
    result = CliRunner().invoke(cli, ["init", str(data_dir)])

    assert result.exit_code == 0
    assert (data_dir / "lotsa.yaml").is_file()


def test_init_does_not_create_logs_directory(tmp_path):
    """lotsa init no longer creates an empty logs/ — nothing in the codebase
    writes to it. The dashboard streams to the SQLite DB instead.
    """
    from lotsa.cli import cli

    data_dir = tmp_path / "lotsa"
    result = CliRunner().invoke(cli, ["init", str(data_dir)])

    assert result.exit_code == 0
    assert not (data_dir / "logs").exists()


def test_init_output_does_not_mention_lotsa_run(tmp_path):
    """The quick-start message no longer tells users to run `lotsa run`."""
    from lotsa.cli import cli

    data_dir = tmp_path / "lotsa"
    result = CliRunner().invoke(cli, ["init", str(data_dir)])

    assert result.exit_code == 0
    assert "lotsa run" not in result.output


def test_init_output_does_not_mention_example_task(tmp_path):
    """The quick-start message no longer references example-task.yaml."""
    from lotsa.cli import cli

    data_dir = tmp_path / "lotsa"
    result = CliRunner().invoke(cli, ["init", str(data_dir)])

    assert result.exit_code == 0
    assert "example-task" not in result.output


def test_init_output_points_users_at_the_dashboard(tmp_path):
    """The quick-start message tells users to start the dashboard."""
    from lotsa.cli import cli

    data_dir = tmp_path / "lotsa"
    result = CliRunner().invoke(cli, ["init", str(data_dir)])

    assert result.exit_code == 0
    assert "lotsa serve" in result.output


def test_init_is_idempotent_against_lotsa_yaml(tmp_path):
    """Running init twice leaves an existing lotsa.yaml untouched."""
    from lotsa.cli import cli

    data_dir = tmp_path / "lotsa"
    CliRunner().invoke(cli, ["init", str(data_dir)])
    (data_dir / "lotsa.yaml").write_text("modified: true")
    CliRunner().invoke(cli, ["init", str(data_dir)])

    assert (data_dir / "lotsa.yaml").read_text() == "modified: true"


# ---------------------------------------------------------------------------
# LotsaConfig.loop removal
# ---------------------------------------------------------------------------


def test_lotsa_config_instance_has_no_loop_attribute():
    """A LotsaConfig instance no longer exposes `.loop`."""
    from lotsa.config import LotsaConfig

    config = LotsaConfig()
    assert not hasattr(config, "loop")


def test_lotsa_config_dataclass_fields_do_not_include_loop():
    """`loop` is removed from the LotsaConfig dataclass definition."""
    from lotsa.config import LotsaConfig

    field_names = {f.name for f in fields(LotsaConfig)}
    assert "loop" not in field_names


def test_lotsa_config_load_ignores_loop_key_in_yaml(tmp_path):
    """Legacy lotsa.yaml files containing `loop:` still load without error.

    The `_apply_yaml` helper iterates over dataclass fields, so removing the
    field naturally causes `loop:` entries in YAML to be silently skipped.
    """
    from lotsa.config import LotsaConfig

    config_file = tmp_path / "lotsa.yaml"
    config_file.write_text(yaml.dump({"model": "sonnet", "loop": 30}))

    config = LotsaConfig.load(data_dir=tmp_path, config_path=config_file)
    assert not hasattr(config, "loop")
    assert config.model == "sonnet"


# ---------------------------------------------------------------------------
# `next_dispatchable_state` preservation
# ---------------------------------------------------------------------------


def test_next_dispatchable_state_is_still_exported_from_flows():
    """Regression: the orchestrator's helper is still importable."""
    from lotsa.flows import next_dispatchable_state

    assert callable(next_dispatchable_state)


def test_next_dispatchable_state_docstring_does_not_mention_lotsa_approve():
    """The docstring no longer references the removed `lotsa approve` command."""
    from lotsa.flows import next_dispatchable_state

    doc = inspect.getdoc(next_dispatchable_state) or ""
    assert "lotsa approve" not in doc


# ---------------------------------------------------------------------------
# Grep-based scrub of stale references in live code + top-level docs
# ---------------------------------------------------------------------------


def _read(relative_path: str) -> str:
    return (REPO_ROOT / relative_path).read_text()


_LIVE_FILES = [
    "lotsa/README.md",
    "lotsa/flows.py",
    "lotsa/db.py",
    "lotsa/cli.py",
    "CLAUDE.md",
]


@pytest.mark.parametrize("path", _LIVE_FILES)
def test_live_files_have_no_reference_to_FileItemSource(path):
    """Live code and top-level docs no longer mention FileItemSource."""
    assert "FileItemSource" not in _read(path)


@pytest.mark.parametrize("path", _LIVE_FILES)
def test_live_files_have_no_reference_to_lotsa_runner_module(path):
    """Live code and top-level docs no longer import or cite lotsa.runner."""
    text = _read(path)
    assert "from lotsa.runner" not in text
    assert "lotsa.runner" not in text


@pytest.mark.parametrize("path", _LIVE_FILES)
def test_live_files_have_no_reference_to_file_item_source_module(path):
    """Live code and top-level docs no longer cite lotsa.file_item_source."""
    assert "lotsa.file_item_source" not in _read(path)


@pytest.mark.parametrize("path", _LIVE_FILES)
def test_live_files_have_no_reference_to_run_once_or_run_loop(path):
    """Live code and top-level docs no longer cite run_once/run_loop."""
    text = _read(path)
    assert "run_once" not in text
    assert "run_loop" not in text


@pytest.mark.parametrize(
    "path,phrase",
    [
        ("lotsa/README.md", "lotsa new"),
        ("lotsa/README.md", "lotsa approve"),
        ("lotsa/README.md", "lotsa run"),
        ("lotsa/README.md", "lotsa chat"),
        ("lotsa/README.md", "--loop"),
        ("lotsa/cli.py", "lotsa run"),
        ("lotsa/cli.py", "lotsa chat"),
        ("CLAUDE.md", "lotsa run"),
        ("CLAUDE.md", "lotsa chat"),
    ],
)
def test_live_files_have_no_stale_command_references(path, phrase):
    """README, cli.py, and CLAUDE.md no longer reference removed CLI commands."""
    assert phrase not in _read(path)


def test_flows_comment_no_longer_references_approve_command():
    """The `# ... (for approve command)` comment has been rewritten."""
    assert "for approve command" not in _read("lotsa/flows.py")


def test_db_module_docstring_no_longer_references_FileItemSource():
    """lotsa/db.py module docstring no longer says 'Replaces FileItemSource'."""
    import lotsa.db

    doc = lotsa.db.__doc__ or ""
    assert "FileItemSource" not in doc
    assert "Replaces" not in doc


def test_claude_md_cli_description_is_updated():
    """CLAUDE.md's lotsa/cli.py description lists the current commands."""
    text = _read("CLAUDE.md")
    assert "lotsa run, lotsa chat, lotsa flows" not in text


def test_readme_architecture_table_uses_SQLiteItemSource():
    """The README's ItemSource row points at SQLiteItemSource, not FileItemSource."""
    text = _read("lotsa/README.md")
    assert "SQLiteItemSource" in text


def test_readme_no_longer_documents_loop_config_field():
    """README's lotsa.yaml example no longer includes a `loop:` line."""
    text = _read("lotsa/README.md")
    # Tolerate the word elsewhere; the specific stale doc-line is what we're after.
    assert "loop: 0" not in text
    assert "loop: 30" not in text


# ---------------------------------------------------------------------------
# Legacy HTMX / Jinja UI removal
# ---------------------------------------------------------------------------


def test_templates_directory_is_deleted():
    """lotsa/server/templates/ no longer exists — React SPA is the only UI."""
    assert not (REPO_ROOT / "lotsa" / "server" / "templates").exists()


@pytest.mark.parametrize(
    "asset",
    ["htmx.min.js", "tailwind.min.js", "dashboard.js", "chat-ui.css"],
)
def test_legacy_static_assets_are_deleted(asset):
    """HTMX library, CDN Tailwind, and Jinja-only CSS/JS are gone."""
    assert not (REPO_ROOT / "lotsa" / "server" / "static" / asset).exists()


def test_legacy_static_fonts_directory_is_deleted():
    """lotsa/server/static/fonts/ removed — SPA build emits its own under dist/fonts/."""
    assert not (REPO_ROOT / "lotsa" / "server" / "static" / "fonts").exists()


@pytest.mark.parametrize(
    "needle",
    ["Jinja2Templates", "TemplateResponse", "import nh3", "Form(", "csrf_token"],
)
def test_server_app_has_no_jinja_or_form_machinery(needle):
    """lotsa/server/app.py is SPA + JSON only — no Jinja or form-CSRF machinery."""
    text = _read("lotsa/server/app.py")
    assert needle not in text, f"Found legacy marker {needle!r} in lotsa/server/app.py"


@pytest.mark.parametrize(
    "test_file",
    ["test_server.py", "test_chat_ui.py", "test_chat_log_status_bar.py"],
)
def test_legacy_test_files_are_deleted(test_file):
    """The HTMX/Jinja test files were removed along with the UI they covered."""
    assert not (REPO_ROOT / "lotsa" / "tests" / test_file).exists()
