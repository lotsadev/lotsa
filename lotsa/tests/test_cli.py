"""Tests for the Click CLI."""

import yaml
from click.testing import CliRunner

from lotsa.cli import cli


def test_init_creates_directory(tmp_path):
    """lotsa init creates the data directory with a lotsa.yaml file."""
    data_dir = tmp_path / "lotsa"
    runner = CliRunner()
    result = runner.invoke(cli, ["init", str(data_dir)])

    assert result.exit_code == 0
    assert (data_dir / "lotsa.yaml").exists()

    # Verify config content
    config = yaml.safe_load((data_dir / "lotsa.yaml").read_text())
    assert config["model"] == "sonnet"


def test_init_does_not_create_logs(tmp_path):
    """lotsa init does not create the legacy logs/ subdirectory.

    The dashboard streams to the SQLite DB; nothing in the codebase
    writes to ``logs/``. Keeping the mkdir would leave a dead directory
    on every fresh install.
    """
    data_dir = tmp_path / "lotsa"
    runner = CliRunner()
    runner.invoke(cli, ["init", str(data_dir)])
    assert not (data_dir / "logs").exists()


def test_init_idempotent(tmp_path):
    """Running init twice doesn't overwrite an existing lotsa.yaml."""
    data_dir = tmp_path / "lotsa"
    runner = CliRunner()

    runner.invoke(cli, ["init", str(data_dir)])
    # Modify the config
    (data_dir / "lotsa.yaml").write_text("modified: true")
    runner.invoke(cli, ["init", str(data_dir)])

    # File should not be overwritten
    assert (data_dir / "lotsa.yaml").read_text() == "modified: true"


def test_init_default_target_is_home_lotsa(tmp_path, monkeypatch):
    """``lotsa init`` with no positional defaults to ``~/.lotsa``."""
    monkeypatch.setenv("HOME", str(tmp_path))
    runner = CliRunner()
    result = runner.invoke(cli, ["init"])

    assert result.exit_code == 0
    assert (tmp_path / ".lotsa" / "lotsa.yaml").exists()


def test_init_does_not_write_data_dir_in_yaml(tmp_path):
    """The generated lotsa.yaml omits ``data_dir`` — it's CLI-only."""
    data_dir = tmp_path / "lotsa"
    runner = CliRunner()
    runner.invoke(cli, ["init", str(data_dir)])

    raw = (data_dir / "lotsa.yaml").read_text()
    config = yaml.safe_load(raw)
    assert "data_dir" not in config, (
        f"lotsa.yaml must not contain data_dir (it lives inside data_dir, would be circular); got {list(config)}"
    )
    assert "tasks_dir" not in config, "tasks_dir is a retired field — generated YAML must not reference it"


def test_version():
    """--version flag works."""
    runner = CliRunner()
    result = runner.invoke(cli, ["--version"])
    assert result.exit_code == 0
    assert "0.1.0" in result.output


# --- lotsa build ---


def test_build_runs_docker(tmp_path, monkeypatch):
    """lotsa build calls docker build with the Dockerfile."""
    from unittest.mock import patch

    runner = CliRunner()
    with patch("lotsa.cli.subprocess.run") as mock_run:
        mock_run.return_value.returncode = 0
        result = runner.invoke(cli, ["build"])

    assert result.exit_code == 0
    assert "Built" in result.output
    cmd = mock_run.call_args[0][0]
    assert cmd[0] == "docker"
    assert cmd[1] == "build"
    assert "lotsa-agent:latest" in cmd


def test_build_custom_tag():
    """lotsa build --tag custom:v1 passes custom tag."""
    from unittest.mock import patch

    runner = CliRunner()
    with patch("lotsa.cli.subprocess.run") as mock_run:
        mock_run.return_value.returncode = 0
        result = runner.invoke(cli, ["build", "--tag", "my-image:v2"])

    assert result.exit_code == 0
    cmd = mock_run.call_args[0][0]
    assert "my-image:v2" in cmd


def test_init_includes_flow(tmp_path):
    """lotsa init includes flow in generated config."""
    data_dir = tmp_path / "lotsa"
    runner = CliRunner()
    runner.invoke(cli, ["init", str(data_dir)])

    config = yaml.safe_load((data_dir / "lotsa.yaml").read_text())
    # ADR-034 §2 / R6 — the scaffold writes the chat-first default explicitly.
    assert config["flow"] == "chat"


# --- lotsa serve --- (no config found error path)


def test_serve_errors_when_no_lotsa_yaml_found(tmp_path):
    """``lotsa serve`` against an empty data_dir errors instead of starting
    with all-default config pointing at a non-existent ``lotsa.db``.
    """
    runner = CliRunner()
    result = runner.invoke(cli, ["serve", "--data-dir", str(tmp_path / "empty")])

    assert result.exit_code == 1, f"expected failure exit code, got {result.exit_code}; output={result.output!r}"
    assert "no lotsa.yaml found" in result.output
    assert "lotsa init" in result.output


# --- lotsa serve --process / --flow aliasing ---


def _capture_serve_flow(extra_args: list[str], tmp_path):
    """Drive ``lotsa serve`` far enough to capture the ``flow`` value
    that ``LotsaConfig.load`` ends up receiving, then short-circuit.

    Returns ``(result, captured_flow)`` where ``captured_flow`` is the
    ``flow=`` kwarg LotsaConfig.load was called with. We patch
    ``LotsaConfig.load`` to raise so ``serve`` never reaches the
    uvicorn startup path — the alias-resolution logic at the top of
    ``serve`` runs to completion before the patched call fires.
    """
    from typing import Any
    from unittest.mock import patch

    captured: dict[str, Any] = {}

    class _Halt(SystemExit):
        pass

    def fake_load(**kwargs):
        captured["flow"] = kwargs.get("flow")
        raise _Halt(0)

    runner = CliRunner()
    with patch("lotsa.config.LotsaConfig.load", side_effect=fake_load):
        result = runner.invoke(cli, ["serve", "--data-dir", str(tmp_path), *extra_args])
    return result, captured


def test_serve_uses_process_over_flow_and_warns_when_they_disagree(tmp_path):
    """``--process`` wins over ``--flow`` and a warning goes to stderr."""
    result, captured = _capture_serve_flow(["--flow", "full", "--process", "marketing"], tmp_path)
    assert captured.get("flow") == "marketing", (
        f"--process should override --flow as the resolved selection; got flow={captured.get('flow')!r}"
    )
    # CliRunner combines stdout + stderr into result.output by default.
    assert "Both --flow=" in result.output
    assert "--process='marketing'" in result.output


def test_serve_silent_when_flow_and_process_match(tmp_path):
    """When ``--flow`` and ``--process`` carry the same value, no warning."""
    result, captured = _capture_serve_flow(["--flow", "full", "--process", "full"], tmp_path)
    assert captured.get("flow") == "full"
    # The conflict-warning line must NOT appear (other warnings about
    # missing claude credentials may; we check only the conflict line).
    assert "Both --flow=" not in result.output


def test_serve_uses_process_when_only_process_given(tmp_path):
    """Only ``--process`` given: it becomes the selected flow."""
    _, captured = _capture_serve_flow(["--process", "marketing"], tmp_path)
    assert captured.get("flow") == "marketing"


def test_serve_uses_flow_when_only_flow_given(tmp_path):
    """Only ``--flow`` given: behaves as today (no warning)."""
    result, captured = _capture_serve_flow(["--flow", "full"], tmp_path)
    assert captured.get("flow") == "full"
    assert "Both --flow=" not in result.output
