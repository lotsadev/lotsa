"""Failing tests for the formalized handoff suggestion (`handoff` routing target).

Realizes ADR-044 Phase 4(c) — the deferred "promotion payload" formalization,
amending ADR-027 §1 (agents may *suggest* a hand-off via the routing edge; the
operator still gates the actual promotion).

Design under test (see the implementation plan):

* ``handoff`` becomes a first-class rule ``target`` (``flows.py`` —
  ``_validate_rule_targets`` sentinel; the ``routes:`` sugar already passes the
  value through).
* The bundled ``chat`` step declares ``routes: { COMPLETED: handoff }``.
* An agent on a ``→ handoff`` edge names a destination in the marker payload
  (``AGENT_RESULT: COMPLETED <workflow>``). The completion drainer validates the
  name against loaded ``hand-off``-invocable processes, records it as a
  ``handoff_suggestion`` named artifact (latest-wins), and PARKS at ``waiting``
  in the same state — it never advances, promotes, or terminates the REPL.
* ``_marker_requirement_footer`` advertises the handoff option as *optional*
  (not the mandatory-marker phrasing) and lists the destinations.
* The suggestion reaches the frontend through the existing
  ``TaskDetailFullResponse.artifacts`` map (no API change).

Every test here fails against the pre-fix tree for the intended reason: the
parsers/helpers don't exist (``AttributeError``), the footer signature lacks the
new kwarg (``TypeError``) / still renders "mandatory", ``handoff`` is rejected at
build time (``ValueError``), and the drainer never saves the artifact
(assertion / ``None``).
"""

from __future__ import annotations

import asyncio

import pytest

import lotsa.orchestrator as orch
from lotsa.config import LotsaConfig
from lotsa.db import TaskDB
from lotsa.flows import OutputRule, build_process
from lotsa.orchestrator import OrchestratorService, _marker_requirement_footer
from lotsa.tests.conftest import wait_for_completion, wait_for_status
from lotsa.tests.test_orchestrator import FakeRunner, SequentialFakeRunner
from rigg.models import AgentResult


# ---------------------------------------------------------------------------
# Helpers / fixtures
# ---------------------------------------------------------------------------


def _chat_result(stdout: str) -> AgentResult:
    """A canned agent result carrying full run stats (so the drainer's chat
    message picks up the rich metadata path)."""
    return AgentResult(
        success=True,
        stdout=stdout,
        stderr="",
        return_code=0,
        duration_ms=1200,
        model="sonnet",
        session_id="sess-handoff",
        input_tokens=120,
        output_tokens=40,
        cost_usd=0.003,
    )


@pytest.fixture()
def chat_service(tmp_path, _loop, run):
    """A started service whose ACTIVE process is the bundled ``chat`` workflow.

    ``start()`` auto-loads the full catalog (ADR-034), so ``build`` and ``fix``
    are present as valid hand-off destinations for the drainer's validation.
    The chat step is ``needs_worktree: false``, so it runs in the (non-git)
    project work_dir — no commit posthook, nothing needs a real repo.
    """
    (tmp_path / "data").mkdir()
    config = LotsaConfig(
        data_dir=tmp_path / "data",
        work_dir=tmp_path,
        flow="chat",
        model="sonnet",
        budget=5.0,
    )
    db = TaskDB(tmp_path / "data" / "lotsa.db")
    run(db.initialize())
    svc = OrchestratorService(config, db)
    svc.runner = FakeRunner()
    run(svc.start())
    yield svc
    run(svc.shutdown())
    run(db.close())


def _create_chat_task(svc, run, stdout: str):
    """Set the runner to emit *stdout*, create a chat task, and drain."""
    svc.runner = FakeRunner(_chat_result(stdout))
    task = run(svc.create_task(message="Let's talk about a change", process_name="chat"))
    run(wait_for_completion(svc, task.id))
    run(wait_for_status(svc, task.id, "waiting"))
    return task


# ---------------------------------------------------------------------------
# Parser: _extract_handoff_suggestion
# ---------------------------------------------------------------------------


