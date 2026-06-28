"""Tests that verify AgentResult carries token fields and runners populate them.

With `--output-format json` the Claude CLI writes a JSON envelope to stdout:

    {"result": "<text>", "session_id": "...", "cost_usd": 0.012,
     "usage": {"input_tokens": 840, "output_tokens": 400}}

Runners must:
  1. Add `--output-format json` to the CLI command
  2. Parse the envelope from stdout
  3. Set AgentResult.stdout to the extracted `result` text (not raw JSON)
  4. Populate input_tokens, output_tokens, cost_usd from the envelope
  5. Populate session_id from the envelope (already existing field)

These tests fail until:
- `rigg.models.AgentResult` gains `input_tokens` and `output_tokens` fields
- Both runners add `--output-format json` and parse the envelope
"""

from __future__ import annotations

import json
from unittest.mock import MagicMock, patch

import pytest

from lotsa.docker_runner import DockerAgentRunner
from rigg.agent_runner import ClaudeCodeRunner
from rigg.models import AgentResult

# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def _make_envelope(
    text: str = "Agent response text.",
    input_tokens: int = 840,
    output_tokens: int = 400,
    cost_usd: float = 0.012,
    session_id: str = "ses-abc",
) -> str:
    return json.dumps(
        {
            "type": "result",
            "result": text,
            "session_id": session_id,
            "total_cost_usd": cost_usd,
            "usage": {"input_tokens": input_tokens, "output_tokens": output_tokens},
        }
    )


def _make_envelope_no_usage(text: str = "Done.") -> str:
    return json.dumps({"type": "result", "result": text, "session_id": "ses-1"})


# ---------------------------------------------------------------------------
# AgentResult dataclass — token fields must exist with None defaults
# ---------------------------------------------------------------------------


class TestAgentResultTokenFields:
    def test_input_tokens_defaults_to_none(self):
        result = AgentResult(success=True, stdout="", stderr="", return_code=0, duration_ms=0)
        assert result.input_tokens is None

    def test_output_tokens_defaults_to_none(self):
        result = AgentResult(success=True, stdout="", stderr="", return_code=0, duration_ms=0)
        assert result.output_tokens is None

    def test_can_construct_with_token_values(self):
        result = AgentResult(
            success=True,
            stdout="",
            stderr="",
            return_code=0,
            duration_ms=100,
            input_tokens=840,
            output_tokens=400,
        )
        assert result.input_tokens == 840
        assert result.output_tokens == 400

    def test_cost_usd_field_still_present(self):
        result = AgentResult(success=True, stdout="", stderr="", return_code=0, duration_ms=0)
        assert result.cost_usd is None


# ---------------------------------------------------------------------------
# ClaudeCodeRunner — uses --output-format json and parses envelope
# ---------------------------------------------------------------------------


class TestClaudeCodeRunnerJsonMode:
    @pytest.mark.asyncio
    async def test_output_format_json_flag_in_command(self, tmp_path):
        runner = ClaudeCodeRunner(model="sonnet")
        mock_proc = MagicMock()
        mock_proc.returncode = 0
        mock_proc.stdout = _make_envelope()
        mock_proc.stderr = ""

        with patch("rigg.agent_runner.subprocess.run", return_value=mock_proc) as mock_run:
            await runner.run("sys", "usr", tmp_path)

        cmd = mock_run.call_args[0][0]
        assert "--output-format" in cmd
        idx = cmd.index("--output-format")
        assert cmd[idx + 1] == "json"

    @pytest.mark.asyncio
    async def test_stdout_set_to_extracted_text_not_raw_json(self, tmp_path):
        """result.stdout must be the text from envelope['result'], not the raw JSON."""
        runner = ClaudeCodeRunner(model="sonnet")
        mock_proc = MagicMock()
        mock_proc.returncode = 0
        mock_proc.stdout = _make_envelope(text="The answer is 42.")
        mock_proc.stderr = ""

        with patch("rigg.agent_runner.subprocess.run", return_value=mock_proc):
            result = await runner.run("sys", "usr", tmp_path)

        assert result.stdout == "The answer is 42."

    @pytest.mark.asyncio
    async def test_populates_input_tokens(self, tmp_path):
        runner = ClaudeCodeRunner(model="sonnet")
        mock_proc = MagicMock()
        mock_proc.returncode = 0
        mock_proc.stdout = _make_envelope(input_tokens=840)
        mock_proc.stderr = ""

        with patch("rigg.agent_runner.subprocess.run", return_value=mock_proc):
            result = await runner.run("sys", "usr", tmp_path)

        assert result.input_tokens == 840

    @pytest.mark.asyncio
    async def test_populates_output_tokens(self, tmp_path):
        runner = ClaudeCodeRunner(model="sonnet")
        mock_proc = MagicMock()
        mock_proc.returncode = 0
        mock_proc.stdout = _make_envelope(output_tokens=400)
        mock_proc.stderr = ""

        with patch("rigg.agent_runner.subprocess.run", return_value=mock_proc):
            result = await runner.run("sys", "usr", tmp_path)

        assert result.output_tokens == 400

    @pytest.mark.asyncio
    async def test_populates_cost_usd(self, tmp_path):
        runner = ClaudeCodeRunner(model="sonnet")
        mock_proc = MagicMock()
        mock_proc.returncode = 0
        mock_proc.stdout = _make_envelope(cost_usd=0.012)
        mock_proc.stderr = ""

        with patch("rigg.agent_runner.subprocess.run", return_value=mock_proc):
            result = await runner.run("sys", "usr", tmp_path)

        assert result.cost_usd == pytest.approx(0.012)

    @pytest.mark.asyncio
    async def test_populates_session_id_from_envelope(self, tmp_path):
        runner = ClaudeCodeRunner(model="sonnet")
        mock_proc = MagicMock()
        mock_proc.returncode = 0
        mock_proc.stdout = _make_envelope(session_id="ses-xyz")
        mock_proc.stderr = ""

        with patch("rigg.agent_runner.subprocess.run", return_value=mock_proc):
            result = await runner.run("sys", "usr", tmp_path)

        assert result.session_id == "ses-xyz"

    @pytest.mark.asyncio
    async def test_tokens_none_when_no_usage_in_envelope(self, tmp_path):
        runner = ClaudeCodeRunner(model="sonnet")
        mock_proc = MagicMock()
        mock_proc.returncode = 0
        mock_proc.stdout = _make_envelope_no_usage()
        mock_proc.stderr = ""

        with patch("rigg.agent_runner.subprocess.run", return_value=mock_proc):
            result = await runner.run("sys", "usr", tmp_path)

        assert result.input_tokens is None
        assert result.output_tokens is None

    @pytest.mark.asyncio
    async def test_tokens_none_on_timeout(self, tmp_path):
        import subprocess

        runner = ClaudeCodeRunner()
        with patch("rigg.agent_runner.subprocess.run", side_effect=subprocess.TimeoutExpired("claude", 60)):
            result = await runner.run("sys", "usr", tmp_path, timeout_seconds=60)

        assert result.input_tokens is None
        assert result.output_tokens is None

    @pytest.mark.asyncio
    async def test_success_true_on_zero_returncode(self, tmp_path):
        runner = ClaudeCodeRunner(model="sonnet")
        mock_proc = MagicMock()
        mock_proc.returncode = 0
        mock_proc.stdout = _make_envelope()
        mock_proc.stderr = ""

        with patch("rigg.agent_runner.subprocess.run", return_value=mock_proc):
            result = await runner.run("sys", "usr", tmp_path)

        assert result.success is True

    @pytest.mark.asyncio
    async def test_failure_on_nonzero_returncode(self, tmp_path):
        runner = ClaudeCodeRunner(model="sonnet")
        mock_proc = MagicMock()
        mock_proc.returncode = 1
        mock_proc.stdout = ""
        mock_proc.stderr = "error"

        with patch("rigg.agent_runner.subprocess.run", return_value=mock_proc):
            result = await runner.run("sys", "usr", tmp_path)

        assert result.success is False


