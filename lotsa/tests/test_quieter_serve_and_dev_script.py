"""Acceptance tests for the 'quieter lotsa serve + dev startup script' change.

These tests verify the acceptance criteria from the implementation plan:

  * lotsa/cli.py no longer registers the --no-open Click option, no longer
    accepts a no_open parameter, and no longer imports or calls webbrowser.
  * lotsa/README.md no longer documents the removed --no-open flag.
  * scripts/dev.sh exists, is executable, has the right shebang, performs an
    editable install, and execs `lotsa serve` with argument passthrough.
  * The Makefile has a `dev` target wired up and declares it .PHONY.

Tests in this file fail until the change is implemented. Regression-style
checks (e.g. "serve is still registered", "--port is still a flag") are
included to catch over-deletion.
"""

from __future__ import annotations

import stat
from pathlib import Path

from click.testing import CliRunner

REPO_ROOT = Path(__file__).resolve().parents[2]


def _read(relative_path: str) -> str:
    return (REPO_ROOT / relative_path).read_text()


# ---------------------------------------------------------------------------
# lotsa serve: --no-open removal
# ---------------------------------------------------------------------------


def test_serve_help_does_not_mention_no_open_flag():
    """`lotsa serve --help` output no longer lists --no-open."""
    from lotsa.cli import cli

    result = CliRunner().invoke(cli, ["serve", "--help"])
    assert result.exit_code == 0
    assert "--no-open" not in result.output


def test_serve_help_still_lists_port_flag():
    """Regression: --port is still a flag on `lotsa serve`."""
    from lotsa.cli import cli

    result = CliRunner().invoke(cli, ["serve", "--help"])
    assert result.exit_code == 0
    assert "--port" in result.output


def test_serve_help_still_lists_host_flag():
    """Regression: --host is still a flag on `lotsa serve`."""
    from lotsa.cli import cli

    result = CliRunner().invoke(cli, ["serve", "--help"])
    assert result.exit_code == 0
    assert "--host" in result.output


def test_serve_command_signature_has_no_no_open_parameter():
    """The `serve` callback no longer accepts a `no_open` parameter."""
    import inspect

    from lotsa.cli import serve

    # Click wraps the function but preserves the underlying callable.
    callback = getattr(serve, "callback", serve)
    params = inspect.signature(callback).parameters
    assert "no_open" not in params


def test_serve_command_is_still_registered():
    """Regression: `serve` survives the change."""
    from lotsa.cli import cli

    assert "serve" in cli.commands


# ---------------------------------------------------------------------------
# lotsa/cli.py: webbrowser removal
# ---------------------------------------------------------------------------


def test_cli_source_does_not_import_webbrowser():
    """lotsa/cli.py no longer imports the webbrowser module."""
    text = _read("lotsa/cli.py")
    assert "import webbrowser" not in text


def test_cli_source_does_not_reference_webbrowser():
    """lotsa/cli.py contains no reference to webbrowser at all.

    This also covers any future regression where webbrowser is added back
    at module scope — a source-text check is strictly stronger than a
    `hasattr(lotsa.cli, "webbrowser")` namespace check, since the old
    auto-open block imported webbrowser *inside* the serve() function
    body, which never binds the name onto the module's __dict__.
    """
    text = _read("lotsa/cli.py")
    assert "webbrowser" not in text


def test_cli_source_does_not_schedule_threading_timer_for_browser():
    """The threading.Timer(...) auto-open block has been removed."""
    text = _read("lotsa/cli.py")
    assert "threading.Timer" not in text


# ---------------------------------------------------------------------------
# lotsa/README.md: --no-open documentation removal
# ---------------------------------------------------------------------------


def test_readme_does_not_mention_no_open_flag():
    """The lotsa README no longer documents the --no-open flag."""
    assert "--no-open" not in _read("lotsa/README.md")


def test_readme_does_not_promise_browser_auto_launch():
    """The README no longer claims `lotsa serve` opens the browser for you."""
    text = _read("lotsa/README.md")
    assert "Opens the web UI in your browser." not in text


# ---------------------------------------------------------------------------
# scripts/dev.sh: shell launcher
# ---------------------------------------------------------------------------


def _dev_script_path() -> Path:
    return REPO_ROOT / "scripts" / "dev.sh"


def test_scripts_directory_exists():
    """The repo root has a scripts/ directory."""
    assert (REPO_ROOT / "scripts").is_dir()


def test_dev_script_exists():
    """scripts/dev.sh exists on disk."""
    assert _dev_script_path().is_file()