class TestExtractHandoffSuggestion:
    """``_extract_handoff_suggestion`` pulls the workflow-name token out of a
    ``AGENT_RESULT: COMPLETED <workflow>`` marker (module-scope parser, sibling
    to ``_extract_needs_input``). Fails pre-fix: the function does not exist
    (``AttributeError``)."""

    def test_returns_workflow_name(self):
        stdout = "This is clearly a full build.\n\nAGENT_RESULT: COMPLETED build"
        assert orch._extract_handoff_suggestion(stdout) == "build"

    def test_latest_marker_wins(self):
        stdout = "AGENT_RESULT: COMPLETED fix\n...reconsidered...\nAGENT_RESULT: COMPLETED build"
        assert orch._extract_handoff_suggestion(stdout) == "build"

    def test_bare_completed_is_none(self):
        # A bare COMPLETED with no workflow name carries no suggestion.
        assert orch._extract_handoff_suggestion("AGENT_RESULT: COMPLETED") is None

    def test_takes_first_payload_token(self):
        # Tolerant parsing: the name is the first whitespace token of the payload.
        stdout = "AGENT_RESULT: COMPLETED build — go for the full SDLC pass"
        assert orch._extract_handoff_suggestion(stdout) == "build"

    def test_absent_marker_is_none(self):
        assert orch._extract_handoff_suggestion("just some prose, no marker here") is None


# ---------------------------------------------------------------------------
# Display: _strip_handoff_marker
# ---------------------------------------------------------------------------


class TestStripHandoffMarker:
    """``_strip_handoff_marker`` removes the COMPLETED marker line from a chat
    message so the stored/displayed transcript shows only the agent's prose.
    Fails pre-fix: the helper does not exist (``AttributeError``)."""

    def test_removes_marker_line_keeps_prose(self):
        stdout = "Here is my recommendation.\nAGENT_RESULT: COMPLETED build"
        stripped = orch._strip_handoff_marker(stdout)
        assert "AGENT_RESULT: COMPLETED" not in stripped
        assert "Here is my recommendation." in stripped

    def test_no_marker_left_unchanged(self):
        text = "An ordinary chat turn with no marker."
        assert orch._strip_handoff_marker(text).strip() == text


# ---------------------------------------------------------------------------
# Footer: _marker_requirement_footer becomes handoff-aware
# ---------------------------------------------------------------------------


class TestMarkerFooterHandoff:
    """The mandatory-marker footer (ADR-039 stopgap) must render a ``→ handoff``
    edge as an OPTIONAL suggestion — a never-completing REPL must not be told a
    marker is mandatory every turn — and list the destinations."""

    def test_handoff_only_step_is_not_mandatory(self):
        # Behavioural red: pre-fix, the footer lists the COMPLETED handoff marker
        # under the "advances ONLY when… (mandatory)" block. Post-fix it renders
        # the optional handoff block instead.
        rules = [OutputRule(source="stdout", pattern="^AGENT_RESULT: COMPLETED", target="handoff")]
        footer = _marker_requirement_footer(rules)
        assert footer != ""
        assert "mandatory" not in footer.lower()
        assert "advances ONLY" not in footer
        assert "optional" in footer.lower()

    def test_handoff_footer_lists_destinations(self):
        # Signature red: pre-fix, ``_marker_requirement_footer`` takes no
        # ``handoff_destinations`` kwarg (``TypeError``).
        rules = [OutputRule(source="stdout", pattern="^AGENT_RESULT: COMPLETED", target="handoff")]
        footer = _marker_requirement_footer(
            rules, handoff_destinations="- build: Execute at full depth\n- fix: Shallow fix"
        )
        assert "build" in footer
        assert "fix" in footer

    def test_mixes_mandatory_and_optional_handoff(self):
        # A gate outcome stays mandatory; the handoff outcome is optional.
        rules = [
            OutputRule(source="stdout", pattern="^AGENT_RESULT: PASSED", target="next"),
            OutputRule(source="stdout", pattern="^AGENT_RESULT: COMPLETED", target="handoff"),
        ]
        footer = _marker_requirement_footer(rules, handoff_destinations="- build: Execute at full depth")
        assert "mandatory" in footer.lower()  # PASSED is still a required marker
        assert "PASSED" in footer
        assert "optional" in footer.lower()  # COMPLETED handoff is opt-in
        assert "build" in footer


# ---------------------------------------------------------------------------
# Routing: `handoff` is a valid rule target (flows.py)
# ---------------------------------------------------------------------------


