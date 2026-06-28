"""Prompt, preamble, and process.yaml content checks for ADR-024.

Commit moves out of agent-prompt prose and into the orchestrator-run
``commit`` posthook. These tests pin the textual consequences:

* ``full/process.yaml`` declares ``posthooks: [commit]`` on the four
  code-producing jobs (``test``/``code``/``verify``/``pr-fix``) and on none
  of the non-producers (``spec``/``plan``/``review``).
* The four producer prompts no longer carry a ``git commit`` instruction and
  each gains the "you do not commit" line.
* ``OPERATIONAL_PREAMBLE`` states that commit is orchestrator-owned.

These read the real bundled files; they fail against pre-fix content (which
still tells agents to commit) and pass once R5/R6 land.
"""

from __future__ import annotations

from pathlib import Path

import pytest
import yaml

_FULL = Path(__file__).resolve().parents[1] / "prompts" / "full"

_PRODUCERS = ("test", "code", "verify", "pr-fix")
_NON_PRODUCERS = ("spec", "plan", "review")


def _load_jobs() -> dict[str, dict]:
    data = yaml.safe_load((_FULL / "process.yaml").read_text())
    return {j["name"]: j for j in data["jobs"]}


# ---------------------------------------------------------------------------
# process.yaml — posthook declarations
# ---------------------------------------------------------------------------


@pytest.mark.parametrize("job_name", _PRODUCERS)
def test_producer_job_declares_commit_posthook(job_name: str):
    jobs = _load_jobs()
    posthooks = jobs[job_name].get("posthooks") or []
    assert "commit" in posthooks, f"{job_name!r} must declare posthooks: [commit]"


@pytest.mark.parametrize("job_name", _NON_PRODUCERS)
def test_non_producer_job_has_no_commit_posthook(job_name: str):
    jobs = _load_jobs()
    posthooks = jobs[job_name].get("posthooks") or []
    assert "commit" not in posthooks, f"{job_name!r} must not run the commit posthook"


# ---------------------------------------------------------------------------
# Prompts — commit instructions removed; "you do not commit" line added
# ---------------------------------------------------------------------------


@pytest.mark.parametrize("prompt_base", ("coding", "testing", "pr-fix", "verify"))
def test_producer_prompt_has_do_not_commit_line(prompt_base: str):
    text = (_FULL / f"{prompt_base}-system.md").read_text().lower()
    assert "you do not commit" in text, f"{prompt_base}-system.md must state that the orchestrator owns commit"


@pytest.mark.parametrize("prompt_base", ("coding", "testing"))
def test_producer_prompt_drops_git_commit_instruction(prompt_base: str):
    """The literal ``git commit`` command must be gone from the producer prompts."""
    text = (_FULL / f"{prompt_base}-system.md").read_text().lower()
    assert "git commit" not in text, f"{prompt_base}-system.md must not instruct the agent to run ``git commit``"


# ---------------------------------------------------------------------------
# OPERATIONAL_PREAMBLE — commit joins push as orchestrator-owned
# ---------------------------------------------------------------------------


def test_operational_preamble_states_commit_is_orchestrator_owned():
    from lotsa.orchestrator import OPERATIONAL_PREAMBLE

    text = OPERATIONAL_PREAMBLE.lower()
    assert "do not commit" in text, (
        "the preamble must tell agents not to commit (commit is orchestrator-owned, like push)"
    )
