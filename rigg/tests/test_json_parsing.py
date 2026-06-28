"""Tests for claude CLI JSON output parsing."""

from __future__ import annotations

import json

from rigg.parsing import parse_claude_output


def _make_result_event(
    result_text: str,
    session_id: str = "sess-123",
    cost_usd: float | None = None,
    usage: dict | None = None,
) -> dict:
    event: dict = {
        "type": "result",
        "subtype": "success",
        "result": result_text,
        "session_id": session_id,
        "duration_ms": 1000,
    }
    if cost_usd is not None:
        event["total_cost_usd"] = cost_usd
    if usage is not None:
        event["usage"] = usage
    return event


def _make_system_event() -> dict:
    return {"type": "system", "subtype": "init", "session_id": "sess-123"}


def _make_assistant_event(text: str) -> dict:
    return {"type": "assistant", "message": {"content": [{"type": "text", "text": text}]}}


class TestParseClaudeOutput:
    """Test parse_claude_output which extracts result, session_id, and usage."""

    def test_json_array_with_result(self):
        """Verbose mode: JSON array with system, assistant, and result events."""
        events = [_make_system_event(), _make_assistant_event("Hi"), _make_result_event("Hello!", "sess-abc")]
        raw = json.dumps(events)

        parsed = parse_claude_output(raw)
        assert parsed.stdout == "Hello!"
        assert parsed.session_id == "sess-abc"

    def test_single_json_object(self):
        """Non-verbose mode: single JSON result object."""
        event = _make_result_event("Done.", "sess-xyz")
        raw = json.dumps(event)

        parsed = parse_claude_output(raw)
        assert parsed.stdout == "Done."
        assert parsed.session_id == "sess-xyz"

    def test_malformed_json_falls_back(self):
        """Non-JSON output: falls back to raw stdout, no session_id."""
        raw = "This is plain text output"

        parsed = parse_claude_output(raw)
        assert parsed.stdout == raw
        assert parsed.session_id is None

    def test_json_array_without_result_event(self):
        """JSON array with no type=result entry: summarises from assistant text."""
        events = [_make_system_event(), _make_assistant_event("Hi from the agent")]
        raw = json.dumps(events)

        parsed = parse_claude_output(raw)
        # No result event → fall back to a bounded summary including the
        # last assistant message, not the raw stream.
        assert raw != parsed.stdout, "Expected a summary, not the raw stream"
        assert "Hi from the agent" in parsed.stdout
        assert parsed.session_id is None

    def test_empty_string(self):
        """Empty stdout: no crash, returns empty."""
        parsed = parse_claude_output("")
        assert parsed.stdout == ""
        assert parsed.session_id is None

    def test_partial_json(self):
        """Truncated JSON: bounded fallback summary, not the raw stream verbatim."""
        raw = '[{"type": "system"'

        parsed = parse_claude_output(raw)
        assert len(parsed.stdout.encode("utf-8")) <= 20_000
        assert parsed.session_id is None


