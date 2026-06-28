"""ADR-038 Phase 1 — host-sandbox preflight check + wiring."""

from __future__ import annotations

from lotsa import preflight
from lotsa.config import LotsaConfig
from lotsa.preflight import Severity


class TestCheckHostSandbox:
    def test_macos_seatbelt_ok(self, monkeypatch):
        monkeypatch.setattr(preflight.sys, "platform", "darwin")
        r = preflight.check_host_sandbox(LotsaConfig())
        assert r.severity is Severity.FATAL and r.ok is True

    def test_linux_native_is_fatal_and_steers_to_docker(self, monkeypatch):
        # Claude's native sandbox doesn't reliably start on Linux → don't claim
        # a false green; require --docker (or the explicit opt-out).
        monkeypatch.setattr(preflight.sys, "platform", "linux")
        r = preflight.check_host_sandbox(LotsaConfig())
        assert r.severity is Severity.FATAL and r.ok is False
        assert "--docker" in (r.remedy or "")

    def test_override_is_warn_not_fatal(self, monkeypatch):
        # --dangerously-skip-permissions: unsandboxed by explicit choice.
        monkeypatch.setattr(preflight.sys, "platform", "linux")
        monkeypatch.setattr(preflight.shutil, "which", lambda _n: None)
        r = preflight.check_host_sandbox(LotsaConfig(skip_permissions=True))
        assert r.severity is Severity.WARN and r.ok is False

    def test_unsupported_platform_is_fatal(self, monkeypatch):
        monkeypatch.setattr(preflight.sys, "platform", "win32")
        assert preflight.check_host_sandbox(LotsaConfig()).ok is False


class TestSandboxWiring:
    def test_included_for_native_runner(self, monkeypatch):
        monkeypatch.setattr(preflight.sys, "platform", "darwin")
        names = [r.name for r in preflight.run_all_checks(LotsaConfig())]
        assert "host-sandbox" in names

    def test_excluded_in_docker_mode(self, monkeypatch):
        import subprocess

        monkeypatch.setattr(preflight.shutil, "which", lambda _n: "/usr/bin/docker")
        monkeypatch.setattr(preflight.subprocess, "run", lambda *a, **k: subprocess.CompletedProcess(a, 0, "", ""))
        names = [r.name for r in preflight.run_all_checks(LotsaConfig(docker=True))]
        assert "host-sandbox" not in names  # docker IS the sandbox

    def test_excluded_for_sdk_runner(self, monkeypatch):
        monkeypatch.setattr(preflight.sys, "platform", "darwin")
        names = [r.name for r in preflight.run_all_checks(LotsaConfig(runner="claude-agent-sdk"))]
        assert "host-sandbox" not in names  # ADR-038 Phase 3


class TestConfigSkipPermissions:
    def test_default_false_and_yaml_ignored(self, tmp_path, monkeypatch):
        # Field defaults False, and a yaml value is ignored (CLI-only, per launch).
        assert LotsaConfig().skip_permissions is False
        yaml = tmp_path / "lotsa.yaml"
        yaml.write_text("skip_permissions: true\nmodel: sonnet\n")
        cfg = LotsaConfig.load(config_path=yaml)
        assert cfg.skip_permissions is False
