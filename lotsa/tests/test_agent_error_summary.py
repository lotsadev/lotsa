"""_summarize_agent_error surfaces the real cause, not a trailing usage hint."""

from __future__ import annotations

from lotsa.orchestrator import _summarize_agent_error


def test_docker_missing_image_surfaces_real_cause():
    # Exactly the shape docker emits on a missing/unpullable image (exit 125).
    stderr = (
        "Unable to find image 'lotsa-agent:latest' locally\n"
        "docker: Error response from daemon: pull access denied for lotsa-agent, "
        "repository does not exist or may require 'docker login'.\n"
        "See 'docker run --help'.\n"
    )
    msg = _summarize_agent_error(125, stderr)
    assert "pull access denied" in msg  # the actionable cause
    assert "--help" not in msg  # not the useless trailing hint


def test_plain_stderr_uses_last_line():
    assert _summarize_agent_error(1, "boom\nactual error here") == "Agent exited with code 1: actual error here"


def test_empty_stderr_is_just_the_code():
    assert _summarize_agent_error(2, "") == "Agent exited with code 2"
    assert _summarize_agent_error(2, None) == "Agent exited with code 2"


def test_all_help_hints_falls_back_to_code():
    assert _summarize_agent_error(125, "See 'docker run --help'.") == "Agent exited with code 125"
