"""Tests for Rigg GitRunner."""

from unittest.mock import MagicMock, patch

import pytest

from rigg.git import GitRunner, TokenCredentialStrategy


class TestTokenCredentialStrategy:
    def test_env_contains_git_config(self):
        strategy = TokenCredentialStrategy(token="ghp_test123")
        env = strategy.env()
        assert "GIT_TERMINAL_PROMPT" in env
        assert env["GIT_TERMINAL_PROMPT"] == "0"
        # Should configure a credential helper
        assert "GIT_CONFIG_COUNT" in env


class TestGitRunner:
    @pytest.mark.asyncio
    async def test_run_passes_credentials(self, tmp_path):
        strategy = MagicMock()
        strategy.env.return_value = {"GIT_TOKEN": "abc"}

        runner = GitRunner(repo_url="https://github.com/test/repo.git", credentials=strategy)

        mock_result = MagicMock()
        mock_result.returncode = 0
        mock_result.stdout = ""
        mock_result.stderr = ""

        with patch("rigg.git.subprocess.run", return_value=mock_result) as mock_run:
            await runner.run(["git", "status"], cwd=tmp_path)

        call_kwargs = mock_run.call_args[1]
        assert call_kwargs["env"]["GIT_TOKEN"] == "abc"

    @pytest.mark.asyncio
    async def test_setup_new_branch(self, tmp_path):
        strategy = MagicMock()
        strategy.env.return_value = {}

        runner = GitRunner(repo_url="https://github.com/test/repo.git", credentials=strategy)

        mock_result = MagicMock()
        mock_result.returncode = 0
        mock_result.stdout = ""
        mock_result.stderr = ""

        with patch("rigg.git.subprocess.run", return_value=mock_result) as mock_run:
            work_dir, branch = await runner.setup_new_branch(tmp_path, base="main")

        # Should have called git clone and git checkout -b
        commands = [call[0][0] for call in mock_run.call_args_list]
        assert any("clone" in cmd for cmd in commands)
        assert any("checkout" in cmd for cmd in commands)
        assert branch.startswith("rigg/")

    @pytest.mark.asyncio
    async def test_setup_existing_branch(self, tmp_path):
        work_dir = tmp_path / "repo"
        work_dir.mkdir()

        strategy = MagicMock()
        strategy.env.return_value = {}

        runner = GitRunner(repo_url="https://github.com/test/repo.git", credentials=strategy)

        mock_result = MagicMock()
        mock_result.returncode = 0
        mock_result.stdout = ""
        mock_result.stderr = ""

        with patch("rigg.git.subprocess.run", return_value=mock_result):
            result = await runner.setup_existing_branch(work_dir, "feature/test")

        assert result == work_dir
