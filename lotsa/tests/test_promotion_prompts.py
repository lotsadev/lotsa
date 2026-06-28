"""Prompt smoke tests for ADR-027 (operator-driven process promotion).

These guard the *prompt-level* contract of promotion — the bundled prompt
files the handover depends on. They join the ``TestOperationalPreamble``-style
guardrails: a silent revert or truncation of these paragraphs would break
chat→full handover (verify-instead-of-elicit) or the chat agent's triage,
without any Python test noticing.

The prompt files are read straight off disk via ``BUNDLED_PROMPTS`` (the same
shape as ``test_pr_summary.py``'s file-existence checks).
"""

from __future__ import annotations

from lotsa.flows import BUNDLED_PROMPTS

# ───────────────────────────────────────────────────────────────────────────
# PR 1 — full/spec handover (R5)
# ───────────────────────────────────────────────────────────────────────────


def _spec_system() -> str:
    return (BUNDLED_PROMPTS / "full" / "spec-system.md").read_text()


def _spec_user() -> str:
    return (BUNDLED_PROMPTS / "full" / "spec-user.md").read_text()


def test_spec_system_has_verify_instead_of_elicit_branch():
    """ADR-027 §4: the spec step must verify-and-finalize a pre-seeded draft
    instead of eliciting from scratch.

    Fails pre-fix: the verify-instead-of-elicit paragraph is absent."""
    text = _spec_system().lower()
    # The branch is keyed off a pre-existing/agreed spec ("already present").
    assert "already" in text and "draft_spec" in text
    # And it routes to verify/finalize rather than re-eliciting.
    assert "verify" in text


def test_spec_user_injects_draft_spec_artifact():
    """The seeded ``draft_spec`` only reaches the agent if the user template
    references it via the ``{artifact:NAME}`` injection mechanism
    (orchestrator artifact substitution). Without this, AC #3 cannot pass.

    Fails pre-fix: ``spec-user.md`` has no ``{artifact:draft_spec}`` reference."""
    assert "{artifact:draft_spec}" in _spec_user()


# ───────────────────────────────────────────────────────────────────────────
# PR 2 — chat triage prompt (R8)
# ───────────────────────────────────────────────────────────────────────────


def _chat_system() -> str:
    path = BUNDLED_PROMPTS / "chat" / "chat-system.md"
    return path.read_text()


def test_chat_system_file_exists():
    """Fails pre-fix: the bundled chat process prompt does not exist."""
    assert (BUNDLED_PROMPTS / "chat" / "chat-system.md").is_file()


def test_chat_system_renders_available_processes_block():
    """The triage prompt must carry the ``{available_processes}`` placeholder
    the orchestrator substitutes with the loaded catalog at dispatch time
    (ADR-027 §3 — data-driven triage, not hardcoded heuristics)."""
    assert "{available_processes}" in _chat_system()


def test_chat_system_instructs_suggest_not_self_promote():
    """ADR-027 §1/§3 — the chat agent suggests a destination; it must never
    promote on its own authority (promotion is operator-only)."""
    text = _chat_system().lower()
    assert "promote" in text
    # The "suggest, operator confirms / do not self-promote" contract.
    assert "operator" in text


def test_chat_system_carries_operational_rules():
    """The chat step is conversational, so the shared OPERATIONAL_PREAMBLE is
    not prepended (``_build_system_prompt`` skips it for conversational
    steps). The no-branch/no-push and NEEDS_INPUT rules must therefore be
    embedded in the chat prompt itself (ADR-027 §6)."""
    text = _chat_system()
    assert "NEEDS_INPUT" in text


# ───────────────────────────────────────────────────────────────────────────
# PR 2 — quickfix coder prompt (R9)
# ───────────────────────────────────────────────────────────────────────────


def test_quickfix_coding_prompts_exist():
    """Fails pre-fix: the quickfix process prompts do not exist."""
    assert (BUNDLED_PROMPTS / "quickfix" / "coding-system.md").is_file()
    assert (BUNDLED_PROMPTS / "quickfix" / "coding-user.md").is_file()


def test_quickfix_coder_injects_seeded_instruction():
    """The operator's promoted ``instruction`` reaches the quickfix coder via
    the ``{artifact:instruction}`` injection (mirrors draft_spec)."""
    text = (BUNDLED_PROMPTS / "quickfix" / "coding-user.md").read_text()
    assert "{artifact:instruction}" in text