def test_handoff_accepted_as_rule_target(tmp_path):
    """A ``routes: { COMPLETED: handoff }`` edge builds — ``handoff`` is a
    recognized routing sentinel alongside ``next``/``blocked``/``needs_input``.

    Fails pre-fix: ``_validate_rule_targets`` rejects ``handoff`` (not a sentinel,
    not a job name) with ``ValueError`` at ``build_process`` time.
    """
    process_file = tmp_path / "process.yaml"
    process_file.write_text(
        "process: handoff_test\n"
        "jobs:\n"
        "  - name: triage\n"
        "    type: agent\n"
        "    conversational: true\n"
        "    routes: { COMPLETED: handoff }\n"
        "flows:\n"
        "  main: { steps: [triage] }\n"
    )
    process = build_process("handoff_test", process_file=process_file)
    step = process.flows["main"].steps[0]
    handoff_rules = [r for r in step.rules if r.target == "handoff"]
    assert len(handoff_rules) == 1
    assert "COMPLETED" in handoff_rules[0].pattern


def test_handoff_target_adds_no_state_machine_edge(tmp_path):
    """``handoff`` records-and-parks; it is intercepted in the drainer and never
    resolved to a state, so it contributes no state-machine state/transition
    (the task parks in place)."""
    process_file = tmp_path / "process.yaml"
    process_file.write_text(
        "process: handoff_sm\n"
        "jobs:\n"
        "  - name: triage\n"
        "    type: agent\n"
        "    conversational: true\n"
        "    routes: { COMPLETED: handoff }\n"
        "flows:\n"
        "  main: { steps: [triage] }\n"
    )
    process = build_process("handoff_sm", process_file=process_file)
    sm = process.flows["main"].state_machine
    assert "handoff" not in sm.states
    assert not any(dst == "handoff" for (_src, dst) in sm.transitions)


def test_bundled_chat_routes_completed_to_handoff():
    """The bundled ``chat`` step edge-routes ``COMPLETED → handoff`` (the
    mechanism is on the edge, not a chat-name special case).

    Fails pre-fix: the chat REPL step declares no rules (``step.rules == []``).
    """
    chat = build_process("chat")
    step = chat.flows["main"].steps[0]
    handoff_rules = [
        r for r in step.rules if r.source == "stdout" and r.target == "handoff" and "COMPLETED" in r.pattern
    ]
    assert handoff_rules, f"chat step must route COMPLETED → handoff; rules={step.rules!r}"


# ---------------------------------------------------------------------------
# Catalog helper: _handoff_destinations
# ---------------------------------------------------------------------------


def test_handoff_destinations_lists_only_handoff_invocable(chat_service, run):
    """``_handoff_destinations`` returns the loaded ``hand-off``-invocable
    processes (build/fix) and excludes ``chat`` (``invocable: [start]``) — the
    same filtered set the suggest-catalog and the drainer's validation share.

    Fails pre-fix: ``_handoff_destinations`` does not exist (``AttributeError``).
    """
    names = {name for name, _desc in chat_service._handoff_destinations()}
    assert "build" in names
    assert "fix" in names
    assert "chat" not in names


# ---------------------------------------------------------------------------
# Drainer: record suggestion + park, never advance/promote (integration)
# ---------------------------------------------------------------------------


