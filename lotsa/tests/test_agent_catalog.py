"""RED tests for the shared agent catalog (ADR-044, Phase 1).

These specify the process-independent **agent catalog** that Phase 1 hoists out
of the per-process ``prompts/{process}/`` folders into ``lotsa/prompts/agents/``.
Each agent is a directory holding ``agent.yaml`` (declared properties) plus its
``system.md`` / ``user.md`` prompt bodies.

The contract these tests pin down (the coding step implements ``lotsa.agents``):

* ``AGENT_OUTCOMES`` — the closed universal vocabulary
  ``COMPLETED / PASSED / FAILED / SKIPPED / INPUT``.
* ``AGENTS_DIR`` — the bundled catalog directory.
* ``load_agent_catalog(agents_dir=AGENTS_DIR) -> dict[str, Agent]``.
* ``load_agent(name, agents_dir=AGENTS_DIR) -> Agent``.
* ``Agent`` exposes ``name``, ``outcomes`` (declared closed set ⊆ vocab),
  ``is_gate`` / ``is_worker`` (worker-vs-gate class), and the reserved
  property slots ``needs_worktree`` / ``produces_changes``.

They fail RED today because ``lotsa.agents`` does not exist and the bundled
catalog directory has not been created. Imports are done inside each test so a
missing symbol reds that behaviour independently rather than erroring the whole
module at collection.
"""

from __future__ import annotations

from pathlib import Path

import pytest

# The set of agents Phase 1 hoists into the catalog. ``fix_coding`` is fix's
# distinctive "execute this instruction" coder, kept distinct from build's
# ``coding`` to preserve current behaviour (see the distinctness test at the
# bottom, which does not hardcode the name).
_CORE_AGENTS = (
    "planning",
    "testing",
    "coding",
    "review",
    "verify",
    "pr_fix",
    "pr_summary",
    "resolve_conflicts",
    "chat",
)

# Worker-vs-gate default emittable sets (the closed universe an agent of each
# class may declare a subset of). ``INPUT`` is orthogonal — any agent may emit
# it — so it is admissible for both classes.
_WORKER_ADMISSIBLE = {"COMPLETED", "SKIPPED", "FAILED", "INPUT"}
_GATE_ADMISSIBLE = {"PASSED", "FAILED", "INPUT"}


# ───────────────────────────────────────────────────────────────────────────
# Vocabulary constant
# ───────────────────────────────────────────────────────────────────────────


def test_agent_outcomes_is_the_closed_five_marker_vocabulary():
    from lotsa.agents import AGENT_OUTCOMES

    assert set(AGENT_OUTCOMES) == {"COMPLETED", "PASSED", "FAILED", "SKIPPED", "INPUT"}


# ───────────────────────────────────────────────────────────────────────────
# Catalog presence + loading
# ───────────────────────────────────────────────────────────────────────────


def test_agents_dir_exists_and_is_a_directory():
    from lotsa.agents import AGENTS_DIR

    assert AGENTS_DIR.is_dir()


def test_catalog_contains_every_core_agent():
    from lotsa.agents import load_agent_catalog

    catalog = load_agent_catalog()
    for name in _CORE_AGENTS:
        assert name in catalog, f"catalog missing agent {name!r}"


@pytest.mark.parametrize("name", _CORE_AGENTS)
def test_each_core_agent_loads(name):
    from lotsa.agents import load_agent

    agent = load_agent(name)
    assert agent.name == name


# ───────────────────────────────────────────────────────────────────────────
# Universal property invariants (hold for every bundled agent)
# ───────────────────────────────────────────────────────────────────────────


def test_every_agent_declares_outcomes_within_the_vocabulary():
    from lotsa.agents import AGENT_OUTCOMES, load_agent_catalog

    vocab = set(AGENT_OUTCOMES)
    for name, agent in load_agent_catalog().items():
        declared = set(agent.outcomes)
        assert declared, f"{name!r} declares no outcomes"
        assert declared.issubset(vocab), f"{name!r} declares out-of-vocab outcomes: {declared - vocab}"


def test_every_agent_is_exactly_worker_or_gate():
    from lotsa.agents import load_agent_catalog

    for name, agent in load_agent_catalog().items():
        assert agent.is_worker != agent.is_gate, f"{name!r} must be exactly one of worker/gate"


def test_class_constrains_admissible_outcomes():
    from lotsa.agents import load_agent_catalog

    for name, agent in load_agent_catalog().items():
        declared = set(agent.outcomes)
        admissible = _GATE_ADMISSIBLE if agent.is_gate else _WORKER_ADMISSIBLE
        assert declared.issubset(admissible), f"{name!r} ({'gate' if agent.is_gate else 'worker'}) declares {declared - admissible}"


def test_reserved_property_slots_are_booleans():
    """``needs_worktree`` / ``produces_changes`` exist in the schema from day one
    (declared in Phase 1, wired in Phases 2/3)."""
    from lotsa.agents import load_agent_catalog

    for name, agent in load_agent_catalog().items():
        assert isinstance(agent.needs_worktree, bool), f"{name!r}.needs_worktree not bool"
        assert isinstance(agent.produces_changes, bool), f"{name!r}.produces_changes not bool"