# ---------------------------------------------------------------------------
# DockerAgentRunner — same JSON envelope behaviour
# ---------------------------------------------------------------------------


class TestDockerAgentRunnerJsonMode:
    @pytest.mark.asyncio
    async def test_output_format_json_flag_in_command(self, tmp_path):
        runner = DockerAgentRunner(image="test:latest", model="sonnet")
        mock_proc = MagicMock()
        mock_proc.returncode = 0
        mock_proc.stdout = _make_envelope()
        mock_proc.stderr = ""

        with patch("lotsa.docker_runner.subprocess.run", return_value=mock_proc) as mock_run:
            await runner.run("sys", "usr", tmp_path)

        cmd = mock_run.call_args[0][0]
        assert "--output-format" in cmd
        idx = cmd.index("--output-format")
        assert cmd[idx + 1] == "json"

    @pytest.mark.asyncio
    async def test_stdout_set_to_extracted_text(self, tmp_path):
        runner = DockerAgentRunner(image="test:latest", model="sonnet")
        mock_proc = MagicMock()
        mock_proc.returncode = 0
        mock_proc.stdout = _make_envelope(text="Docker response.")
        mock_proc.stderr = ""

        with patch("lotsa.docker_runner.subprocess.run", return_value=mock_proc):
            result = await runner.run("sys", "usr", tmp_path)

        assert result.stdout == "Docker response."

    @pytest.mark.asyncio
    async def test_populates_input_tokens(self, tmp_path):
        runner = DockerAgentRunner(image="test:latest", model="sonnet")
        mock_proc = MagicMock()
        mock_proc.returncode = 0
        mock_proc.stdout = _make_envelope(input_tokens=500)
        mock_proc.stderr = ""

        with patch("lotsa.docker_runner.subprocess.run", return_value=mock_proc):
            result = await runner.run("sys", "usr", tmp_path)

        assert result.input_tokens == 500

    @pytest.mark.asyncio
    async def test_populates_output_tokens(self, tmp_path):
        runner = DockerAgentRunner(image="test:latest", model="sonnet")
        mock_proc = MagicMock()
        mock_proc.returncode = 0
        mock_proc.stdout = _make_envelope(output_tokens=200)
        mock_proc.stderr = ""

        with patch("lotsa.docker_runner.subprocess.run", return_value=mock_proc):
            result = await runner.run("sys", "usr", tmp_path)

        assert result.output_tokens == 200

    @pytest.mark.asyncio
    async def test_tokens_none_when_no_usage_in_envelope(self, tmp_path):
        runner = DockerAgentRunner(image="test:latest", model="sonnet")
        mock_proc = MagicMock()
        mock_proc.returncode = 0
        mock_proc.stdout = _make_envelope_no_usage()
        mock_proc.stderr = ""

        with patch("lotsa.docker_runner.subprocess.run", return_value=mock_proc):
            result = await runner.run("sys", "usr", tmp_path)

        assert result.input_tokens is None
        assert result.output_tokens is None
