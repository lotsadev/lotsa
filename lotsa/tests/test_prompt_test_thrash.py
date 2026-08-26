"""Prompt guards against the failure mode behind prod incident ``f22e232b``.

A ``review`` step burned its whole dispatch and was killed at the timeout with
no review delivered. The session log shows where the hour went:

* **~40 minutes re-running the full test suite, eight times.** Not because
  anything changed — the flags kept changing (``-q``, ``-p no:cacheprovider``,
  ``-rf --tb=no``, ``--tb=line --maxfail=6``, redirect-to-file) because the
  agent was re-running the suite to reshape its *output*. One run captured to a
  file, grepped as many ways as needed, would have cost five minutes.
* **The rest polling for background shells that no longer had a writer** —
  ``sleep 45; cat out``, ``until grep -q "done---" /tmp/marker_done; do sleep 3;
  done``, repeated reads of ``tasks/*.output``. Under ``claude --print`` the
  backgrounded process is reaped with the turn, so those files never filled.

The deepest problem is that **review was not supposed to run the suite at
all.** Its own first line scopes it to "issues that the test suite won't
catch", the ``verify`` step immediately after it is the one that runs tests,
and the canonical review workflow's Phase 0 is lint + typecheck — tests appear
nowhere in it. The agent invented the work.

These are prompt guards, so they constrain *text*, not behaviour: they assert
the instruction is present and specific, which is the most a prompt fix can
promise. Nothing here prevents an agent from ignoring it.
"""

from __future__ import annotations

import pytest

from lotsa.flows import BUNDLED_PROMPTS

REVIEW_SYSTEM = BUNDLED_PROMPTS / "agents" / "review" / "system.md"


# ===========================================================================
# 1. review must not run the test suite
# ===========================================================================


def test_review_prompt_forbids_running_the_test_suite():
    text = REVIEW_SYSTEM.read_text().lower()

    assert "do not run the project's test suite" in text, (
        "review must be told not to run the suite — its charter is the issues the "
        "suite won't catch, and `verify` runs tests immediately after it"
    )


def test_review_prompt_points_at_verify_as_the_place_tests_run():
    """A bare prohibition invites working around it. Name the alternative."""
    text = REVIEW_SYSTEM.read_text().lower()

    assert "verify" in text.split("do not run the project's test suite")[1][:800], (
        "the prohibition must say where tests DO get run, so the agent routes "
        "the question instead of finding another way to answer it itself"
    )


def test_review_prompt_allows_a_targeted_single_file_run():
    """An absolute ban would make a finding that hinges on one test unverifiable."""
    text = REVIEW_SYSTEM.read_text().lower()

    assert "targeted" in text or "one test file" in text or "single test file" in text, (
        "review needs an escape hatch for a finding that genuinely depends on one test"
    )


def test_review_prompt_keeps_its_no_test_suite_rule_near_the_other_prohibitions():
    """Discoverability: the rule belongs with the other 'do not' rules, not
    buried in the middle of the workflow description."""
    text = REVIEW_SYSTEM.read_text()

    section = text.split("## What this prompt is NOT")
    assert len(section) == 2, "the review prompt's prohibition section is gone or renamed"
    assert "test suite" in section[1], "the no-suite rule must live with the other prohibitions"


# ===========================================================================
# 2. The dispatch-shape fragment must name the SHELL-level patterns
# ===========================================================================


@pytest.fixture
def fragment() -> str:
    from rigg import CLI_DISPATCH_SHAPE_FRAGMENT

    return CLI_DISPATCH_SHAPE_FRAGMENT


def test_fragment_names_shell_backgrounding_not_just_tools(fragment):
    """The existing text lists *tools* (Monitor, BashOutput, ...). The agent
    didn't use a tool — it used shell syntax, which the tool list doesn't cover.
    """
    assert "&" in fragment
    assert "until" in fragment, "the ``until ...; do sleep; done`` poll idiom must be named"
    assert "sleep" in fragment, "the ``sleep N; cat file`` idiom must be named"


def test_fragment_explains_why_polling_cannot_work(fragment):
    """ "Don't do X" is weaker than "X cannot work, here's why"."""
    lowered = fragment.lower()
    assert "reaped" in lowered
    assert "never fills" in lowered or "never fill" in lowered, (
        "say what actually happens: the polled file never fills because the writer is gone"
    )


def test_fragment_tells_agents_to_capture_slow_output_once(fragment):
    """The eight-suite-runs pattern: re-running a slow command to reshape its
    output instead of capturing once and grepping the capture."""
    lowered = fragment.lower()
    assert "once" in lowered, "the run-once rule must be stated"
    assert "tee" in lowered or "capture" in lowered or "redirect" in lowered, (
        "name the mechanism — capture the output to a file and re-read it"
    )


def test_fragment_still_names_every_unsupported_tool(fragment):
    """Guard the pre-existing contract while adding to it (rigg's own
    test_dispatch_shape_prompt.py asserts the same list)."""
    for tool in ("Monitor", "ScheduleWakeup", "Task", "BashOutput", "AskUserQuestion"):
        assert tool in fragment
