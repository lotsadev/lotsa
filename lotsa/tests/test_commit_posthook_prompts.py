"""Prompt, preamble, and process.yaml content checks for ADR-024 / ADR-044.

Commit moves out of agent-prompt prose and into the orchestrator-run
``commit`` posthook (ADR-024). Under ADR-044 Phase 2 the posthook is no longer
hand-declared per job in the YAML — it is *derived* from each agent's
``produces_changes`` property. These tests pin the consequences:

* The bundled ``build`` process *resolves* ``commit`` onto the producing
  agent steps (``test``/``code``/``pr-fix``) and onto none of the
  non-producers (``plan``/``review``/``verify``). ``verify`` is a gate that
  observes; it no longer commits.
* The producer prompts no longer carry a ``git commit`` instruction and each
  gains the "you do not commit" line.
* ``OPERATIONAL_PREAMBLE`` states that commit is orchestrator-owned.

The posthook-declaration tests assert on the *resolved* ``FlowStep.posthooks``
(``build_process``) rather than the raw YAML, because Phase 2 drops the literal
``posthooks: [commit]`` lines and derives them instead.
"""

from __future__ import annotations

from pathlib import Path

import pytest

_FULL = Path(__file__).resolve().parents[1] / "prompts" / "build"
# ADR-044 — prompt bodies live in the shared agent catalog; process.yaml stays
# under the process dir. Map a prompt base to its catalog agent (pr-fix uses the
# ``pr_fix`` agent).
_CATALOG = Path(__file__).resolve().parents[1] / "prompts" / "agents"


def _catalog_system(prompt_base: str) -> str:
    agent = "pr_fix" if prompt_base == "pr-fix" else prompt_base
    return (_CATALOG / agent / "system.md").read_text().lower()


# ADR-044 Phase 2: producers derive ``commit`` from ``produces_changes: true``;
# non-producers (incl. the ``verify`` gate, which observes) derive nothing.
_PRODUCERS = ("test", "code", "pr-fix")
_NON_PRODUCERS = ("plan", "review", "verify")


def _resolved_posthooks() -> dict[str, list[str]]:
    """Effective posthooks per job in the bundled ``build`` process."""
    import lotsa.posthooks  # noqa: F401 — ensures the built-in ``commit`` exists
    from lotsa.flows import build_process

    return {j.name: list(j.posthooks) for j in build_process("build").jobs}


# ---------------------------------------------------------------------------
# process.yaml — commit posthook is DERIVED onto producers (ADR-044 Phase 2)
# ---------------------------------------------------------------------------


@pytest.mark.parametrize("job_name", _PRODUCERS)
def test_producer_job_resolves_commit_posthook(job_name: str):
    posthooks = _resolved_posthooks()[job_name]
    assert "commit" in posthooks, f"{job_name!r} must resolve posthooks: [commit] (derived)"


@pytest.mark.parametrize("job_name", _NON_PRODUCERS)
def test_non_producer_job_has_no_commit_posthook(job_name: str):
    posthooks = _resolved_posthooks()[job_name]
    assert "commit" not in posthooks, f"{job_name!r} must not run the commit posthook"


# ---------------------------------------------------------------------------
# Prompts — commit instructions removed; "you do not commit" line added
# ---------------------------------------------------------------------------


@pytest.mark.parametrize("prompt_base", ("coding", "testing", "pr-fix", "verify"))
def test_producer_prompt_has_do_not_commit_line(prompt_base: str):
    text = _catalog_system(prompt_base)
    assert "you do not commit" in text, f"{prompt_base}-system.md must state that the orchestrator owns commit"


@pytest.mark.parametrize("prompt_base", ("coding", "testing"))
def test_producer_prompt_drops_git_commit_instruction(prompt_base: str):
    """The literal ``git commit`` command must be gone from the producer prompts."""
    text = _catalog_system(prompt_base)
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