class TestHandoffDrainer:
    """A chat turn that emits ``AGENT_RESULT: COMPLETED <workflow>`` records the
    suggestion and parks — it must NOT advance, promote, or terminate the REPL.

    Fails pre-fix: chat has no handoff edge, so no ``handoff_suggestion``
    artifact is ever written (the drainer just parks the conversational step).
    """

    def test_valid_suggestion_saved_and_parked(self, chat_service, run):
        svc = chat_service
        task = _create_chat_task(svc, run, "This is a full build.\n\nAGENT_RESULT: COMPLETED build")

        suggestion = run(svc.get_named_artifact(task.id, "handoff_suggestion"))
        assert suggestion == "build"

        row = run(svc.db.get_task(task.id))
        # Parked, NOT advanced/promoted: still on the chat step, still the chat process.
        assert row.status == "waiting"
        assert row.state == "chat"
        assert row.metadata.get("process_name", "chat") == "chat"

    def test_invalid_destination_saves_no_artifact(self, chat_service, run):
        svc = chat_service
        task = _create_chat_task(svc, run, "AGENT_RESULT: COMPLETED not_a_real_process")

        assert run(svc.get_named_artifact(task.id, "handoff_suggestion")) is None
        row = run(svc.db.get_task(task.id))
        assert row.status == "waiting"  # still parked as an ordinary chat turn

    def test_non_invocable_destination_saves_no_artifact(self, chat_service, run):
        # ``chat`` is loaded but is not hand-off-invocable — a suggestion naming
        # it must be dropped, never surfaced as a pre-selection.
        svc = chat_service
        task = _create_chat_task(svc, run, "AGENT_RESULT: COMPLETED chat")

        assert run(svc.get_named_artifact(task.id, "handoff_suggestion")) is None

    def test_bare_completed_saves_no_artifact(self, chat_service, run):
        svc = chat_service
        task = _create_chat_task(svc, run, "Still thinking it through.\n\nAGENT_RESULT: COMPLETED")

        assert run(svc.get_named_artifact(task.id, "handoff_suggestion")) is None
        assert run(svc.db.get_task(task.id)).status == "waiting"

    def test_no_marker_parks_with_no_artifact(self, chat_service, run):
        # An ordinary chat turn (no marker) parks exactly as today.
        svc = chat_service
        task = _create_chat_task(svc, run, "Here's a thought, no decision yet.")

        assert run(svc.get_named_artifact(task.id, "handoff_suggestion")) is None
        assert run(svc.db.get_task(task.id)).status == "waiting"

    def test_latest_suggestion_wins_across_turns(self, chat_service, run):
        # Turn 1 recommends fix; turn 2 (operator kept chatting) recommends build.
        svc = chat_service
        svc.runner = SequentialFakeRunner(
            [
                _chat_result("AGENT_RESULT: COMPLETED fix"),
                _chat_result("On reflection, bigger than I thought.\nAGENT_RESULT: COMPLETED build"),
            ]
        )
        task = run(svc.create_task(message="Let's talk", process_name="chat"))
        run(wait_for_completion(svc, task.id))
        run(wait_for_status(svc, task.id, "waiting"))
        assert run(svc.get_named_artifact(task.id, "handoff_suggestion")) == "fix"

        # Keep chatting — the REPL is resumable after a suggestion.
        run(svc.send_message(task.id, "actually let's do the whole thing"))
        run(wait_for_completion(svc, task.id))
        run(wait_for_status(svc, task.id, "waiting"))
        assert run(svc.get_named_artifact(task.id, "handoff_suggestion")) == "build"

    def test_chat_message_stored_with_marker_stripped(self, chat_service, run):
        svc = chat_service
        task = _create_chat_task(
            svc, run, "I recommend the full build for this.\nAGENT_RESULT: COMPLETED build"
        )

        chat_msgs = run(svc.db.get_messages(task.id, msg_type="chat"))
        agent_msgs = [m for m in chat_msgs if m.role == "agent"]
        assert agent_msgs, "the handoff turn must still be stored as a chat message"
        content = agent_msgs[-1].content
        assert "I recommend the full build" in content
        assert "AGENT_RESULT: COMPLETED" not in content  # marker line stripped from display
        # Rich chat metadata is preserved on the handoff turn.
        assert agent_msgs[-1].metadata.get("agent_model") == "sonnet"

    def test_does_not_auto_promote(self, chat_service, run, monkeypatch):
        # The suggestion must never trigger promotion — that stays operator-only.
        svc = chat_service
        calls: list[tuple] = []
        real_promote = svc.promote_task

        async def _spy(task_id, to_process, initial_artifacts=None):
            calls.append((task_id, to_process))
            return await real_promote(task_id, to_process, initial_artifacts)

        monkeypatch.setattr(svc, "promote_task", _spy)
        _create_chat_task(svc, run, "AGENT_RESULT: COMPLETED build")
        assert calls == [], "the drainer must not auto-promote on a handoff suggestion"


# ---------------------------------------------------------------------------
# API surfacing (through the existing named-artifact exposure)
# ---------------------------------------------------------------------------


def test_task_detail_surfaces_handoff_suggestion(chat_service, run):
    """The captured suggestion reaches the frontend via the existing
    ``TaskDetailFullResponse.artifacts`` map — no bespoke API field.

    Fails pre-fix: the drainer never writes the ``handoff_suggestion`` artifact,
    so it is absent from the detail response.
    """
    from lotsa.server.api_routes import _build_task_detail

    svc = chat_service
    task = _create_chat_task(svc, run, "AGENT_RESULT: COMPLETED build")

    detail = run(_build_task_detail(svc, task.id))
    assert detail.artifacts.get("handoff_suggestion") == "build"
