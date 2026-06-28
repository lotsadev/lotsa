"""Tests for the startup preflight checks and the `lotsa serve` gate (ADR-036 §2)."""

from __future__ import annotations

import pytest
from click.testing import CliRunner

from lotsa import cli as cli_module
from lotsa import preflight
from lotsa.config import LotsaConfig
from lotsa.preflight import CheckResult, Severity


def _cfg(**kw) -> LotsaConfig:
    return LotsaConfig(**kw)


# ── individual checks ──────────────────────────────────────────────────────


class TestAgentAuth:
    def test_sdk_runner_without_key_is_fatal(self, monkeypatch):
        monkeypatch.delenv("ANTHROPIC_API_KEY", raising=False)
        r = preflight.check_agent_auth(_cfg(runner="claude-agent-sdk"))
        assert r.severity is Severity.FATAL and r.ok is False

    def test_sdk_runner_with_key_passes(self, monkeypatch):
        monkeypatch.setenv("ANTHROPIC_API_KEY", "sk-test")
        r = preflight.check_agent_auth(_cfg(runner="claude-agent-sdk"))
        assert r.severity is Severity.FATAL and r.ok is True

    def test_cli_runner_without_env_is_warn_not_fatal(self, monkeypatch):
        # Keychain auth is undetectable from env — must not hard-fail the CLI runner.
        monkeypatch.delenv("ANTHROPIC_API_KEY", raising=False)
        monkeypatch.delenv("CLAUDE_CODE_OAUTH_TOKEN", raising=False)
        r = preflight.check_agent_auth(_cfg())
        assert r.severity is Severity.WARN and r.ok is False

    def test_cli_runner_with_oauth_passes(self, monkeypatch):
        monkeypatch.delenv("ANTHROPIC_API_KEY", raising=False)
        monkeypatch.setenv("CLAUDE_CODE_OAUTH_TOKEN", "tok")
        r = preflight.check_agent_auth(_cfg())
        assert r.ok is True


class TestClaudeCli:
    def test_missing_cli_is_fatal(self, monkeypatch):
        monkeypatch.setattr(preflight.shutil, "which", lambda _: None)
        r = preflight.check_claude_cli(_cfg())
        assert r.severity is Severity.FATAL and r.ok is False

    def test_present_cli_passes(self, monkeypatch):
        monkeypatch.setattr(preflight.shutil, "which", lambda name: "/usr/bin/claude" if name == "claude" else None)
        assert preflight.check_claude_cli(_cfg()).ok is True


class TestProjectRepo:
    def test_git_repo_passes(self, tmp_path):
        (tmp_path / ".git").mkdir()
        assert preflight.check_project_repo(_cfg(work_dir=tmp_path)).ok is True

    def test_non_git_dir_is_fatal(self, tmp_path):
        r = preflight.check_project_repo(_cfg(work_dir=tmp_path))
        assert r.severity is Severity.FATAL and r.ok is False

    def test_invalid_projects_config_is_fatal(self, tmp_path):
        # A declared project pointing at a non-existent path raises in resolution.
        r = preflight.check_project_repo(_cfg(projects={"app": {"path": str(tmp_path / "nope")}}))
        assert r.severity is Severity.FATAL and r.ok is False


class TestDashboardBundle:
    def _wire(self, monkeypatch, tmp_path):
        from lotsa.server import app as appmod

        monkeypatch.setattr(appmod, "_STATIC_DIR", tmp_path / "static")
        monkeypatch.setattr(appmod, "_FRONTEND_DIR", tmp_path / "frontend")
        return appmod

    def test_present_bundle_passes(self, tmp_path, monkeypatch):
        self._wire(monkeypatch, tmp_path)
        idx = tmp_path / "static/dist/index.html"
        idx.parent.mkdir(parents=True)
        idx.write_text("x")
        assert preflight.check_dashboard_bundle(_cfg()).ok is True

    def test_missing_but_buildable_passes(self, tmp_path, monkeypatch):
        self._wire(monkeypatch, tmp_path)
        (tmp_path / "frontend").mkdir()
        (tmp_path / "frontend/package.json").write_text("{}")
        monkeypatch.setattr(preflight.shutil, "which", lambda name: "/usr/bin/npm" if name == "npm" else None)
        assert preflight.check_dashboard_bundle(_cfg()).ok is True

    def test_missing_and_unbuildable_is_fatal(self, tmp_path, monkeypatch):
        self._wire(monkeypatch, tmp_path)  # no frontend source, no bundle
        monkeypatch.setattr(preflight.shutil, "which", lambda _: None)
        r = preflight.check_dashboard_bundle(_cfg())
        assert r.severity is Severity.FATAL and r.ok is False


class TestGithubToken:
    def test_missing_token_is_confirm(self, monkeypatch):
        monkeypatch.delenv("GITHUB_TOKEN", raising=False)
        r = preflight.check_github_token(_cfg())
        assert r.severity is Severity.CONFIRM and r.ok is False

    def test_present_token_passes(self, monkeypatch):
        monkeypatch.setenv("GITHUB_TOKEN", "ghp_x")
        assert preflight.check_github_token(_cfg()).ok is True