# ───────────────────────────────────────────────────────────────────────────
# Load-bearing per-agent declarations
# ───────────────────────────────────────────────────────────────────────────


def test_review_is_a_gate_emitting_passed_failed():
    from lotsa.agents import load_agent

    review = load_agent("review")
    assert review.is_gate is True
    assert set(review.outcomes) == {"PASSED", "FAILED"}


def test_verify_is_a_gate_emitting_passed_failed():
    """verify's old three-way NEEDS_CODE/NEEDS_REVIEW/VERIFIED collapses to the
    gate's PASSED/FAILED (FAILED routes back to code)."""
    from lotsa.agents import load_agent

    verify = load_agent("verify")
    assert verify.is_gate is True
    assert set(verify.outcomes) == {"PASSED", "FAILED"}


def test_pr_fix_is_a_worker_declaring_the_wider_closed_set():
    """pr-fix is the canonical worker that declares a set wider than the default
    worker exit — COMPLETED (done→review), SKIPPED (→monitor), FAILED
    (blocked), INPUT (needs-decision)."""
    from lotsa.agents import load_agent

    pr_fix = load_agent("pr_fix")
    assert pr_fix.is_worker is True
    assert set(pr_fix.outcomes) == {"COMPLETED", "SKIPPED", "FAILED", "INPUT"}


def test_coding_is_a_worker_that_produces_changes():
    from lotsa.agents import load_agent

    coding = load_agent("coding")
    assert coding.is_worker is True
    assert coding.produces_changes is True
    assert set(coding.outcomes) == {"COMPLETED"}


def test_chat_does_not_need_a_worktree():
    """A chat-only task allocates no writable worktree (Phase 3 wires this;
    Phase 1 declares it)."""
    from lotsa.agents import load_agent

    chat = load_agent("chat")
    assert chat.needs_worktree is False


# ───────────────────────────────────────────────────────────────────────────
# Load-time validation (fail loud, mirroring flows.py's validators)
# ───────────────────────────────────────────────────────────────────────────


def _write_agent(dir_: Path, name: str, *, klass: str, outcomes: list[str]) -> None:
    agent_dir = dir_ / name
    agent_dir.mkdir(parents=True)
    (agent_dir / "system.md").write_text("system prompt body\n")
    (agent_dir / "user.md").write_text("user prompt body\n")
    import yaml

    (agent_dir / "agent.yaml").write_text(
        yaml.safe_dump({"name": name, "class": klass, "outcomes": outcomes})
    )


def test_out_of_vocab_outcome_fails_loud(tmp_path):
    from lotsa.agents import load_agent_catalog

    _write_agent(tmp_path, "bogus", klass="worker", outcomes=["BOGUS"])
    with pytest.raises(ValueError):
        load_agent_catalog(tmp_path)


def test_unknown_class_fails_loud(tmp_path):
    from lotsa.agents import load_agent_catalog

    _write_agent(tmp_path, "weird", klass="oracle", outcomes=["COMPLETED"])
    with pytest.raises(ValueError):
        load_agent_catalog(tmp_path)


def test_gate_declaring_completed_fails_loud(tmp_path):
    """A gate may not emit a worker-only outcome — the class/outcome consistency
    check rejects it at load time."""
    from lotsa.agents import load_agent_catalog

    _write_agent(tmp_path, "confused", klass="gate", outcomes=["COMPLETED"])
    with pytest.raises(ValueError):
        load_agent_catalog(tmp_path)


# ───────────────────────────────────────────────────────────────────────────
# fix keeps a coder distinct from build's (behaviour preserved without
# hardcoding the catalog name for it)
# ───────────────────────────────────────────────────────────────────────────


def test_fix_code_resolves_to_a_different_prompt_than_build_code():
    """Under the single shared catalog, fix's ``code`` step and build's ``code``
    step must reference *distinct* agents to preserve their distinct prompts
    (the ``fix→build`` search-path fallback is removed in Phase 1). Today both
    resolve to ``coding`` (equal), so this reds."""
    from lotsa.flows import build_process

    build_code = {j.name: j for j in build_process("build").jobs}["code"].prompt_name
    fix_code = {j.name: j for j in build_process("fix").jobs}["code"].prompt_name
    assert build_code != fix_code


# ───────────────────────────────────────────────────────────────────────────
# ADR-044 lands as the first commit
# ───────────────────────────────────────────────────────────────────────────


def test_adr_044_exists_and_defines_the_vocabulary():
    repo_root = Path(__file__).resolve().parents[2]
    adr = repo_root / "docs" / "adr" / "ADR-044-workflows-for-agents.md"
    assert adr.is_file(), "ADR-044 must land as the first commit of this build"
    text = adr.read_text()
    assert "AGENT_RESULT" in text
    for outcome in ("COMPLETED", "PASSED", "FAILED", "SKIPPED", "INPUT"):
        assert outcome in text, f"ADR-044 does not mention outcome {outcome!r}"
