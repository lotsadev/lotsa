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
# PR 1 — chat→build spec carry (ADR-043 dissolved the standalone spec step;
# the spec is distilled in chat and read by build's planning prompt)
# ───────────────────────────────────────────────────────────────────────────


def test_build_planning_user_injects_draft_spec_artifact():
    """The spec carried from chat only reaches the build agent if the planning
    user template references it via the ``{artifact:NAME}`` injection mechanism
    (orchestrator artifact substitution)."""
    text = (BUNDLED_PROMPTS / "build" / "planning-user.md").read_text()
    assert "{artifact:draft_spec}" in text


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
    assert "hand off" in text or "handoff" in text or "promote" in text
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
# PR 2 — fix coder prompt (R9); ``fix`` is ADR-043's Execute-at-shallow-depth
# process (the former ``quickfix``)
# ───────────────────────────────────────────────────────────────────────────


def test_fix_coding_prompts_exist():
    assert (BUNDLED_PROMPTS / "fix" / "coding-system.md").is_file()
    assert (BUNDLED_PROMPTS / "fix" / "coding-user.md").is_file()


def test_fix_coder_injects_seeded_instruction():
    """The operator's handed-off ``instruction`` reaches the fix coder via the
    ``{artifact:instruction}`` injection (mirrors draft_spec)."""
    text = (BUNDLED_PROMPTS / "fix" / "coding-user.md").read_text()
    assert "{artifact:instruction}" in text
