"""Tests for the credential scrubber and least-privilege agent env (audit #1-#4, #6)."""

from __future__ import annotations

import subprocess

import pytest

from rigg.scrub import scrub_secrets


class TestScrubSecrets:
    def test_live_env_value_is_redacted(self, monkeypatch):
        monkeypatch.setenv("GITHUB_TOKEN", "supersecrettokenvalue123")
        out = scrub_secrets("remote url has supersecrettokenvalue123 in it")
        assert "supersecrettokenvalue123" not in out
        assert "***" in out

    def test_token_shaped_strings_redacted_without_env(self, monkeypatch):
        for var in ("GITHUB_TOKEN", "GH_TOKEN", "ANTHROPIC_API_KEY", "CLAUDE_CODE_OAUTH_TOKEN"):
            monkeypatch.delenv(var, raising=False)
        for tok in ("ghp_" + "a" * 36, "github_pat_" + "A1" * 15, "sk-ant-" + "x" * 30):
            out = scrub_secrets(f"leak {tok} here")
            assert tok not in out
            assert "***" in out

    def test_short_env_value_is_not_used_as_a_needle(self, monkeypatch):
        # A short/empty value must not blank out unrelated text.
        monkeypatch.setenv("GITHUB_TOKEN", "abc")
        assert scrub_secrets("abc def") == "abc def"

    def test_clean_text_is_unchanged(self, monkeypatch):
        for var in ("GITHUB_TOKEN", "GH_TOKEN", "ANTHROPIC_API_KEY", "CLAUDE_CODE_OAUTH_TOKEN"):
            monkeypatch.delenv(var, raising=False)
        assert scrub_secrets("nothing to redact here") == "nothing to redact here"

    def test_empty(self):
        assert scrub_secrets("") == ""


class TestAgentSubprocessEnv:
    def test_github_token_stripped_auth_kept(self, monkeypatch, tmp_path):
        from rigg.agent_runner import ClaudeCodeRunner

        monkeypatch.setenv("GITHUB_TOKEN", "ghp_" + "z" * 36)
        monkeypatch.setenv("GH_TOKEN", "ghp_other")
        monkeypatch.setenv("ANTHROPIC_API_KEY", "sk-ant-keepme")
        env = ClaudeCodeRunner()._subprocess_env(tmp_path)
        assert "GITHUB_TOKEN" not in env  # agent never pushes — least privilege
        assert "GH_TOKEN" not in env
        assert env["ANTHROPIC_API_KEY"] == "sk-ant-keepme"  # still needed to call claude
        assert env["PWD"] == str(tmp_path)


class TestGitRunnerScrub:
    async def test_called_process_error_is_scrubbed(self, monkeypatch, tmp_path):
        from rigg.git import GitRunner, TokenCredentialStrategy

        token = "ghp_" + "q" * 36

        def _boom(*_a, **_k):
            raise subprocess.CalledProcessError(1, ["git", "x"], output="", stderr=f"fatal: auth {token} rejected")

        monkeypatch.setattr(subprocess, "run", _boom)
        runner = GitRunner("https://example.com/repo.git", TokenCredentialStrategy(token))
        with pytest.raises(subprocess.CalledProcessError) as ei:
            await runner.run(["git", "status"], tmp_path)
        assert token not in (ei.value.stderr or "")
        assert "***" in ei.value.stderr