class TestGitIdentity:
    def test_unset_identity_is_warn(self, monkeypatch):
        monkeypatch.setattr(preflight, "_git_config", lambda _: None)
        r = preflight.check_git_identity(_cfg())
        assert r.severity is Severity.WARN and r.ok is False

    def test_set_identity_passes(self, monkeypatch):
        monkeypatch.setattr(preflight, "_git_config", lambda key: "Andrew" if key == "user.name" else "a@b.c")
        assert preflight.check_git_identity(_cfg()).ok is True


def test_format_line_symbols():
    ok = CheckResult("x", Severity.WARN, True, "fine")
    fatal = CheckResult("y", Severity.FATAL, False, "broken", remedy="fix it")
    assert preflight.format_line(ok).startswith("✔")
    out = preflight.format_line(fatal)
    assert out.startswith("✖") and "→ fix it" in out


# ── serve gate ─────────────────────────────────────────────────────────────


def _results(*specs) -> list[CheckResult]:
    return [CheckResult(n, sev, ok, n) for (n, sev, ok) in specs]


class TestPreflightGate:
    def test_fatal_aborts(self, monkeypatch):
        monkeypatch.setattr(cli_module, "run_all_checks", lambda c: _results(("a", Severity.FATAL, False)))
        with pytest.raises(SystemExit):
            cli_module._run_preflight_gate(_cfg(), assume_yes=False, isatty=True)

    def test_all_ok_passes(self, monkeypatch):
        monkeypatch.setattr(cli_module, "run_all_checks", lambda c: _results(("a", Severity.FATAL, True)))
        cli_module._run_preflight_gate(_cfg(), assume_yes=False, isatty=True)  # no raise

    def test_confirm_with_assume_yes_passes(self, monkeypatch):
        monkeypatch.setattr(cli_module, "run_all_checks", lambda c: _results(("gh", Severity.CONFIRM, False)))
        cli_module._run_preflight_gate(_cfg(), assume_yes=True, isatty=True)  # no raise

    def test_confirm_non_tty_fails_closed(self, monkeypatch):
        monkeypatch.setattr(cli_module, "run_all_checks", lambda c: _results(("gh", Severity.CONFIRM, False)))
        with pytest.raises(SystemExit):
            cli_module._run_preflight_gate(_cfg(), assume_yes=False, isatty=False)

    def test_confirm_tty_accepted(self, monkeypatch):
        monkeypatch.setattr(cli_module, "run_all_checks", lambda c: _results(("gh", Severity.CONFIRM, False)))
        monkeypatch.setattr(cli_module.click, "confirm", lambda *a, **k: True)
        cli_module._run_preflight_gate(_cfg(), assume_yes=False, isatty=True)  # no raise

    def test_confirm_tty_declined_aborts(self, monkeypatch):
        monkeypatch.setattr(cli_module, "run_all_checks", lambda c: _results(("gh", Severity.CONFIRM, False)))
        monkeypatch.setattr(cli_module.click, "confirm", lambda *a, **k: False)
        with pytest.raises(SystemExit):
            cli_module._run_preflight_gate(_cfg(), assume_yes=False, isatty=True)


# ── doctor command ─────────────────────────────────────────────────────────


class TestDoctorCommand:
    def test_exit_zero_when_clean(self, tmp_path, monkeypatch):
        monkeypatch.setattr(cli_module, "run_all_checks", lambda c: _results(("a", Severity.FATAL, True)))
        result = CliRunner().invoke(cli_module.cli, ["doctor", "--data-dir", str(tmp_path)])
        assert result.exit_code == 0
        assert "good to go" in result.output

    def test_exit_one_on_fatal(self, tmp_path, monkeypatch):
        monkeypatch.setattr(cli_module, "run_all_checks", lambda c: _results(("a", Severity.FATAL, False)))
        result = CliRunner().invoke(cli_module.cli, ["doctor", "--data-dir", str(tmp_path)])
        assert result.exit_code == 1
        assert "blocking issue" in result.output

    def test_confirm_only_is_ready(self, tmp_path, monkeypatch):
        monkeypatch.setattr(cli_module, "run_all_checks", lambda c: _results(("gh", Severity.CONFIRM, False)))
        result = CliRunner().invoke(cli_module.cli, ["doctor", "--data-dir", str(tmp_path)])
        assert result.exit_code == 0
        assert "need confirmation" in result.output


def test_env_truthy(monkeypatch):
    monkeypatch.setenv("X", "1")
    assert cli_module._env_truthy("X") is True
    monkeypatch.setenv("X", "0")
    assert cli_module._env_truthy("X") is False
    monkeypatch.delenv("X", raising=False)
    assert cli_module._env_truthy("X") is False