def test_dev_script_is_executable():
    """scripts/dev.sh is executable (user-execute bit set)."""
    mode = _dev_script_path().stat().st_mode
    assert mode & stat.S_IXUSR, "scripts/dev.sh is not user-executable"


def test_dev_script_has_bash_shebang():
    """scripts/dev.sh starts with `#!/usr/bin/env bash`."""
    first_line = _dev_script_path().read_text().splitlines()[0]
    assert first_line == "#!/usr/bin/env bash"


def test_dev_script_uses_strict_mode():
    """scripts/dev.sh enables `set -euo pipefail`."""
    text = _dev_script_path().read_text()
    assert "set -euo pipefail" in text


def test_dev_script_performs_editable_install():
    """scripts/dev.sh runs `pip install -e .` so local edits take effect."""
    text = _dev_script_path().read_text()
    assert "pip install -e ." in text


def test_dev_script_execs_lotsa_serve_with_passthrough():
    """scripts/dev.sh execs `lotsa serve "$@"` so signals and args propagate."""
    text = _dev_script_path().read_text()
    assert 'exec lotsa serve "$@"' in text


def test_dev_script_is_syntactically_valid_bash():
    """`bash -n scripts/dev.sh` parses without syntax errors."""
    import subprocess

    result = subprocess.run(
        ["bash", "-n", str(_dev_script_path())],
        capture_output=True,
        text=True,
    )
    assert result.returncode == 0, f"bash -n failed: {result.stderr}"


def test_dev_script_resolves_repo_root_from_its_own_location():
    """The script derives the repo root from $BASH_SOURCE, not the caller's cwd.

    This is what makes `cd /tmp && /path/to/scripts/dev.sh` still work.
    """
    text = _dev_script_path().read_text()
    assert "BASH_SOURCE" in text


# ---------------------------------------------------------------------------
# Makefile: `dev` target
# ---------------------------------------------------------------------------


def _makefile_text() -> str:
    return _read("Makefile")


def test_makefile_declares_dev_phony():
    """The Makefile's .PHONY list includes `dev`."""
    text = _makefile_text()
    # Find the first .PHONY: line and check its targets.
    phony_lines = [line for line in text.splitlines() if line.startswith(".PHONY:")]
    assert phony_lines, "Makefile has no .PHONY declaration"
    combined = " ".join(phony_lines)
    # Use word-boundary-style check so `dev-up` doesn't satisfy this.
    targets = combined.replace(".PHONY:", "").split()
    assert "dev" in targets


def test_makefile_has_dev_target():
    """The Makefile defines a `dev:` target."""
    text = _makefile_text()
    # Match `dev:` at start of line, not `dev-up:` or `dev-down:`.
    target_lines = [line for line in text.splitlines() if line.startswith("dev:") or line.startswith("dev :")]
    assert target_lines, "Makefile has no `dev:` target"


def test_makefile_dev_target_invokes_dev_script():
    """The `dev` recipe invokes ./scripts/dev.sh."""
    text = _makefile_text()
    # Find the `dev:` target and the recipe lines that follow it. Recipe lines
    # start with a tab in Make.
    lines = text.splitlines()
    recipe: list[str] = []
    in_dev_target = False
    for line in lines:
        if line.startswith("dev:") or line.startswith("dev :"):
            in_dev_target = True
            continue
        if in_dev_target:
            if line.startswith("\t"):
                recipe.append(line)
            elif line.strip() == "":
                # Blank line is allowed inside the recipe block in some styles;
                # but a non-tab, non-blank line ends the recipe.
                continue
            else:
                break
    assert recipe, "No recipe lines found under `dev:` target"
    joined = "\n".join(recipe)
    assert "./scripts/dev.sh" in joined


def test_makefile_dev_target_runs_without_make_parse_errors():
    """`make -n dev` parses the Makefile and prints a recipe."""
    import subprocess

    result = subprocess.run(
        ["make", "-n", "dev"],
        cwd=REPO_ROOT,
        capture_output=True,
        text=True,
    )
    assert result.returncode == 0, f"make -n dev failed: {result.stderr}"
    assert "./scripts/dev.sh" in result.stdout


def test_makefile_still_declares_existing_phony_targets():
    """Regression: the core .PHONY targets are not lost when `dev` is added."""
    text = _makefile_text()
    phony_lines = [line for line in text.splitlines() if line.startswith(".PHONY:")]
    combined = " ".join(phony_lines)
    targets = set(combined.replace(".PHONY:", "").split())
    for required in {
        "dev",
        "test",
        "lint",
        "format",
        "typecheck",
        "frontend-dev",
        "frontend-build",
    }:
        assert required in targets, f"Expected .PHONY target {required!r} missing"
