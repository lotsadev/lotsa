"""Docker-mode preflight check (ADR-036) — daemon + agent-image readiness."""

from __future__ import annotations

import subprocess

from lotsa import preflight
from lotsa.config import LotsaConfig
from lotsa.preflight import Severity


def _fake_run(info_rc: int = 0, inspect_rc: int = 0):
    def _run(cmd, **_kw):
        rc = 0
        if cmd[:2] == ["docker", "info"]:
            rc = info_rc
        elif cmd[:3] == ["docker", "image", "inspect"]:
            rc = inspect_rc
        return subprocess.CompletedProcess(cmd, rc, "", "")

    return _run


class TestDockerCheck:
    def test_skipped_when_docker_mode_off(self, monkeypatch):
        names = [r.name for r in preflight.run_all_checks(LotsaConfig(docker=False))]
        assert "docker" not in names

    def test_included_when_docker_mode_on(self, monkeypatch):
        monkeypatch.setattr(preflight.shutil, "which", lambda _n: "/usr/bin/docker")
        monkeypatch.setattr(preflight.subprocess, "run", _fake_run())
        names = [r.name for r in preflight.run_all_checks(LotsaConfig(docker=True))]
        assert "docker" in names

    def test_missing_cli_is_fatal(self, monkeypatch):
        monkeypatch.setattr(preflight.shutil, "which", lambda _n: None)
        r = preflight.check_docker(LotsaConfig(docker=True))
        assert r.severity is Severity.FATAL and r.ok is False

    def test_daemon_down_is_fatal(self, monkeypatch):
        monkeypatch.setattr(preflight.shutil, "which", lambda _n: "/usr/bin/docker")
        monkeypatch.setattr(preflight.subprocess, "run", _fake_run(info_rc=1))
        r = preflight.check_docker(LotsaConfig(docker=True))
        assert r.severity is Severity.FATAL and r.ok is False
        assert "daemon" in r.detail.lower()

    def test_image_present_passes(self, monkeypatch):
        monkeypatch.setattr(preflight.shutil, "which", lambda _n: "/usr/bin/docker")
        monkeypatch.setattr(preflight.subprocess, "run", _fake_run(inspect_rc=0))
        assert preflight.check_docker(LotsaConfig(docker=True)).ok is True

    def test_default_image_missing_is_fatal_with_build_remedy(self, monkeypatch):
        monkeypatch.setattr(preflight.shutil, "which", lambda _n: "/usr/bin/docker")
        monkeypatch.setattr(preflight.subprocess, "run", _fake_run(inspect_rc=1))
        r = preflight.check_docker(LotsaConfig(docker=True))  # default image
        assert r.severity is Severity.FATAL and r.ok is False
        assert "lotsa build" in (r.remedy or "")

    def test_custom_image_missing_is_warn(self, monkeypatch):
        monkeypatch.setattr(preflight.shutil, "which", lambda _n: "/usr/bin/docker")
        monkeypatch.setattr(preflight.subprocess, "run", _fake_run(inspect_rc=1))
        r = preflight.check_docker(LotsaConfig(docker=True, docker_image="myreg/agent:v1"))
        assert r.severity is Severity.WARN and r.ok is False


class TestDockerAgentAuth:
    """In docker mode, keychain can't cross the container — env auth is FATAL."""

    def test_no_env_auth_is_fatal_in_docker_mode(self, monkeypatch):
        for var in ("ANTHROPIC_API_KEY", "CLAUDE_CODE_OAUTH_TOKEN"):
            monkeypatch.delenv(var, raising=False)
        r = preflight.check_agent_auth(LotsaConfig(docker=True))
        assert r.severity is Severity.FATAL and r.ok is False

    def test_api_key_satisfies_docker_mode(self, monkeypatch):
        monkeypatch.delenv("CLAUDE_CODE_OAUTH_TOKEN", raising=False)
        monkeypatch.setenv("ANTHROPIC_API_KEY", "sk-ant-x")
        assert preflight.check_agent_auth(LotsaConfig(docker=True)).ok is True

    def test_oauth_token_satisfies_docker_mode(self, monkeypatch):
        monkeypatch.delenv("ANTHROPIC_API_KEY", raising=False)
        monkeypatch.setenv("CLAUDE_CODE_OAUTH_TOKEN", "tok")
        assert preflight.check_agent_auth(LotsaConfig(docker=True)).ok is True

    def test_native_mode_no_env_is_only_warn(self, monkeypatch):
        # Sanity: the native CLI runner still tolerates keychain-only auth.
        for var in ("ANTHROPIC_API_KEY", "CLAUDE_CODE_OAUTH_TOKEN"):
            monkeypatch.delenv(var, raising=False)
        assert preflight.check_agent_auth(LotsaConfig(docker=False)).severity is Severity.WARN