class TestParseClaudeOutputSummary:
    """Bounded-summary behaviour for verbose streams that exit abnormally.

    The runner persists ``parsed.stdout`` to the chat audit log as the step's
    ``output`` message. When the agent terminates abnormally (budget exit,
    timeout, parse failure), the parser must NEVER return the raw multi-MB
    verbose stream — that crashes the React UI's JSON parse path. Instead,
    the parser produces a structured summary capped at 20 KB.
    """

    _MAX_BYTES = 20_000

    def test_summary_includes_final_assistant_message_when_no_result(self):
        """Stream with no result event → summary keeps the last assistant text."""
        events = [
            _make_system_event(),
            _make_assistant_event("I started analysing the code"),
            _make_assistant_event("Final answer: refactored auth.py"),
        ]
        raw = json.dumps(events)

        parsed = parse_claude_output(raw)
        # The summary must be a derived artifact, not the raw stream verbatim.
        assert parsed.stdout != raw, "Expected a summary, not the raw JSON stream"
        # And it must surface the final assistant message.
        assert "Final answer: refactored auth.py" in parsed.stdout

    def test_summary_includes_tool_call_counts(self):
        """Summary surfaces tool usage so operators can see what the agent did."""
        events = [
            _make_system_event(),
            {"type": "assistant", "message": {"content": [{"type": "tool_use", "name": "Edit", "id": "1"}]}},
            {"type": "assistant", "message": {"content": [{"type": "tool_use", "name": "Edit", "id": "2"}]}},
            {"type": "assistant", "message": {"content": [{"type": "tool_use", "name": "Read", "id": "3"}]}},
            {"type": "assistant", "message": {"content": [{"type": "tool_use", "name": "Bash", "id": "4"}]}},
            _make_assistant_event("Done"),
        ]
        raw = json.dumps(events)

        parsed = parse_claude_output(raw)
        assert parsed.stdout != raw, "Expected a summary, not the raw JSON stream"
        # Tool counts should appear in the summary. Look for an "Edit ×2"-style
        # or "Edit (2)"-style structure — the format is flexible but the count
        # must be associated with the name (not just both substrings present
        # somewhere in the raw payload).
        assert "Edit ×2" in parsed.stdout or "Edit (2)" in parsed.stdout or "Edit: 2" in parsed.stdout, parsed.stdout
        assert "Read" in parsed.stdout
        assert "Bash" in parsed.stdout

    def test_summary_surfaces_budget_error_when_result_event_has_errors(self):
        """Budget-exit shape: result event with ``errors`` and no ``result``."""
        events = [
            _make_system_event(),
            _make_assistant_event("Working on it"),
            {"type": "result", "errors": ["Reached maximum budget ($5)"]},
        ]
        raw = json.dumps(events)

        parsed = parse_claude_output(raw)
        assert "budget" in parsed.stdout.lower() or "Reached maximum" in parsed.stdout
        # Must not be the raw stream
        assert raw != parsed.stdout
        # The assistant text should still be there so the operator sees what
        # the agent was doing when it hit the cap.
        assert "Working on it" in parsed.stdout

    def test_summary_capped_at_max_bytes(self):
        """Pathological stream with thousands of tool calls is still bounded."""
        events = [_make_system_event()]
        for i in range(5000):
            events.append(
                {"type": "assistant", "message": {"content": [{"type": "tool_use", "name": "Edit", "id": str(i)}]}}
            )
        raw = json.dumps(events)
        # Raw size is comfortably above the cap to make the test meaningful.
        assert len(raw.encode("utf-8")) > self._MAX_BYTES

        parsed = parse_claude_output(raw)
        assert len(parsed.stdout.encode("utf-8")) <= self._MAX_BYTES

    def test_summary_for_plaintext_capped_at_max_bytes(self):
        """Non-JSON input larger than the cap is bounded too."""
        raw = "x" * (self._MAX_BYTES * 2)

        parsed = parse_claude_output(raw)
        assert len(parsed.stdout.encode("utf-8")) <= self._MAX_BYTES

    def test_summary_normal_result_unchanged(self):
        """Regression guard: when there's a clean result text, summary path doesn't kick in."""
        events = [
            _make_system_event(),
            _make_assistant_event("intermediate"),
            _make_result_event("the actual answer", "sess-xyz"),
        ]
        raw = json.dumps(events)

        parsed = parse_claude_output(raw)
        # The presence of a result event short-circuits summarisation.
        assert parsed.stdout == "the actual answer"
        assert parsed.session_id == "sess-xyz"


class TestParseClaudeOutputUsage:
    """Test token usage and cost extraction from parse_claude_output."""

    def test_extracts_usage_and_cost(self):
        event = _make_result_event(
            "Answer",
            cost_usd=0.012,
            usage={"input_tokens": 840, "output_tokens": 400},
        )
        raw = json.dumps(event)

        parsed = parse_claude_output(raw)
        assert parsed.input_tokens == 840
        assert parsed.output_tokens == 400
        assert parsed.cost_usd == 0.012

    def test_missing_usage_returns_none(self):
        event = _make_result_event("Answer")
        raw = json.dumps(event)

        parsed = parse_claude_output(raw)
        assert parsed.input_tokens is None
        assert parsed.output_tokens is None
        assert parsed.cost_usd is None

    def test_partial_usage(self):
        event = _make_result_event("Answer", usage={"input_tokens": 100})
        raw = json.dumps(event)

        parsed = parse_claude_output(raw)
        assert parsed.input_tokens == 100
        assert parsed.output_tokens is None

    def test_verbose_array_with_usage(self):
        events = [
            _make_system_event(),
            _make_result_event("Done", cost_usd=0.05, usage={"input_tokens": 500, "output_tokens": 200}),
        ]
        raw = json.dumps(events)

        parsed = parse_claude_output(raw)
        assert parsed.stdout == "Done"
        assert parsed.input_tokens == 500
        assert parsed.output_tokens == 200
        assert parsed.cost_usd == 0.05

    def test_zero_cost_preserved(self):
        """total_cost_usd: 0.0 should not be treated as missing."""
        event = {"type": "result", "result": "Free run", "total_cost_usd": 0.0}
        raw = json.dumps(event)

        parsed = parse_claude_output(raw)
        assert parsed.cost_usd == 0.0

    def test_plain_text_no_usage(self):
        parsed = parse_claude_output("plain text output")
        assert parsed.input_tokens is None
        assert parsed.output_tokens is None
        assert parsed.cost_usd is None
