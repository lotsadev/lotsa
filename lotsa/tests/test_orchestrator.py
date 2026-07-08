"""Tests for the headless OrchestratorService."""

from __future__ import annotations

import asyncio
from pathlib import Path

import pytest

from lotsa.config import LotsaConfig
from lotsa.db import TaskDB
from lotsa.flows import FlowStep, OutputRule
from lotsa.orchestrator import OPERATIONAL_PREAMBLE, OrchestratorService
from rigg.models import AgentResult


class FakeRunner:
    """Mock agent runner that returns a canned result."""

    def __init__(self, result: AgentResult | None = None):
        self.result = result or AgentResult(
            success=True,
            stdout="Agent output here",
            stderr="",
            return_code=0,
            duration_ms=1000,
            session_id="session-123",
        )
        self.calls: list[dict] = []

    def dispatch_shape_prompt(self) -> str:
        # AgentRunner protocol method (ADR-028 Phase 2); test double adds no fragment.
        return ""

    async def run(self, system_prompt, user_prompt, work_dir, **kwargs) -> AgentResult:
        self.calls.append(
            {
                "system_prompt": system_prompt,
                "user_prompt": user_prompt,
                "work_dir": work_dir,
                **kwargs,
            }
        )
        return self.result


class SequentialFakeRunner:
    """Mock agent runner that returns different results for each call."""

    def __init__(self, results: list[AgentResult]):
        self._results = results
        self.calls: list[dict] = []

    def dispatch_shape_prompt(self) -> str:
        # AgentRunner protocol method (ADR-028 Phase 2); test double adds no fragment.
        return ""

    async def run(self, system_prompt, user_prompt, work_dir, **kwargs) -> AgentResult:
        idx = len(self.calls)
        self.calls.append(
            {
                "system_prompt": system_prompt,
                "user_prompt": user_prompt,
                "work_dir": work_dir,
                **kwargs,
            }
        )
        if idx < len(self._results):
            return self._results[idx]
        # Fallback: return last result
        return self._results[-1]


def _make_config(data_dir: Path, flow: str = "simple") -> LotsaConfig:
    return LotsaConfig(
        data_dir=data_dir,
        work_dir=data_dir.parent,
        flow=flow,
        model="sonnet",
        budget=5.0,
    )


@pytest.fixture()
def _loop():
    loop = asyncio.new_event_loop()
    yield loop
    loop.close()


@pytest.fixture()
def run(_loop):
    return _loop.run_until_complete


@pytest.fixture()
def service(tmp_path, _loop, run):
    """Create an OrchestratorService with a FakeRunner and temp DB.

    Uses a single-step flow with evaluate=true so the task waits for
    human approval after the agent completes (matching the gated workflow).
    """
    data_dir = tmp_path / "tasks"
    data_dir.mkdir()
    # Single-step flow with evaluate gate — task waits for human approval after agent runs
    flow_yaml = tmp_path / "test_flow.yaml"
    flow_yaml.write_text("name: test\njobs:\n  - name: coding\n    evaluate: true\n")
    config = LotsaConfig(
        data_dir=data_dir,
        work_dir=data_dir.parent,
        flow="custom",
        flow_file=flow_yaml,
        model="sonnet",
        budget=5.0,
    )
    db = TaskDB(data_dir / "lotsa.db")
    run(db.initialize())

    svc = OrchestratorService(config, db)
    svc.runner = FakeRunner()

    run(svc.start())
    yield svc
    run(svc.shutdown())
    run(db.close())


class TestOperationalPreamble:
    """The preamble is the authoritative-layer rule block (ADR-025).

    A silent revert or truncation would leave the agent without git
    authority guardrails and the precedence-over-CLAUDE.md statement.
    These smoke tests catch that.
    """

    def test_names_precedence_over_project_claude_md(self):
        # The precedence statement is what makes the preamble win over
        # an emphatic project CLAUDE.md on operational matters.
        assert "take precedence over project" in OPERATIONAL_PREAMBLE

    def test_forbids_branch_operations(self):
        # The class of bug that PR #95 / b08a33d1 surfaced. If the
        # preamble loses this rule, the planning agent (or any other)
        # can drift back to git checkout -b feature/... and break the
        # worktree contract again.
        assert "Do not create, switch, rebase, or reset branches" in OPERATIONAL_PREAMBLE

    def test_forbids_push(self):
        # push_pr is orchestrator-owned. An agent push would race the
        # action step and produce ambiguous PR state.
        assert "Do not push" in OPERATIONAL_PREAMBLE

    def test_forbids_changing_working_directory(self):
        # The .git-introspection escape route: the worktree's .git
        # file points at the main checkout, and an agent that follows
        # it via `cd /path/to/main` can commit into the operator's
        # main repo. Observed on an internal task on 2026-06-08.
        assert "Do not change the working directory" in OPERATIONAL_PREAMBLE

    def test_keeps_needs_input_contract(self):
        # NEEDS_INPUT is how blocking questions reach the operator.
        # The orchestrator parses this marker; lose the rule and
        # agents drop questions silently into stdout.
        assert "NEEDS_INPUT" in OPERATIONAL_PREAMBLE

    def test_names_cross_turn_tools_explicitly(self):
        # Task b11cb04f used Monitor + ScheduleWakeup to defer work
        # after Bash background was locked down — the class is
        # "patterns that assume a long-lived interactive session,"
        # not just one instance. Each tool that fails must be named
        # explicitly so the agent knows in advance.
        #
        # ADR-028 Phase 2: this is CLI-shape-specific, so the naming now
        # lives in CLI_DISPATCH_SHAPE_FRAGMENT (appended after the universal
        # preamble for CLI runners), not in OPERATIONAL_PREAMBLE itself.
        from rigg.agent_runner import CLI_DISPATCH_SHAPE_FRAGMENT

        for tool in ("Monitor", "ScheduleWakeup", "Task", "BashOutput", "AskUserQuestion"):
            assert tool in CLI_DISPATCH_SHAPE_FRAGMENT, (
                f"{tool} not named in CLI dispatch-shape fragment; agent may reach for it without warning"
            )

    def test_explains_environment_not_just_rules(self):
        # The "Your environment" section gives the agent a model of
        # what dispatch shape it's running in, so it can reason
        # about edge cases rather than just following an enumerated
        # block-list. Without this framing, the agent reaches for
        # interactive patterns (e.g. AskUserQuestion → "dismissed")
        # and misinterprets the error results as user feedback.
        # Observed on an internal task on 2026-06-08.
        #
        # ADR-028 Phase 2: the dispatch-shape framing moved into the
        # CLI-shape fragment; the universal preamble no longer carries it.
        from rigg.agent_runner import CLI_DISPATCH_SHAPE_FRAGMENT

        assert "Your environment" in CLI_DISPATCH_SHAPE_FRAGMENT
        assert "No human is watching your output stream" in CLI_DISPATCH_SHAPE_FRAGMENT
        # The dismissal misinterpretation lesson, recorded so we
        # don't regress to "agent thinks dismissed = user dismissed".
        assert "dismissed" in CLI_DISPATCH_SHAPE_FRAGMENT

    def test_explains_how_to_communicate(self):
        # The "How to communicate with the operator" section gives
        # the agent the positive path: stdout for non-blocking
        # output, NEEDS_INPUT for blocking questions, dashboard
        # chat for non-blocking redirects. Pairs with the
        # environment section as the affirmative answer. Stays
        # universal across runner shapes (ADR-028 Phase 2).
        assert "How to communicate with the operator" in OPERATIONAL_PREAMBLE
        assert "audit log" in OPERATIONAL_PREAMBLE
        assert "Revision Feedback" in OPERATIONAL_PREAMBLE

    def test_dispatch_is_explicitly_one_shot(self):
        # The reason for forbidding cross-turn tools is structural —
        # `claude --print` exits at end_turn. The CLI-shape fragment must
        # explain *why* so the agent treats this as a fact, not a
        # preference (ADR-028 Phase 2: CLI-shape-specific).
        from rigg.agent_runner import CLI_DISPATCH_SHAPE_FRAGMENT

        assert "one-shot" in CLI_DISPATCH_SHAPE_FRAGMENT


class TestCreateTask:
    def test_creates_task_in_db(self, service, run):
        task = run(service.create_task("My Task", body="Do something"))
        assert task.title == "My Task"

        detail = run(service.get_task(task.id))
        assert detail is not None
        assert detail.title == "My Task"

    def test_dispatches_first_step(self, service, run):
        run(service.create_task("Build feature"))
        # Give the drainer a moment to process
        run(asyncio.sleep(0.1))

        runner = service.runner
        assert len(runner.calls) >= 1

    def test_create_task_from_message_auto_generates_title(self, service, run):
        """When message is provided without title, title is auto-generated."""
        long_message = (
            "Add a caching layer for connector queries. "
            "We're seeing 2-3s latency on repeated lookups and it's impacting "
            "the user experience significantly during peak hours."
        )
        task = run(service.create_task(message=long_message))
        # Title should be auto-generated from first sentence, <= 80 chars
        assert len(task.title) <= 80
        assert task.title == "Add a caching layer for connector queries"

    def test_create_task_from_message_stores_full_message_as_chat(self, service, run):
        """The full message (not truncated title) is stored as first chat message."""
        long_message = "Add a caching layer for connector queries. We're seeing 2-3s latency on repeated lookups."
        task = run(service.create_task(message=long_message))
        # The full message should be stored as the first user chat message
        messages = run(service.db.get_messages(task.id, msg_type="chat"))
        assert len(messages) >= 1
        assert messages[0].content == long_message
        assert messages[0].role == "user"

    def test_create_task_from_message_truncates_long_first_sentence(self, service, run):
        """If first sentence exceeds 80 chars, it gets truncated."""
        long_message = (
            "Implement a comprehensive distributed caching strategy using Redis "
            "that handles invalidation across multiple service instances correctly"
        )
        task = run(service.create_task(message=long_message))
        assert len(task.title) <= 80

    def test_create_task_from_message_uses_first_line(self, service, run):
        """If message has newlines, use first line for title."""
        message = "Fix the login bug\nThe OAuth flow is broken when users try SSO."
        task = run(service.create_task(message=message))
        assert task.title == "Fix the login bug"

    def test_create_task_with_explicit_title_still_works(self, service, run):
        """Backward compat: explicit title parameter still works."""
        task = run(service.create_task(title="Explicit Title", body="Some body"))
        assert task.title == "Explicit Title"


class TestArtifactCapture:
    def test_conversational_step_no_artifact_until_approve(self, service, run):
        """Conversational steps don't save artifacts on completion — only on approve."""
        from lotsa.flows import build_process

        # ADR-021: dispatch resolves the task's process from ``_processes``, so
        # swap the active catalog entry (not just ``self.flow``) to retarget.
        proc = build_process("build")
        service.process = proc
        service._processes[service._active_process_name] = proc
        service.flow = proc.flows.get("main") or next(iter(proc.flows.values()))

        task = run(service.create_task("Artifact test"))
        run(asyncio.sleep(0.3))

        # Spec step is conversational — no artifact yet (saved on approve)
        messages = run(service.db.get_messages(task.id, msg_type="artifact"))
        assert len(messages) == 0

    def test_non_conversational_stdout_saved_as_artifact(self, service, run):
        """Non-conversational steps with output save stdout as named artifact."""
        from lotsa.flows import build_process

        # Build a single-step flow with output configured, using bundled coding prompts
        flow_yaml = service.config.work_dir / "artifact_flow.yaml"
        flow_yaml.write_text("name: artifact-test\njobs:\n  - name: coding\n    output: plan\n")
        # ADR-021: retarget the active catalog entry so create_task dispatches it.
        proc = build_process("custom", process_file=flow_yaml)
        service.process = proc
        service._processes[service._active_process_name] = proc
        service.flow = proc.flows.get("main") or next(iter(proc.flows.values()))

        service.runner = FakeRunner(
            AgentResult(
                success=True,
                stdout="# Implementation Plan\n\nDo the thing in three steps.",
                stderr="",
                return_code=0,
                duration_ms=100,
            )
        )
        task = run(service.create_task("Plan test"))
        run(asyncio.sleep(0.3))

        messages = run(service.db.get_messages(task.id, msg_type="artifact"))
        assert len(messages) >= 1
        assert messages[-1].metadata.get("artifact_name") == "plan"
        assert "Implementation Plan" in messages[-1].content

    def test_artifact_capture_strips_leading_narration(self, service, run):
        """Narration before the content anchor is stripped at capture.

        Task ``c94e3ed9``: the pr_summary agent's "I have enough to write the
        PR description." opener was persisted verbatim and became the PR
        title. The artifact must start at the first heading / CC-title line.
        """
        from lotsa.flows import build_process

        flow_yaml = service.config.work_dir / "narration_flow.yaml"
        flow_yaml.write_text("name: narration-test\njobs:\n  - name: coding\n    output: pr_description\n")
        proc = build_process("custom", process_file=flow_yaml)
        service.process = proc
        service._processes[service._active_process_name] = proc
        service.flow = proc.flows.get("main") or next(iter(proc.flows.values()))

        service.runner = FakeRunner(
            AgentResult(
                success=True,
                stdout=(
                    "I have enough to write the PR description.\n"
                    "\n"
                    "feat(changes-tab): render PR-style git diff\n"
                    "\n"
                    "Replace the raw-text view with a unified diff.\n"
                ),
                stderr="",
                return_code=0,
                duration_ms=100,
            )
        )
        task = run(service.create_task("Narration test"))
        run(asyncio.sleep(0.3))

        messages = run(service.db.get_messages(task.id, msg_type="artifact"))
        assert len(messages) >= 1
        content = messages[-1].content
        assert content.startswith("feat(changes-tab): render PR-style git diff")
        assert "I have enough" not in content

    def test_artifact_capture_blocks_on_unusable_output(self, service, run):
        """Narration-only / tiny stdout fails the dispatch instead of saving
        a garbage artifact — task lands blocked so Retry re-runs the agent."""
        from lotsa.flows import build_process

        flow_yaml = service.config.work_dir / "tiny_artifact_flow.yaml"
        flow_yaml.write_text("name: tiny-test\njobs:\n  - name: coding\n    output: plan\n")
        proc = build_process("custom", process_file=flow_yaml)
        service.process = proc
        service._processes[service._active_process_name] = proc
        service.flow = proc.flows.get("main") or next(iter(proc.flows.values()))

        service.runner = FakeRunner(
            AgentResult(
                success=True,
                stdout="Done.",
                stderr="",
                return_code=0,
                duration_ms=100,
            )
        )
        task = run(service.create_task("Tiny artifact test"))
        run(asyncio.sleep(0.3))

        messages = run(service.db.get_messages(task.id, msg_type="artifact"))
        assert len(messages) == 0, "an unusable artifact must not be persisted"
        row = run(service.db.get_task(task.id))
        assert row.status == "blocked"


class TestArtifactInputValidation:
    def test_missing_input_blocks_task(self, service, run):
        """Step with inputs requirement blocks if artifact doesn't exist."""
        from lotsa.flows import build_process

        # Flow where coding step requires a "spec" artifact that doesn't exist
        flow_yaml = service.config.work_dir / "input_flow.yaml"
        flow_yaml.write_text("name: input-test\njobs:\n  - name: coding\n    inputs: [spec]\n")
        # ADR-021: retarget the active catalog entry so create_task dispatches it.
        proc = build_process("custom", process_file=flow_yaml)
        service.process = proc
        service._processes[service._active_process_name] = proc
        service.flow = proc.flows.get("main") or next(iter(proc.flows.values()))

        task = run(service.create_task("Input test"))
        run(asyncio.sleep(0.3))

        # Task should be blocked due to missing artifact
        updated = run(service.db.get_task(task.id))
        assert updated.state == "blocked"

        # Error message should mention the missing artifact
        messages = run(service.db.get_messages(task.id, msg_type="error"))
        assert len(messages) >= 1
        assert "spec" in messages[-1].content


class TestListTasks:
    def test_lists_created_tasks(self, service, run):
        run(service.create_task("Task A"))
        run(service.create_task("Task B"))
        run(asyncio.sleep(0.1))

        tasks = run(service.list_tasks_async())
        assert len(tasks) == 2


class TestApprove:
    def test_approve_waiting_task(self, service, run):
        run(service.create_task("Approve me"))
        # Wait for agent to complete and enter waiting
        run(asyncio.sleep(0.2))

        tasks = run(service.list_tasks_async())
        task_id = tasks[0].id

        # Should be in waiting now (DB is source of truth)
        fresh = run(service.db.get_task(task_id))
        assert fresh is not None
        assert fresh.status == "waiting"

        run(service.approve(task_id))

        # Message should be recorded
        messages = run(service.get_messages(task_id))
        feedback_msgs = [m for m in messages if m.type == "feedback"]
        assert any(m.content == "Approved" for m in feedback_msgs)

    def test_approve_from_needs_input_at_evaluate_gate(self, service, run):
        """An agent counter-question (NEEDS_INPUT) at an evaluate gate must not
        trap the operator: Accept works from needs_input when the gate's
        output artifact exists. Task c59515ab: the plan agent answered a
        clarification, asked its own question, and the Accept button vanished.
        """
        task = run(service.db.create_task("Gate Task", state="coding"))
        step = FlowStep(
            name="plan",
            prompt_name="plan",
            resume_session=False,
            evaluate=True,  # it's a gate
            queue_state="backlog",
            active_state="coding",
            success_state="complete",
            output="plan",
        )
        service.flow.jobs.append(step)
        run(
            service.db.add_message(
                task.id, "agent", "plan", "# Plan\n- step", "artifact", metadata={"artifact_name": "plan"}
            )
        )
        # As the agent left it after emitting NEEDS_INPUT mid-gate.
        run(service.db.update_task(task.id, status="needs_input", current_step="plan", state="coding"))

        run(service.approve(task.id))

        updated = run(service.db.get_task(task.id))
        assert updated is not None
        assert updated.status != "needs_input", "accept must advance past the gate"
        messages = run(service.get_messages(task.id))
        assert any("accepted past the agent's open question" in m.content for m in messages if m.type == "feedback")

    def test_approve_from_needs_input_rejected_for_non_gate_step(self, service, run):
        """A non-evaluate step that emitted NEEDS_INPUT (e.g. coding asked a
        question) has no gate to accept — approve() must reject it so the
        operator answers instead."""
        from lotsa.orchestrator import ApproveNotAllowed

        task = run(service.db.create_task("Non-gate Task", state="coding"))
        step = FlowStep(
            name="code",
            prompt_name="coding",
            resume_session=False,
            evaluate=False,  # NOT a gate
            queue_state="backlog",
            active_state="coding",
            success_state="complete",
        )
        service.flow.jobs.append(step)
        run(service.db.update_task(task.id, status="needs_input", current_step="code", state="coding"))

        with pytest.raises(ApproveNotAllowed, match="evaluate gate"):
            run(service.approve(task.id))

    def test_approve_rejected_for_non_gate_waiting_step(self, service, run):
        """A WAITING step that's neither an evaluate gate nor has an output to
        accept (the chat REPL) is not an approval gate — approve() must reject it
        (it used to complete the task), so the chat panel shows no Accept button."""
        from lotsa.orchestrator import ApproveNotAllowed

        task = run(service.db.create_task("Chat REPL", state="chat"))
        step = FlowStep(
            name="chat",
            prompt_name="chat",
            resume_session=False,
            evaluate=False,  # not a gate
            queue_state="chat",
            active_state="chat",
            success_state="complete",
        )
        service.flow.jobs.append(step)
        run(service.db.update_task(task.id, status="waiting", current_step="chat", state="chat"))

        with pytest.raises(ApproveNotAllowed, match="not an approval gate"):
            run(service.approve(task.id))

    def test_approve_advances_conversational_verify_gate(self, service, run):
        """Regression: verify is conversational with a forward (^VERIFIED:→next)
        rule but no output artifact and is not an evaluate gate. The Accept-on-chat
        fix narrowed the gate test to ``output or evaluate``, which dropped verify's
        Accept button and made approve() reject it. A conversational step with a
        next-rule IS an accept-gate — approve() must advance it (it used to raise)."""
        from lotsa.flows import OutputRule

        task = run(service.db.create_task("Verify Gate", state="coding"))
        step = FlowStep(
            name="verify",
            prompt_name="verify",
            resume_session=False,
            evaluate=False,  # not an evaluate gate
            queue_state="backlog",
            active_state="coding",
            success_state="complete",
            conversational=True,
            rules=[OutputRule(source="stdout", pattern="^VERIFIED:", target="next")],
            # no output artifact — gates on the forward rule alone
        )
        service.flow.jobs.append(step)
        run(service.db.update_task(task.id, status="waiting", current_step="verify", state="coding"))

        run(service.approve(task.id))  # must NOT raise (the regression made it reject)

        updated = run(service.db.get_task(task.id))
        assert updated is not None
        assert updated.status != "waiting", "Accept must advance past the conversational verify gate"


class TestRevise:
    def test_revise_re_dispatches(self, service, run):
        run(service.create_task("Revise me"))
        run(asyncio.sleep(0.2))

        tasks = run(service.list_tasks_async())
        task_id = tasks[0].id
        fresh = run(service.db.get_task(task_id))
        assert fresh is not None
        assert fresh.status == "waiting"

        initial_calls = len(service.runner.calls)
        run(service.revise(task_id, "Please reconsider"))
        run(asyncio.sleep(0.2))

        # Should have dispatched again (agent re-ran)
        assert len(service.runner.calls) > initial_calls

        # Feedback message recorded
        messages = run(service.get_messages(task_id))
        feedback_msgs = [m for m in messages if m.type == "feedback" and m.role == "user"]
        assert any("reconsider" in m.content for m in feedback_msgs)


class TestAnswer:
    def test_answer_needs_input(self, service, run):
        # Use a runner that produces NEEDS_INPUT
        service.runner = FakeRunner(
            AgentResult(
                success=True,
                stdout="Analysis complete.\nNEEDS_INPUT: Should we use PostgreSQL?",
                stderr="",
                return_code=0,
                duration_ms=500,
            )
        )

        run(service.create_task("Question task"))
        run(asyncio.sleep(0.2))

        tasks = run(service.list_tasks_async())
        task_id = tasks[0].id

        # Should have a question
        question = run(service.get_question(task_id))
        assert question == "Should we use PostgreSQL?"

        # Answer it
        initial_calls = len(service.runner.calls)
        run(service.answer(task_id, "Yes, use PostgreSQL"))
        run(asyncio.sleep(0.1))

        assert len(service.runner.calls) > initial_calls
        messages = run(service.get_messages(task_id))
        answer_msgs = [m for m in messages if m.type == "answer"]
        assert any("PostgreSQL" in m.content for m in answer_msgs)


class TestBlock:
    def test_block_task(self, service, run):
        run(service.create_task("Block me"))
        run(asyncio.sleep(0.2))

        tasks = run(service.list_tasks_async())
        task_id = tasks[0].id

        run(service.block(task_id))

        detail = run(service.get_task(task_id))
        assert detail is not None
        assert detail.state == "blocked"


class TestDrainerErrorSafety:
    def test_completion_processing_error_blocks_task_not_strands_working(self, service, run):
        """A swallowed completion-processing error must block the task, not
        silently drop the completion and strand it at status=working.

        Regression for the working-orphan that hit an internal task (stuck for an extended period):
        the drainer's ``except Exception`` logged and continued, so a task whose
        completion raised was left ``status=working`` with no agent in flight —
        not retryable, only recoverable by a later restart.

        Force the raise via the ``session_id`` metadata merge, which happens
        ONLY in completion processing (never dispatch), so it's deterministic.
        Pre-fix: task stays ``working``. Post-fix: the safety net blocks it.
        """
        svc = service
        svc.runner = FakeRunner(
            AgentResult(
                success=True,
                stdout="done",
                stderr="",
                return_code=0,
                duration_ms=10,
                session_id="sess-x",
            )
        )

        orig_merge = svc._merge_task_metadata

        async def boom_merge(item, updates):
            if "session_id" in updates:
                raise RuntimeError("simulated completion-processing failure")
            return await orig_merge(item, updates)

        svc._merge_task_metadata = boom_merge  # type: ignore[assignment]

        task = run(svc.create_task("will fail in drainer"))
        run(asyncio.sleep(0.3))  # dispatch → agent completes → drainer processes

        fresh = run(svc.db.get_task(task.id))
        assert fresh.status == "blocked", (
            "a completion-processing error must block the task (visible + retryable), "
            f"not strand it at status=working; got status={fresh.status!r}"
        )
        # State is preserved (status→blocked, state kept), mirroring restart
        # recovery — this is an infrastructure failure, not the agent-exit path
        # that moves to the ``blocked`` SM sink. The atomic_transition is a
        # state-preserving self-transition so retry resumes the same step.
        assert fresh.state != "blocked", (
            "completion-error block must preserve state (mirror restart recovery), "
            f"not move to the blocked SM sink; got state={fresh.state!r}"
        )
        msgs = run(svc.db.get_messages(task.id))
        assert any("Completion processing failed" in m.content for m in msgs), (
            "the block must leave an explanatory audit message"
        )


class TestRetry:
    def test_retry_blocked_task(self, service, run):
        # Create a task that will fail
        service.runner = FakeRunner(
            AgentResult(success=False, stdout="", stderr="Error", return_code=1, duration_ms=100)
        )
        run(service.create_task("Will fail"))
        run(asyncio.sleep(0.2))

        tasks = run(service.list_tasks_async())
        task_id = tasks[0].id

        detail = run(service.get_task(task_id))
        assert detail is not None
        assert detail.state == "blocked"

        # Switch to a successful runner and retry
        service.runner = FakeRunner()
        run(service.retry(task_id))
        run(asyncio.sleep(0.2))

        detail = run(service.get_task(task_id))
        assert detail is not None
        assert detail.state != "blocked"


class TestRunAgentExceptionRecovery:
    """When the runner raises mid-dispatch, the task must not get stuck at
    status='working'. The drainer should still observe a completion event
    (with success=False) and CAS the row to 'blocked'."""

    def test_runner_exception_lands_task_in_blocked(self, service, run):
        class RaisingRunner:
            calls = 0

            def dispatch_shape_prompt(self) -> str:
                return ""

            async def run(self, **kwargs):
                RaisingRunner.calls += 1
                raise RuntimeError("simulated runner crash")

        service.runner = RaisingRunner()
        run(service.create_task("Will crash"))
        run(asyncio.sleep(0.2))

        tasks = run(service.list_tasks_async())
        task_id = tasks[0].id

        detail = run(service.get_task(task_id))
        assert detail is not None
        assert detail.status == "blocked", f"expected blocked, got status={detail.status!r}"
        assert task_id not in service._in_flight, "in-flight slot leaked after runner crash"

        # The synthetic failure stderr is surfaced as an error message.
        messages = run(service.get_messages(task_id))
        err_msgs = [m for m in messages if m.type == "error"]
        assert any("RuntimeError" in m.content for m in err_msgs)


class TestConversationalRules:
    def test_extracts_full_spec_from_rule(self):
        from lotsa.flows import check_conversational_rules

        step = FlowStep(
            name="spec",
            prompt_name="spec",
            resume_session=False,
            evaluate=False,
            queue_state="backlog",
            active_state="spec",
            success_state="backlog",
            conversational=True,
            rules=[OutputRule(source="stdout", pattern="^SPEC_COMPLETE:")],
        )
        stdout = "Discussion...\nSPEC_COMPLETE: Build a cache\n## Requirements\n- Fast"
        result = check_conversational_rules(step, stdout)
        assert result is not None
        assert "SPEC_COMPLETE:" in result
        assert "## Requirements" in result

    def test_no_match_returns_none(self):
        from lotsa.flows import check_conversational_rules

        step = FlowStep(
            name="spec",
            prompt_name="spec",
            resume_session=False,
            evaluate=False,
            queue_state="backlog",
            active_state="spec",
            success_state="backlog",
            conversational=True,
            rules=[OutputRule(source="stdout", pattern="^SPEC_COMPLETE:")],
        )
        assert check_conversational_rules(step, "Just a response") is None

    def test_no_rules_returns_none(self):
        from lotsa.flows import check_conversational_rules

        step = FlowStep(
            name="spec",
            prompt_name="spec",
            resume_session=False,
            evaluate=False,
            queue_state="backlog",
            active_state="spec",
            success_state="backlog",
            conversational=True,
        )
        assert check_conversational_rules(step, "SPEC_COMPLETE: foo") is None


class TestApproveConversational:
    def test_approve_saves_spec_as_artifact_and_body(self, service, run):
        task = run(service.db.create_task("Spec Task", state="speccing"))

        # Use states that exist in the simple flow's state machine
        step = FlowStep(
            name="spec",
            prompt_name="spec",
            resume_session=False,
            evaluate=False,
            queue_state="backlog",
            active_state="coding",
            success_state="complete",
            conversational=True,
            output="spec",
            rules=[OutputRule(source="stdout", pattern="^SPEC_COMPLETE:")],
        )
        # Inject step into flow so approve() can find it by current_step name
        service.flow.jobs.append(step)

        # Pre-stage artifact + body as the drainer would have done in production.
        # Use db.add_message directly to bypass the SQLiteItemSource wrapper in test setup.
        run(
            service.db.add_message(
                task.id,
                "agent",
                "spec",
                "## Requirements\n- Fast",
                "artifact",
                metadata={"artifact_name": "spec"},
            )
        )
        run(service.db.update_task(task.id, body="## Requirements\n- Fast"))
        run(service.db.update_task(task.id, status="waiting", current_step="spec", state="coding"))

        run(service.approve(task.id))

        # Task body should have spec content without the marker line
        updated = run(service.db.get_task(task.id))
        assert updated is not None
        assert "## Requirements" in updated.body
        assert "SPEC_COMPLETE" not in updated.body

        # Artifact message should exist
        messages = run(service.db.get_messages(task.id, msg_type="artifact"))
        assert len(messages) >= 1
        assert "## Requirements" in messages[-1].content


class TestChatMessageMetadata:
    """Test that conversational step chat messages include execution metadata."""

    @pytest.fixture()
    def conv_service(self, tmp_path, _loop, run):
        """Service with a single conversational step."""
        flow_yaml = tmp_path / "conv_flow.yaml"
        flow_yaml.write_text(
            "name: conv-test\njobs:\n"
            "  - name: spec\n    prompt: spec\n    conversational: true\n"
            "    queue_state: backlog\n    active_state: spec\n"
        )
        prompts_dir = tmp_path / "prompts"
        prompts_dir.mkdir()
        (prompts_dir / "spec-system.md").write_text("# Spec\n{title}\n{body}")
        (prompts_dir / "spec-user.md").write_text("{title}\n{body}")
        config = LotsaConfig(
            data_dir=tmp_path / "data",
            work_dir=tmp_path,
            flow="custom",
            flow_file=flow_yaml,
            prompts_dir=prompts_dir,
            model="sonnet",
            budget=5.0,
        )
        (tmp_path / "data").mkdir()
        db = TaskDB(tmp_path / "data" / "lotsa.db")
        run(db.initialize())
        svc = OrchestratorService(config, db)
        run(svc.start())
        yield svc
        run(svc.shutdown())
        run(db.close())

    def test_metadata_includes_all_fields(self, conv_service, run):
        """Chat message metadata should include duration, model, tokens, cost."""
        svc = conv_service
        svc.runner = FakeRunner(
            AgentResult(
                success=True,
                stdout="Here is my response",
                stderr="",
                return_code=0,
                duration_ms=1500,
                model="sonnet",
                session_id="sess-1",
                input_tokens=840,
                output_tokens=400,
                cost_usd=0.012,
            )
        )
        task = run(svc.create_task("Metadata test"))
        run(asyncio.sleep(0.3))

        messages = run(svc.db.get_messages(task.id, msg_type="chat"))
        agent_msgs = [m for m in messages if m.role == "agent"]
        assert len(agent_msgs) >= 1
        meta = agent_msgs[-1].metadata
        assert meta["duration_ms"] == 1500
        assert meta["model"] == "sonnet"
        assert meta["input_tokens"] == 840
        assert meta["output_tokens"] == 400
        assert meta["cost_usd"] == 0.012
        # ADR-023 — the former class-name ``runner`` field is replaced by the
        # registered runner name (``agent_runner``); ``default`` for the
        # injected per-instance runner.
        assert meta["agent_runner"] == "default"

    def test_metadata_omits_none_fields(self, conv_service, run):
        """Metadata should omit token/cost fields when they are None."""
        svc = conv_service
        svc.runner = FakeRunner(
            AgentResult(
                success=True,
                stdout="Response without tokens",
                stderr="",
                return_code=0,
                duration_ms=500,
                model="sonnet",
                session_id="sess-2",
            )
        )
        task = run(svc.create_task("No tokens test"))
        run(asyncio.sleep(0.3))

        messages = run(svc.db.get_messages(task.id, msg_type="chat"))
        agent_msgs = [m for m in messages if m.role == "agent"]
        assert len(agent_msgs) >= 1
        meta = agent_msgs[-1].metadata
        assert meta["duration_ms"] == 500
        assert meta["model"] == "sonnet"
        assert "input_tokens" not in meta
        assert "output_tokens" not in meta
        assert "cost_usd" not in meta


class TestSendMessage:
    def test_sends_message_to_draft_task(self, service, run):
        step = service.flow.steps[0]
        # Create task in the step's active_state (as the drainer would leave it)
        task = run(service.db.create_task("Draft Task", state=step.active_state))

        # Set status to waiting and current_step before calling send_message
        run(service.db.update_task(task.id, status="waiting", current_step=step.name))

        run(service.send_message(task.id, "User follow-up"))

        messages = run(service.db.get_messages(task.id, msg_type="chat"))
        user_msgs = [m for m in messages if m.role == "user"]
        assert len(user_msgs) == 1
        assert user_msgs[0].content == "User follow-up"

    def test_sends_message_to_stopped_task(self, service, run):
        """Stop → amend → resume: send_message accepts status='blocked'.

        stop() parks a task at blocked preserving state/current_step; the
        operator's natural next move is a corrected message, not a bare
        Retry that re-runs the step without their input.
        """
        step = service.flow.steps[0]
        task = run(service.db.create_task("Stopped Task", state=step.active_state))
        # As stop() leaves it: blocked, state/current_step preserved.
        run(service.db.update_task(task.id, status="blocked", current_step=step.name))

        run(service.send_message(task.id, "Actually, do it this other way"))

        messages = run(service.db.get_messages(task.id, msg_type="chat"))
        user_msgs = [m for m in messages if m.role == "user"]
        assert len(user_msgs) == 1
        assert user_msgs[0].content == "Actually, do it this other way"
        row = run(service.db.get_task(task.id))
        # The instant FakeRunner may already have completed the dispatch
        # (blocked → working → waiting); the invariant is that the message
        # un-parked the task.
        assert row.status != "blocked", "send_message from blocked must re-dispatch"

    def test_send_message_rejects_terminal_status(self, service, run):
        from lotsa.orchestrator import ReviseNotAllowed

        step = service.flow.steps[0]
        task = run(service.db.create_task("Done Task", state=step.active_state))
        run(service.db.update_task(task.id, status="complete", current_step=step.name))

        with pytest.raises(ReviseNotAllowed, match="requires status"):
            run(service.send_message(task.id, "too late"))


class TestExtractNeedsInput:
    def test_extracts_last_match(self):
        from lotsa.orchestrator import _extract_needs_input

        stdout = "Line 1\nNEEDS_INPUT: First question\nLine 3\nNEEDS_INPUT: Second question"
        assert _extract_needs_input(stdout) == "Second question"

    def test_no_match(self):
        from lotsa.orchestrator import _extract_needs_input

        assert _extract_needs_input("No questions here") is None


class TestFeedbackIsActionable:
    """The guard that decides whether a pr-fix skip counts toward the cap
    (fix for an internal task — benign skips must not burn max_consecutive_skipped)."""

    def test_real_feedback_is_actionable(self):
        from lotsa.orchestrator import _feedback_is_actionable

        assert _feedback_is_actionable("### PR Comments\n\nMedium: fix the thing") is True

    def test_empty_and_none_are_benign(self):
        from lotsa.orchestrator import _feedback_is_actionable

        assert _feedback_is_actionable(None) is False
        assert _feedback_is_actionable("") is False
        assert _feedback_is_actionable("   \n  ") is False

    def test_aggregate_sentinel_is_benign(self):
        from lotsa.orchestrator import _feedback_is_actionable

        # aggregate_feedback returns this exact string when nothing is pending.
        assert _feedback_is_actionable("No specific feedback found.") is False

    def test_conflicts_resolved_echo_is_benign(self):
        """A pr-fix dispatched right after resolve_conflicts is fed that agent's
        stdout (the CONFLICTS_RESOLVED report) as feedback via the rule-route
        carry-forward (feedback=result.stdout). Skipping that echo is benign —
        the conflict is already resolved, nothing new for pr-fix to do — so it
        must not count toward max_consecutive_skipped (internal tasks / 04ee0735).
        """
        from lotsa.orchestrator import _feedback_is_actionable

        echo = (
            "Resolved the conflict in `CLAUDE.md` — merged the two adjacent ADR-index rows.\n"
            "CONFLICTS_RESOLVED: merged ADR index rows"
        )
        assert _feedback_is_actionable(echo) is False


class TestGatherPendingPrFeedback:
    """retry/jump feedback re-resolution (fix #2): orchestrator re-aggregates the
    PR's current feedback when a pr-fix dispatch has no explicit operator text."""

    def test_returns_none_without_engine_or_coords(self, service, run):
        """No monitor engine (simple flow) and/or no PR coords → None, not raise.
        Callers then dispatch with no feedback (a benign skip)."""
        task = run(service.db.create_task("No coords"))
        row = run(service.db.get_task(task.id))
        assert run(service._gather_pending_pr_feedback(row)) is None

    def test_returns_aggregated_feedback_when_engine_has_pending(self, service, run, monkeypatch):
        """With coords + token + an engine that has pending feedback, the helper
        returns the aggregated text (the value retry() then injects)."""
        from unittest.mock import AsyncMock

        monkeypatch.setenv("GITHUB_TOKEN", "tok")
        task = run(
            service.db.create_task(
                "With coords",
                metadata={"pr_number": 7, "github_owner": "o", "github_repo": "r"},
            )
        )
        row = run(service.db.get_task(task.id))

        class _FakeEngine:
            gather_pending_feedback = AsyncMock(return_value="### PR Comments\n\nMedium: do X")

        monkeypatch.setattr(service, "_monitor_engine_for", lambda _task: _FakeEngine())
        assert run(service._gather_pending_pr_feedback(row)) == "### PR Comments\n\nMedium: do X"

    def test_empty_pending_coerces_to_none(self, service, run, monkeypatch):
        """An engine that returns '' (nothing pending) → None, so the caller
        treats it as no feedback (benign skip), not an empty-string dispatch."""
        from unittest.mock import AsyncMock

        monkeypatch.setenv("GITHUB_TOKEN", "tok")
        task = run(
            service.db.create_task(
                "Empty pending",
                metadata={"pr_number": 7, "github_owner": "o", "github_repo": "r"},
            )
        )
        row = run(service.db.get_task(task.id))

        class _FakeEngine:
            gather_pending_feedback = AsyncMock(return_value="")

        monkeypatch.setattr(service, "_monitor_engine_for", lambda _task: _FakeEngine())
        assert run(service._gather_pending_pr_feedback(row)) is None


class TestWorktreeLifecycle:
    """Tests that verify worktree integration in the orchestrator."""

    @pytest.fixture()
    def git_service(self, tmp_path, _loop, run):
        """Service with a real git repo as work_dir so worktrees work."""
        import subprocess

        # Create a real git repo
        repo = tmp_path / "repo"
        repo.mkdir()
        subprocess.run(["git", "init", str(repo)], capture_output=True, check=True)
        subprocess.run(
            ["git", "-C", str(repo), "config", "user.email", "test@test.com"],
            capture_output=True,
            check=True,
        )
        subprocess.run(
            ["git", "-C", str(repo), "config", "user.name", "Test"],
            capture_output=True,
            check=True,
        )
        (repo / "README.md").write_text("# Test")
        subprocess.run(["git", "-C", str(repo), "add", "."], capture_output=True, check=True)
        subprocess.run(["git", "-C", str(repo), "commit", "-m", "init"], capture_output=True, check=True)

        data_dir = tmp_path / "data"
        data_dir.mkdir()
        # Use evaluate: true so task waits for approval (doesn't auto-complete)
        flow_yaml = tmp_path / "gated_flow.yaml"
        flow_yaml.write_text("name: gated\njobs:\n  - name: coding\n    evaluate: true\n")
        config = LotsaConfig(
            data_dir=data_dir,
            work_dir=repo,
            flow="custom",
            flow_file=flow_yaml,
            model="sonnet",
            budget=5.0,
        )
        db = TaskDB(data_dir / "lotsa.db")
        run(db.initialize())

        svc = OrchestratorService(config, db)
        svc.runner = FakeRunner()
        run(svc.start())
        yield svc
        run(svc.shutdown())
        run(db.close())

    def test_worktree_created_on_dispatch(self, git_service, run):
        svc = git_service
        run(svc.create_task("Worktree test"))
        run(asyncio.sleep(0.1))

        # Check that a worktree was created (namespaced under the project, ADR-029)
        worktrees_dir = svc.config.data_dir / "worktrees" / "default"
        assert worktrees_dir.exists()
        entries = list(worktrees_dir.iterdir())
        assert len(entries) == 1
        assert (entries[0] / ".git").exists()  # valid worktree

    def test_worktree_used_as_work_dir(self, git_service, run):
        svc = git_service
        run(svc.create_task("Work dir test"))
        run(asyncio.sleep(0.2))

        # The runner should have been called with a worktree path, not the main repo
        assert len(svc.runner.calls) >= 1
        call = svc.runner.calls[0]
        assert call["work_dir"] != svc.config.work_dir
        assert "worktrees" in str(call["work_dir"])

    def test_worktree_fallback_on_failure(self, git_service, run):
        svc = git_service

        # Patch create to always fail
        async def _fail(*args, **kwargs):
            raise RuntimeError("Worktree creation failed")

        svc._worktree_managers["default"].create = _fail

        run(svc.create_task("Fallback test"))
        run(asyncio.sleep(0.2))

        # Runner should still be called, with the project work_dir as fallback
        # (ADR-029 — resolve via the task's project root; compare resolved to be
        # robust to symlinked temp dirs like /var → /private/var on macOS).
        assert len(svc.runner.calls) >= 1
        assert Path(svc.runner.calls[0]["work_dir"]).resolve() == Path(svc.config.work_dir).resolve()

    def test_worktree_removed_on_complete(self, git_service, run):
        svc = git_service
        run(svc.create_task("Cleanup test"))
        run(asyncio.sleep(0.2))

        # Worktree should exist after dispatch (namespaced under the project)
        worktrees_dir = svc.config.data_dir / "worktrees" / "default"
        entries = list(worktrees_dir.iterdir()) if worktrees_dir.exists() else []
        assert len(entries) == 1
        task_id = entries[0].name

        # Wait for completion drainer, then approve to advance to complete
        tasks = run(svc.list_tasks_async())
        run(svc.approve(tasks[0].id))
        run(asyncio.sleep(0.2))

        # Worktree should be cleaned up (task reached "complete" in simple flow)
        assert not (worktrees_dir / task_id).exists()

    def test_worktree_preserved_on_block(self, git_service, run):
        svc = git_service
        run(svc.create_task("Block test"))
        run(asyncio.sleep(0.2))

        # Worktree should exist (namespaced under the project)
        worktrees_dir = svc.config.data_dir / "worktrees" / "default"
        entries = list(worktrees_dir.iterdir()) if worktrees_dir.exists() else []
        assert len(entries) == 1
        task_id = entries[0].name

        # Block the task
        tasks = run(svc.list_tasks_async())
        run(svc.block(tasks[0].id))
        run(asyncio.sleep(0.1))

        # Worktree should still exist (blocked != complete)
        assert (worktrees_dir / task_id).exists()


class TestOutputRuleRouting:
    """Test that output rules route tasks correctly (e.g. REVIEW_FAIL → code)."""

    @pytest.fixture()
    def rule_service(self, tmp_path, _loop, run):
        """Service with a two-step flow: code → review (with REVIEW_FAIL → code rule)."""
        flow_yaml = tmp_path / "rule_flow.yaml"
        flow_yaml.write_text(
            "name: rule-test\njobs:\n"
            "  - name: code\n    prompt: coding\n    resume: true\n"
            "    queue_state: backlog\n    active_state: coding\n"
            "  - name: review\n    prompt: review\n"
            "    queue_state: reviewing\n    active_state: reviewing\n"
            "    rules:\n"
            "      - source: stdout\n        pattern: '^REVIEW_PASS'\n        target: next\n"
            "      - source: stdout\n        pattern: '^REVIEW_FAIL'\n        target: code\n"
        )
        # Create stub prompt files for the custom flow
        prompts_dir = tmp_path / "prompts"
        prompts_dir.mkdir()
        for name in ("coding-system", "coding-user", "review-system", "review-user"):
            (prompts_dir / f"{name}.md").write_text(f"# {name}\n{{title}}\n{{body}}")
        config = LotsaConfig(
            data_dir=tmp_path / "data",
            work_dir=tmp_path,
            flow="custom",
            flow_file=flow_yaml,
            prompts_dir=prompts_dir,
            model="sonnet",
            budget=5.0,
        )
        (tmp_path / "data").mkdir()
        db = TaskDB(tmp_path / "data" / "lotsa.db")
        run(db.initialize())

        svc = OrchestratorService(config, db)
        svc.runner = FakeRunner()
        run(svc.start())
        yield svc
        run(svc.shutdown())
        run(db.close())

    def test_review_fail_routes_to_code(self, rule_service, run):
        """REVIEW_FAIL output should re-dispatch the code step."""
        svc = rule_service
        # Sequence: code runs (plain output), auto-advances to review,
        # review outputs REVIEW_FAIL → routes back to code (3rd call)
        outputs = [
            AgentResult(success=True, stdout="Code done", stderr="", return_code=0, duration_ms=100),
            AgentResult(
                success=True, stdout="Issues found\nREVIEW_FAIL: fix the bug", stderr="", return_code=0, duration_ms=100
            ),
            AgentResult(success=True, stdout="Code fixed\nREVIEW_PASS", stderr="", return_code=0, duration_ms=100),
        ]
        runner = SequentialFakeRunner(outputs)
        svc.runner = runner
        run(svc.create_task("Route test"))
        run(asyncio.sleep(1.0))

        # code → review (REVIEW_FAIL) → code → review (REVIEW_PASS) → complete
        assert len(runner.calls) >= 3
        tasks = run(svc.db.list_tasks())
        assert tasks[0].state == "complete"

    def test_review_pass_advances(self, rule_service, run):
        """REVIEW_PASS output should advance past review to complete."""
        svc = rule_service
        # Sequence: code runs (plain output), auto-advances to review,
        # review outputs REVIEW_PASS → auto-advances to complete
        outputs = [
            AgentResult(success=True, stdout="Code done", stderr="", return_code=0, duration_ms=100),
            AgentResult(success=True, stdout="All good\nREVIEW_PASS", stderr="", return_code=0, duration_ms=100),
        ]
        runner = SequentialFakeRunner(outputs)
        svc.runner = runner
        run(svc.create_task("Pass test"))
        run(asyncio.sleep(0.5))

        # code → review (REVIEW_PASS) → complete
        assert len(runner.calls) == 2
        tasks = run(svc.db.list_tasks())
        assert tasks[0].state == "complete"


class TestStageTransitionMessage:
    """Test that approve() emits a stage_transition message."""

    def test_approve_emits_stage_transition(self, service, run):
        """Approving a waiting task should emit a stage_transition message."""
        run(service.create_task("Transition test"))
        run(asyncio.sleep(0.2))

        tasks = run(service.list_tasks_async())
        task_id = tasks[0].id
        fresh = run(service.db.get_task(task_id))
        assert fresh is not None
        assert fresh.status == "waiting"

        run(service.approve(task_id))
        run(asyncio.sleep(0.1))

        messages = run(service.get_messages(task_id))
        transition_msgs = [m for m in messages if m.type == "stage_transition"]
        assert len(transition_msgs) >= 1

        msg = transition_msgs[0]
        assert msg.role == "system"
        assert "from_step" in msg.metadata
        assert "to_step" in msg.metadata


class TestJumpToStep:
    """Test that jump_to_step transitions task to target step."""

    @pytest.fixture()
    def multi_step_service(self, tmp_path, _loop, run):
        """Service with a three-step flow: spec → code → review."""
        flow_yaml = tmp_path / "multi_flow.yaml"
        flow_yaml.write_text(
            "name: multi-test\njobs:\n"
            "  - name: spec\n    evaluate: true\n"
            "  - name: code\n    evaluate: true\n"
            "  - name: review\n    evaluate: true\n"
        )
        # Create stub prompt files for the custom flow
        prompts_dir = tmp_path / "prompts"
        prompts_dir.mkdir()
        for name in ("spec-system", "spec-user", "code-system", "code-user", "review-system", "review-user"):
            (prompts_dir / f"{name}.md").write_text(f"# {name}\n{{title}}\n{{body}}")
        config = LotsaConfig(
            data_dir=tmp_path / "data",
            work_dir=tmp_path,
            flow="custom",
            flow_file=flow_yaml,
            prompts_dir=prompts_dir,
            model="sonnet",
            budget=5.0,
        )
        (tmp_path / "data").mkdir()
        db = TaskDB(tmp_path / "data" / "lotsa.db")
        run(db.initialize())

        svc = OrchestratorService(config, db)
        svc.runner = FakeRunner()
        run(svc.start())
        yield svc
        run(svc.shutdown())
        run(db.close())

    def test_jump_transitions_to_target_step(self, multi_step_service, run):
        """jump_to_step should move the task to the target step's queue state."""
        svc = multi_step_service
        task = run(svc.create_task("Jump test"))
        run(asyncio.sleep(0.2))

        # Task should be waiting at spec step (DB is source of truth)
        task_id = task.id
        fresh = run(svc.db.get_task(task_id))
        assert fresh is not None
        assert fresh.status == "waiting"
        assert fresh.current_step == "spec"

        # Jump to the review step
        run(svc.jump_to_step(task_id, "review"))
        run(asyncio.sleep(0.2))

        # Task should now be waiting at the review step
        fresh = run(svc.db.get_task(task_id))
        assert fresh is not None
        assert fresh.status == "waiting"
        assert fresh.current_step == "review"

    def test_jump_emits_stage_transition(self, multi_step_service, run):
        """jump_to_step should emit a stage_transition message."""
        svc = multi_step_service
        task = run(svc.create_task("Transition test"))
        run(asyncio.sleep(0.2))

        task_id = task.id
        run(svc.jump_to_step(task_id, "review"))
        run(asyncio.sleep(0.2))

        messages = run(svc.get_messages(task_id))
        transition_msgs = [m for m in messages if m.type == "stage_transition"]
        assert len(transition_msgs) >= 1

        msg = transition_msgs[-1]
        assert msg.metadata["from_step"] == "spec"
        assert msg.metadata["to_step"] == "review"
        assert msg.metadata["direction"] == "forward"

    def test_jump_backward(self, multi_step_service, run):
        """jump_to_step backward should have direction='backward'."""
        svc = multi_step_service
        task = run(svc.create_task("Backward test"))
        run(asyncio.sleep(0.2))

        task_id = task.id
        # Jump forward to review first
        run(svc.jump_to_step(task_id, "review"))
        run(asyncio.sleep(0.2))

        # Now jump backward to spec
        run(svc.jump_to_step(task_id, "spec"))
        run(asyncio.sleep(0.2))

        messages = run(svc.get_messages(task_id))
        transition_msgs = [m for m in messages if m.type == "stage_transition"]
        backward_msg = transition_msgs[-1]
        assert backward_msg.metadata["direction"] == "backward"
        assert backward_msg.metadata["from_step"] == "review"
        assert backward_msg.metadata["to_step"] == "spec"

    def test_jump_cancels_in_flight(self, multi_step_service, run):
        """jump_to_step should cancel any in-flight agent."""
        svc = multi_step_service

        # Use a runner that blocks forever so the task stays in-flight
        async def _block(**kwargs):
            await asyncio.sleep(9999)
            return AgentResult(success=True, stdout="", stderr="", return_code=0, duration_ms=0)

        svc.runner.run = _block

        task = run(svc.create_task("Cancel test"))
        run(asyncio.sleep(0.1))

        task_id = task.id
        assert task_id in svc._in_flight

        run(svc.jump_to_step(task_id, "review"))
        run(asyncio.sleep(0.1))

        # The old in-flight entry (for spec) should have been popped and cancelled.
        # A new dispatch may or may not succeed (depends on artifact requirements),
        # but the original spec in-flight must be gone.
        old_in_flight = svc._in_flight.get(task_id)
        if old_in_flight is not None:
            # If still in-flight, it should be for a different step (the jump target)
            assert old_in_flight.step.name != "spec"

    def test_jump_unknown_step_raises(self, multi_step_service, run):
        """jump_to_step with an unknown step name should raise ValueError."""
        svc = multi_step_service
        task = run(svc.create_task("Bad jump"))
        run(asyncio.sleep(0.2))

        with pytest.raises(ValueError, match="Unknown step"):
            run(svc.jump_to_step(task.id, "nonexistent"))


@pytest.mark.skip(
    reason="ADR-014 Layer A — ``target: previous`` and the ``previous_step`` "
    "metadata it tracked were removed. The autonomous code↔review loop is now "
    "spelled by name in the main flow's per-binding REVIEW_FAIL → code override."
)
class TestPreviousStepTracking:
    """Test that previous_step is stored in metadata after step completion."""

    @pytest.fixture()
    def two_step_service(self, tmp_path, _loop, run):
        """Service with a two-step flow: code → review (with previous target)."""
        flow_yaml = tmp_path / "prev_flow.yaml"
        flow_yaml.write_text(
            "name: prev-test\njobs:\n"
            "  - name: code\n    prompt: coding\n"
            "    queue_state: backlog\n    active_state: coding\n"
            "  - name: review\n    prompt: review\n"
            "    queue_state: reviewing\n    active_state: reviewing\n"
            "    rules:\n"
            "      - source: stdout\n        pattern: '^REVIEW_PASS'\n        target: next\n"
            "      - source: stdout\n        pattern: '^REVIEW_FAIL'\n        target: previous\n"
        )
        prompts_dir = tmp_path / "prompts"
        prompts_dir.mkdir()
        for name in ("coding-system", "coding-user", "review-system", "review-user"):
            (prompts_dir / f"{name}.md").write_text(f"# {name}\n{{title}}\n{{body}}")
        config = LotsaConfig(
            data_dir=tmp_path / "data",
            work_dir=tmp_path,
            flow="custom",
            flow_file=flow_yaml,
            prompts_dir=prompts_dir,
            model="sonnet",
            budget=5.0,
        )
        (tmp_path / "data").mkdir()
        db = TaskDB(tmp_path / "data" / "lotsa.db")
        run(db.initialize())

        svc = OrchestratorService(config, db)
        svc.runner = FakeRunner()
        run(svc.start())
        yield svc
        run(svc.shutdown())
        run(db.close())

    def test_previous_step_stored_in_metadata(self, two_step_service, run):
        """After code step completes, previous_step='code' is stored in metadata."""
        svc = two_step_service
        # code runs (plain output), auto-advances to review,
        # review outputs REVIEW_FAIL → should route back to code via "previous"
        outputs = [
            AgentResult(success=True, stdout="Code done", stderr="", return_code=0, duration_ms=100),
            AgentResult(success=True, stdout="Issues\nREVIEW_FAIL: fix it", stderr="", return_code=0, duration_ms=100),
            AgentResult(success=True, stdout="Fixed\nREVIEW_PASS", stderr="", return_code=0, duration_ms=100),
        ]
        runner = SequentialFakeRunner(outputs)
        svc.runner = runner
        run(svc.create_task("Previous step test"))
        run(asyncio.sleep(1.0))

        # Should have gone: code → review (REVIEW_FAIL) → code → review (REVIEW_PASS) → complete
        assert len(runner.calls) >= 3
        tasks = run(svc.db.list_tasks())
        assert tasks[0].state == "complete"

        # Check that previous_step was stored in metadata
        task = run(svc.db.get_task(tasks[0].id))
        assert task.metadata.get("previous_step") == "code"

    def test_review_step_not_tracked_as_previous(self, two_step_service, run):
        """The review step itself should not be stored as previous_step."""
        svc = two_step_service
        outputs = [
            AgentResult(success=True, stdout="Code done", stderr="", return_code=0, duration_ms=100),
            AgentResult(success=True, stdout="All good\nREVIEW_PASS", stderr="", return_code=0, duration_ms=100),
        ]
        runner = SequentialFakeRunner(outputs)
        svc.runner = runner
        run(svc.create_task("No review tracking"))
        run(asyncio.sleep(0.5))

        tasks = run(svc.db.list_tasks())
        task = run(svc.db.get_task(tasks[0].id))
        # previous_step should be "code", not "review"
        assert task.metadata.get("previous_step") == "code"


class TestAgentModelMetadata:
    """Test that agent messages include agent_model in metadata."""

    def test_output_message_includes_agent_model(self, service, run):
        """Non-conversational agent output messages should include agent_model."""
        service.runner = FakeRunner(
            AgentResult(
                success=True,
                stdout="Agent output",
                stderr="",
                return_code=0,
                duration_ms=500,
                session_id="sess-1",
            )
        )
        run(service.create_task("Model metadata test"))
        run(asyncio.sleep(0.3))

        messages = run(service.db.get_messages(run(service.list_tasks_async())[0].id, msg_type="output"))
        assert len(messages) >= 1
        assert messages[-1].metadata.get("agent_model") == "sonnet"


@pytest.mark.skip(
    reason="ADR-014 Layer A — rewrites pending. The synthetic 'pushing' / "
    "'waiting_for_pr' / 'rebasing' states this class targets were removed; "
    "the equivalent behavior now flows through the push_pr action tool and "
    "the wait_for_pr_signal monitor engine. See lotsa/tests/test_push_pr_tool.py "
    "and lotsa/tests/test_pr_monitor_engine.py for the new contract."
)
class TestPrPhaseStates:
    """Test that PR-phase states are wired into the state machine and orchestrator."""

    def test_pushing_state_exists_with_pr_config(self):
        """Flow with pr: config should have pushing/waiting_for_pr/abandoned states."""
        from lotsa.flows import build_flow

        # The full flow has pr: config
        flow = build_flow("build")
        sm_states = set(flow.state_machine._states)
        assert "pushing" in sm_states
        assert "waiting_for_pr" in sm_states
        assert "abandoned" in sm_states
        assert "rebasing" in sm_states

    def test_pushing_state_absent_without_pr_config(self):
        """Flow without pr: config should NOT have pushing state."""
        from lotsa.flows import build_flow

        flow = build_flow("build")
        sm_states = set(flow.state_machine._states)
        assert "pushing" not in sm_states
        assert "waiting_for_pr" not in sm_states

    def test_last_job_success_state_redirected_to_pushing(self):
        """With pr_config, the last job's success_state should be 'pushing'."""
        from lotsa.flows import build_flow

        flow = build_flow("build")
        last_job = flow.jobs[-1]
        assert last_job.success_state == "pushing"

    def test_waiting_for_pr_to_complete_transition(self):
        """State machine should allow waiting_for_pr → complete."""
        from lotsa.flows import build_flow

        flow = build_flow("build")
        assert ("waiting_for_pr", "complete") in flow.state_machine._transitions

    def test_waiting_for_pr_to_abandoned_transition(self):
        """State machine should allow waiting_for_pr → abandoned."""
        from lotsa.flows import build_flow

        flow = build_flow("build")
        assert ("waiting_for_pr", "abandoned") in flow.state_machine._transitions

    def test_waiting_for_pr_to_pr_fix_transition(self):
        """State machine should allow waiting_for_pr → pr-fixing."""
        from lotsa.flows import build_flow

        flow = build_flow("build")
        assert ("waiting_for_pr", "pr-fixing") in flow.state_machine._transitions

    def test_pushing_to_rebasing_transition(self):
        """State machine should allow pushing → rebasing."""
        from lotsa.flows import build_flow

        flow = build_flow("build")
        assert ("pushing", "rebasing") in flow.state_machine._transitions

    @pytest.fixture()
    def pr_service(self, tmp_path, _loop, run):
        """Service with a flow that includes pr: config."""
        flow_yaml = tmp_path / "pr_flow.yaml"
        flow_yaml.write_text(
            "name: pr-test\njobs:\n"
            "  - name: coding\n    evaluate: true\n"
            "  - name: pr-fix\n    target: previous\n"
            "pr: {}\n"
        )
        prompts_dir = tmp_path / "prompts"
        prompts_dir.mkdir()
        for name in ("coding-system", "coding-user", "pr-fix-system", "pr-fix-user"):
            (prompts_dir / f"{name}.md").write_text(f"# {name}\n{{title}}\n{{body}}")
        config = LotsaConfig(
            data_dir=tmp_path / "data",
            work_dir=tmp_path,
            flow="custom",
            flow_file=flow_yaml,
            prompts_dir=prompts_dir,
            model="sonnet",
            budget=5.0,
        )
        (tmp_path / "data").mkdir()
        db = TaskDB(tmp_path / "data" / "lotsa.db")
        run(db.initialize())
        svc = OrchestratorService(config, db)
        svc.runner = FakeRunner()
        run(svc.start())
        yield svc
        run(svc.shutdown())
        run(db.close())

    def test_transition_task_to_complete(self, pr_service, run):
        """transition_task should move from waiting_for_pr to complete."""
        svc = pr_service
        # Create a task and manually set it to waiting_for_pr
        task = run(svc.db.create_task("PR test", state="waiting_for_pr", metadata={"pr_number": 42}))
        run(svc.transition_task(task.id, "complete"))

        updated = run(svc.db.get_task(task.id))
        assert updated.state == "complete"

    def test_transition_task_to_abandoned(self, pr_service, run):
        """transition_task should move from waiting_for_pr to abandoned."""
        svc = pr_service
        task = run(svc.db.create_task("PR test", state="waiting_for_pr", metadata={"pr_number": 42}))
        run(svc.transition_task(task.id, "abandoned"))

        updated = run(svc.db.get_task(task.id))
        assert updated.state == "abandoned"

    def test_list_waiting_pr_tasks(self, pr_service, run):
        """list_waiting_pr_tasks should return tasks in waiting_for_pr state."""
        svc = pr_service
        run(
            svc.db.create_task(
                "PR waiting", state="waiting_for_pr", status="waiting_for_pr", metadata={"pr_number": 10}
            )
        )
        run(svc.db.create_task("Not waiting", state="coding"))

        tasks = run(svc.list_waiting_pr_tasks())
        assert len(tasks) == 1
        assert tasks[0]["metadata"]["pr_number"] == 10

    def test_metadata_in_task_summary(self, pr_service, run):
        """TaskSummary should include metadata for template rendering."""
        svc = pr_service
        run(svc.db.create_task("PR metadata", state="waiting_for_pr", metadata={"pr_number": 99}))
        summaries = run(svc.list_tasks_async())
        pr_tasks = [s for s in summaries if s.state == "waiting_for_pr"]
        assert len(pr_tasks) == 1
        assert pr_tasks[0].metadata.get("pr_number") == 99

    def test_dispatch_pr_fix_nonexistent_task(self, pr_service, run):
        """dispatch_pr_fix with invalid task_id returns without error."""
        svc = pr_service
        run(svc.dispatch_pr_fix("nonexistent", "feedback"))  # should not raise

    def test_execute_push_success(self, pr_service, run):
        """Successful push persists metadata and transitions to waiting_for_pr."""
        from unittest.mock import AsyncMock, patch

        svc = pr_service
        task = run(svc.db.create_task("Push test", state="pushing", metadata={}))

        from rigg.models import Item

        item = Item(id=task.id, state="pushing", title="Push test")

        mock_push = AsyncMock(return_value=(42, "https://github.com/acme/repo/pull/42", "acme", "repo"))
        with patch("lotsa.push_step.execute_push", mock_push):
            run(svc._execute_push(item))

        updated = run(svc.db.get_task(task.id))
        assert updated.metadata["pr_number"] == 42
        assert updated.metadata["github_owner"] == "acme"
        assert item.state == "waiting_for_pr"

    def test_execute_push_failure_blocks_task(self, pr_service, run):
        """Generic push failure transitions to blocked without metadata."""
        from unittest.mock import AsyncMock, patch

        svc = pr_service
        task = run(svc.db.create_task("Push fail test", state="pushing", metadata={}))

        from rigg.models import Item

        item = Item(id=task.id, state="pushing", title="Push fail test")

        mock_push = AsyncMock(side_effect=Exception("Network error"))
        with patch("lotsa.push_step.execute_push", mock_push):
            run(svc._execute_push(item))

        updated = run(svc.db.get_task(task.id))
        assert "pr_number" not in updated.metadata
        assert item.state == "blocked"

    def test_execute_push_non_fast_forward_transitions_to_rebasing(self, pr_service, run):
        """NON_FAST_FORWARD PushError transitions to rebasing, not blocked."""
        from unittest.mock import AsyncMock, patch

        from lotsa.push_step import PushError

        svc = pr_service
        task = run(svc.db.create_task("Rebase test", state="pushing", metadata={}))

        from rigg.models import Item

        item = Item(id=task.id, state="pushing", title="Rebase test")

        mock_push = AsyncMock(side_effect=PushError("NON_FAST_FORWARD: branch rejected"))
        with patch("lotsa.push_step.execute_push", mock_push):
            run(svc._execute_push(item))

        assert item.state == "rebasing"

    def test_dispatch_pr_fix_wrong_state_returns_early(self, pr_service, run):
        """dispatch_pr_fix should not dispatch when task is not in waiting_for_pr state."""
        svc = pr_service
        task = run(svc.db.create_task("Wrong state", state="complete", metadata={"pr_number": 1}))

        run(svc.dispatch_pr_fix(task.id, "Fix something"))
        # Task state should be unchanged — dispatch was a no-op
        updated = run(svc.db.get_task(task.id))
        assert updated.state == "complete"

    @pytest.fixture()
    def pr_fix_service(self, tmp_path, _loop, run):
        """Service with a flow that includes pr: config AND a pr-fix step."""
        flow_yaml = tmp_path / "pr_fix_flow.yaml"
        flow_yaml.write_text(
            "name: pr-fix-test\n"
            "jobs:\n"
            "  - name: coding\n"
            "    evaluate: true\n"
            "  - name: pr-fix\n"
            "    target: previous\n"
            "pr: {}\n"
        )
        prompts_dir = tmp_path / "prompts"
        prompts_dir.mkdir()
        for name in ("coding-system", "coding-user", "pr-fix-system", "pr-fix-user"):
            (prompts_dir / f"{name}.md").write_text(f"# {name}\n{{title}}\n{{body}}")
        config = LotsaConfig(
            data_dir=tmp_path / "data",
            work_dir=tmp_path,
            flow="custom",
            flow_file=flow_yaml,
            prompts_dir=prompts_dir,
            model="sonnet",
            budget=5.0,
        )
        (tmp_path / "data").mkdir()
        db = TaskDB(tmp_path / "data" / "lotsa.db")
        run(db.initialize())
        svc = OrchestratorService(config, db)
        svc.runner = FakeRunner()
        run(svc.start())
        yield svc
        run(svc.shutdown())
        run(db.close())

    def test_dispatch_pr_fix_success(self, pr_fix_service, run):
        """dispatch_pr_fix with a pr-fix step transitions task to pr-fixing."""
        svc = pr_fix_service
        task = run(
            svc.db.create_task(
                "PR fix success",
                state="waiting_for_pr",
                status="waiting_for_pr",
                metadata={"pr_number": 1},
            )
        )

        run(svc.dispatch_pr_fix(task.id, "Fix the lint errors"))

        # Wait for the agent to complete
        import asyncio

        for _ in range(40):
            if task.id not in svc._in_flight:
                break
            run(asyncio.sleep(0.05))

        updated = run(svc.db.get_task(task.id))
        # Task should have left waiting_for_pr (dispatched to pr-fix step)
        assert updated.state != "waiting_for_pr"

    def test_revise_from_rebasing_dispatches_pr_fix(self, pr_fix_service, run):
        """revise() on a rebasing task should transition to waiting_for_pr and dispatch pr-fix."""
        import asyncio

        svc = pr_fix_service
        task = run(
            svc.db.create_task(
                "Rebase recovery",
                state="rebasing",
                metadata={"pr_number": 7, "github_owner": "acme", "github_repo": "x"},
            )
        )

        run(svc.revise(task.id, "rebase on main"))
        for _ in range(40):
            if task.id not in svc._in_flight:
                break
            run(asyncio.sleep(0.05))

        updated = run(svc.db.get_task(task.id))
        # Task should have left rebasing (dispatched into pr-fix cycle)
        assert updated.state != "rebasing"

    def test_revise_from_waiting_for_pr_dispatches_pr_fix(self, pr_fix_service, run):
        """revise() on a waiting_for_pr task dispatches pr-fix with the user's feedback.

        With ``_pr_monitor=None``, ``_build_revise_feedback`` returns the user's
        message directly (no GitHub fetch).  The pr-fix step should then be
        dispatched and the task should leave waiting_for_pr.
        """
        import asyncio

        svc = pr_fix_service
        # Force the monitor to be unset so _build_revise_feedback short-circuits.
        svc._pr_monitor = None
        task = run(
            svc.db.create_task(
                "Revise happy path",
                state="waiting_for_pr",
                status="waiting_for_pr",
                metadata={"pr_number": 11},
            )
        )

        run(svc.revise(task.id, "please address the lint warnings"))
        for _ in range(40):
            if task.id not in svc._in_flight:
                break
            run(asyncio.sleep(0.05))

        updated = run(svc.db.get_task(task.id))
        # Task transitioned out of waiting_for_pr — pr-fix was dispatched.
        assert updated.state != "waiting_for_pr"

        # User feedback was recorded as a chat message.
        messages = run(svc.db.get_messages(task.id, msg_type="feedback"))
        assert any("please address the lint warnings" in m.content for m in messages)

    def test_revise_in_waiting_for_pr_holds_dispatch_lock(self, pr_fix_service, run):
        """revise() on waiting_for_pr must hold the dispatch lock across the GitHub fetch.

        Regression: a concurrent monitor poll could dispatch its own pr-fix
        between revise()'s state check and its dispatch_pr_fix call, dropping
        the user's revise feedback.  The fix acquires _dispatching_pr_fix
        before _build_revise_feedback so a concurrent monitor dispatch sees
        the guard and short-circuits.
        """
        import asyncio
        from unittest.mock import patch

        svc = pr_fix_service
        task = run(
            svc.db.create_task(
                "Revise race",
                state="waiting_for_pr",
                status="waiting_for_pr",
                metadata={"pr_number": 1},
            )
        )

        # Stub _build_revise_feedback to simulate a slow GitHub fetch and
        # verify the lock is held while we're inside it.
        lock_held_during_fetch: dict[str, bool] = {}

        async def slow_build(task_arg, feedback):
            lock_held_during_fetch["held"] = task.id in svc._dispatching_pr_fix
            return feedback

        with patch.object(svc, "_build_revise_feedback", side_effect=slow_build):
            run(svc.revise(task.id, "fix the lint"))
            for _ in range(40):
                if task.id not in svc._in_flight:
                    break
                run(asyncio.sleep(0.05))

        assert lock_held_during_fetch.get("held") is True, (
            "revise() must hold _dispatching_pr_fix while gathering feedback"
        )

    def test_retry_from_push_failure_routes_to_pushing(self, pr_service, run):
        """A task blocked after a push failure should retry into 'pushing', not 'speccing'."""
        import asyncio
        import json

        svc = pr_service
        # Simulate the orchestrator's startup recovery: push failed, task is
        # blocked, and the dispatch event for "push" was emitted.
        task = run(
            svc.db.create_task(
                "Retry push", state="blocked", status="blocked", current_step="push", metadata={"pr_number": 1}
            )
        )
        # Write the dispatch event directly (append_event needs a running
        # loop and the test helper here is sync-style).
        run(
            svc.db.add_message(
                task.id,
                "system",
                "",
                json.dumps({"type": "dispatch", "job_type": "push", "success": False}),
                "status_change",
            )
        )

        # Patch _execute_push so retry doesn't actually try to push to GitHub
        from unittest.mock import AsyncMock, patch

        called: dict[str, object] = {}

        async def fake_execute_push(item):
            called["called"] = True
            called["state"] = item.state

        with patch.object(svc, "_execute_push", new=AsyncMock(side_effect=fake_execute_push)):
            run(svc.retry(task.id))
            for _ in range(20):
                if called.get("called"):
                    break
                run(asyncio.sleep(0.05))

        assert called.get("called"), "retry should have invoked _execute_push"
        assert called["state"] == "pushing"

    # ------------------------------------------------------------------
    # Phase 1 — R4: PR_FIX_SKIPPED: returns to waiting_for_pr (no push)
    # ------------------------------------------------------------------

    @pytest.fixture()
    def pr_fix_skipped_service(self, tmp_path, _loop, run):
        """Service whose pr-fix step has the PR_FIX_SKIPPED: rule wired.

        Mirrors ``pr_fix_service`` but adds the rule under test. The
        existing ``pr_fix_service`` fixture's flow YAML omits rules
        entirely (it predates Phase 1), so reusing it would never match
        ``PR_FIX_SKIPPED:`` regardless of agent output.
        """
        flow_yaml = tmp_path / "pr_fix_skipped_flow.yaml"
        flow_yaml.write_text(
            "name: pr-fix-skipped-test\n"
            "jobs:\n"
            "  - name: coding\n"
            "    evaluate: true\n"
            "  - name: pr-fix\n"
            "    rules:\n"
            "      - source: stdout\n"
            '        pattern: "^PR_FIX_DONE:"\n'
            "        target: coding\n"
            # BLOCKED before SKIPPED — matches production flow.yaml ordering
            # and the prompt's Phase 1 precedence DONE > BLOCKED > SKIPPED.
            "      - source: stdout\n"
            '        pattern: "^PR_FIX_BLOCKED:"\n'
            "        target: blocked\n"
            "      - source: stdout\n"
            '        pattern: "^PR_FIX_SKIPPED:"\n'
            "        target: waiting_for_pr\n"
            "pr: {}\n"
        )
        prompts_dir = tmp_path / "prompts"
        prompts_dir.mkdir()
        for name in ("coding-system", "coding-user", "pr-fix-system", "pr-fix-user"):
            (prompts_dir / f"{name}.md").write_text(f"# {name}\n{{title}}\n{{body}}")
        config = LotsaConfig(
            data_dir=tmp_path / "data",
            work_dir=tmp_path,
            flow="custom",
            flow_file=flow_yaml,
            prompts_dir=prompts_dir,
            model="sonnet",
            budget=5.0,
        )
        (tmp_path / "data").mkdir()
        db = TaskDB(tmp_path / "data" / "lotsa.db")
        run(db.initialize())
        svc = OrchestratorService(config, db)
        svc.runner = FakeRunner()
        run(svc.start())
        yield svc
        run(svc.shutdown())
        run(db.close())

    def test_pr_fix_skipped_returns_to_waiting_for_pr(self, pr_fix_skipped_service, run):
        """PR_FIX_SKIPPED: agent output flips the task back to waiting_for_pr.

        Behavioural invariants enforced by the drainer's special-case:
        - state and status both land on waiting_for_pr
        - current_step is cleared (None) — there is no active step
        - _execute_push is NEVER invoked (no commit, no push)
        - the worktree is unchanged (no agent file writes either)

        The agent's "no-op exit hatch" must not produce any push artifact.
        """
        import asyncio
        from unittest.mock import AsyncMock, patch

        svc = pr_fix_skipped_service
        # Configure the runner to emit PR_FIX_SKIPPED: from the pr-fix step.
        svc.runner = FakeRunner(
            AgentResult(
                success=True,
                stdout="Triaged feedback.\nPR_FIX_SKIPPED: reviewer approved, no actionable items\n",
                stderr="",
                return_code=0,
                duration_ms=200,
                session_id="sess-skip",
            )
        )

        task = run(
            svc.db.create_task(
                "Skip case",
                state="waiting_for_pr",
                status="waiting_for_pr",
                metadata={"pr_number": 1, "github_owner": "o", "github_repo": "r"},
            )
        )

        # Patch _execute_push so the test would *fail loudly* if the SKIPPED
        # path ever calls it. The push step is irrelevant to the skip
        # outcome and must never be invoked.
        with patch.object(svc, "_execute_push", new=AsyncMock()) as mock_push:
            run(svc.dispatch_pr_fix(task.id, "Bot left an approval comment"))
            for _ in range(40):
                if task.id not in svc._in_flight:
                    break
                run(asyncio.sleep(0.05))
            # Give the drainer one more tick to land the SKIPPED transition.
            run(asyncio.sleep(0.1))

            mock_push.assert_not_called()

        updated = run(svc.db.get_task(task.id))
        assert updated is not None
        assert updated.state == "waiting_for_pr", (
            f"SKIPPED must return task to waiting_for_pr, got state={updated.state!r}"
        )
        assert updated.status == "waiting_for_pr", f"status must mirror state on SKIPPED, got status={updated.status!r}"
        assert updated.current_step is None, f"current_step must be cleared on SKIPPED, got {updated.current_step!r}"

    def test_pr_fix_skipped_advances_comments_since(self, pr_fix_skipped_service, run):
        """PR_FIX_SKIPPED: advances pr_comments_since to this round's cutoff.

        Otherwise the monitor's next poll would re-deliver the very feedback
        the agent just declined to act on, producing an immediate redispatch
        loop.  The cutoff is the ``pr_fix_dispatched_at`` value recorded by
        ``_dispatch_pr_fix_locked`` (which is the round's fetch_updated_at
        cursor — i.e. the timestamp captured *before* the agent ran).
        """
        import asyncio
        from unittest.mock import AsyncMock, patch

        svc = pr_fix_skipped_service
        svc.runner = FakeRunner(
            AgentResult(
                success=True,
                stdout="Nothing actionable here.\nPR_FIX_SKIPPED: bot chatter only\n",
                stderr="",
                return_code=0,
                duration_ms=200,
                session_id="sess-skip-2",
            )
        )

        STALE_CURSOR = "2020-01-01T00:00:00+00:00"

        task = run(
            svc.db.create_task(
                "Skip cursor",
                state="waiting_for_pr",
                status="waiting_for_pr",
                metadata={
                    "pr_number": 1,
                    "github_owner": "o",
                    "github_repo": "r",
                    # Old cursor that the SKIPPED handler must overwrite.
                    "pr_comments_since": STALE_CURSOR,
                },
            )
        )

        with patch.object(svc, "_execute_push", new=AsyncMock()):
            run(svc.dispatch_pr_fix(task.id, "stub feedback"))
            for _ in range(40):
                if task.id not in svc._in_flight:
                    break
                run(asyncio.sleep(0.05))
            run(asyncio.sleep(0.1))

        updated = run(svc.db.get_task(task.id))
        assert updated is not None

        # The dispatch path recorded pr_fix_dispatched_at; the SKIPPED branch
        # advances pr_comments_since to match.  Both keys must be present and
        # equal, and neither must equal the stale pre-dispatch value.
        dispatched_at = updated.metadata.get("pr_fix_dispatched_at")
        since = updated.metadata.get("pr_comments_since")
        assert dispatched_at is not None, "_dispatch_pr_fix_locked must record pr_fix_dispatched_at"
        assert since is not None, "PR_FIX_SKIPPED: must persist pr_comments_since"
        assert since == dispatched_at, (
            f"pr_comments_since must equal pr_fix_dispatched_at ({dispatched_at!r}), got {since!r}"
        )
        assert since != STALE_CURSOR, "pr_comments_since must advance past the stale pre-dispatch value"


@pytest.mark.skip(
    reason="ADR-014 Layer A — rewrites pending. Phase-2 pr-fix bookkeeping "
    "(round caps, audit rows, six dispatch entry points) is preserved by the "
    "implementation but the fixture / state-name assumptions of this class "
    "match the pre-refactor synthetic-state model. Replacement tests should "
    "drive sub-flow dispatch via the new dispatch_sub_flow contract."
)
class TestPrFixPhase2:
    """Phase 2: round counter + budget caps, pr_decision audit writes,
    PR_FIX_NEEDS_DECISION → needs_input escalation.

    Spec: docs/superpowers/specs/2026-05-12-autonomous-pr-fix-loop-design.md
    Plan: Tasks 5, 6, 7 of the autonomous PR-fix loop.

    The fixture wires the full Phase 2 ruleset on the pr-fix step:
    DONE → review-equivalent (we use ``coding`` as a stand-in target since
    this flow has no review step), NEEDS_DECISION → needs_input, BLOCKED →
    blocked, SKIPPED → waiting_for_pr. Rule order matches the production
    flow.yaml precedence (DONE > NEEDS_DECISION > BLOCKED > SKIPPED).
    """

    @pytest.fixture()
    def pr_fix_phase2_service(self, tmp_path, _loop, run):
        """Service with the full Phase 2 four-rule wiring on pr-fix."""
        flow_yaml = tmp_path / "pr_fix_phase2_flow.yaml"
        flow_yaml.write_text(
            "name: pr-fix-phase2-test\n"
            "jobs:\n"
            "  - name: coding\n"
            "    evaluate: true\n"
            "  - name: pr-fix\n"
            "    rules:\n"
            "      - source: stdout\n"
            '        pattern: "^PR_FIX_DONE:"\n'
            "        target: coding\n"
            "      - source: stdout\n"
            '        pattern: "^PR_FIX_NEEDS_DECISION:"\n'
            "        target: needs_input\n"
            "      - source: stdout\n"
            '        pattern: "^PR_FIX_BLOCKED:"\n'
            "        target: blocked\n"
            "      - source: stdout\n"
            '        pattern: "^PR_FIX_SKIPPED:"\n'
            "        target: waiting_for_pr\n"
            "pr:\n"
            "  max_pr_fix_rounds: 0\n"  # caps disabled by default; specific tests override
            "  max_consecutive_skipped: 0\n"
        )
        prompts_dir = tmp_path / "prompts"
        prompts_dir.mkdir()
        for name in ("coding-system", "coding-user", "pr-fix-system", "pr-fix-user"):
            (prompts_dir / f"{name}.md").write_text(f"# {name}\n{{title}}\n{{body}}")
        config = LotsaConfig(
            data_dir=tmp_path / "data",
            work_dir=tmp_path,
            flow="custom",
            flow_file=flow_yaml,
            prompts_dir=prompts_dir,
            model="sonnet",
            budget=5.0,
        )
        (tmp_path / "data").mkdir()
        db = TaskDB(tmp_path / "data" / "lotsa.db")
        run(db.initialize())
        svc = OrchestratorService(config, db)
        svc.runner = FakeRunner()
        run(svc.start())
        yield svc
        run(svc.shutdown())
        run(db.close())

    @staticmethod
    def _make_waiting_task(svc, run, **metadata):
        defaults = {"pr_number": 1, "github_owner": "o", "github_repo": "r"}
        defaults.update(metadata)
        return run(
            svc.db.create_task(
                "Phase 2 task",
                state="waiting_for_pr",
                status="waiting_for_pr",
                metadata=defaults,
            )
        )

    @staticmethod
    def _drain(run, svc, task_id):
        """Wait for the in-flight agent to finish and the drainer to land its CAS."""
        for _ in range(40):
            if task_id not in svc._in_flight:
                break
            run(asyncio.sleep(0.05))
        run(asyncio.sleep(0.1))

    # ------------------------------------------------------------------
    # Task 5 — round counter increments
    # ------------------------------------------------------------------

    def test_pr_fix_round_count_increments_on_dispatch(self, pr_fix_phase2_service, run):
        """Each successful dispatch increments ``pr_fix_round_count``.

        Counter starts at 0, lands at 1 after first dispatch, 2 after the
        second — independent of outcome marker.
        """
        from unittest.mock import AsyncMock, patch

        svc = pr_fix_phase2_service
        svc.runner = FakeRunner(
            AgentResult(
                success=True,
                stdout="PR_FIX_SKIPPED: nothing actionable\n",
                stderr="",
                return_code=0,
                duration_ms=100,
                session_id="r1",
            )
        )
        task = self._make_waiting_task(svc, run)

        with patch.object(svc, "_execute_push", new=AsyncMock()):
            run(svc.dispatch_pr_fix(task.id, "feedback round 1"))
            self._drain(run, svc, task.id)
            updated = run(svc.db.get_task(task.id))
            assert updated.metadata.get("pr_fix_round_count") == 1, (
                f"first dispatch must set pr_fix_round_count=1, got {updated.metadata.get('pr_fix_round_count')!r}"
            )

            # Second dispatch — task is back in waiting_for_pr after the skip.
            run(svc.dispatch_pr_fix(task.id, "feedback round 2"))
            self._drain(run, svc, task.id)
            updated = run(svc.db.get_task(task.id))
            assert updated.metadata.get("pr_fix_round_count") == 2, (
                f"second dispatch must increment to 2, got {updated.metadata.get('pr_fix_round_count')!r}"
            )

    def test_pr_fix_round_count_cap_blocks_dispatch(self, pr_fix_phase2_service, run):
        """When ``pr_fix_round_count`` is at the cap, dispatch is refused.

        Pre-check fires BEFORE the dispatching CAS: no agent run happens,
        no counter increment, and the task lands in ``blocked``.
        """
        from unittest.mock import AsyncMock, patch

        svc = pr_fix_phase2_service
        # Override the fixture's cap-disabled config.
        svc.flow.pr_config.max_pr_fix_rounds = 3
        task = self._make_waiting_task(svc, run, pr_fix_round_count=3)

        agent_calls = {"n": 0}

        async def counting_run(*args, **kwargs):
            agent_calls["n"] += 1
            return AgentResult(success=True, stdout="PR_FIX_DONE: x\n", stderr="", return_code=0, duration_ms=10)

        svc.runner.run = counting_run  # type: ignore[assignment]

        with patch.object(svc, "_execute_push", new=AsyncMock()):
            run(svc.dispatch_pr_fix(task.id, "feedback at cap"))
            self._drain(run, svc, task.id)

        assert agent_calls["n"] == 0, "agent must not run when round-cap is hit"
        updated = run(svc.db.get_task(task.id))
        assert updated.state == "blocked", f"round-cap must transition to blocked, got state={updated.state!r}"
        assert updated.status == "blocked", f"round-cap must flip status to blocked, got status={updated.status!r}"
        # Counter remains at the value that triggered the cap (not incremented past it).
        assert updated.metadata.get("pr_fix_round_count") == 3, (
            f"counter must not increment past cap, got {updated.metadata.get('pr_fix_round_count')!r}"
        )

    def test_pr_fix_round_count_cap_writes_pr_decision(self, pr_fix_phase2_service, run):
        """Cap-fire path writes a ``pr_decision`` audit row with decision=blocked.

        Per Task 6, every pr-fix outcome — including the system-emitted
        cap-fire — produces a symmetric audit message. Round number reports
        the round that triggered the block (pre-increment), not the unused
        next round.
        """
        from unittest.mock import AsyncMock, patch

        svc = pr_fix_phase2_service
        svc.flow.pr_config.max_pr_fix_rounds = 5
        task = self._make_waiting_task(svc, run, pr_fix_round_count=5)

        with patch.object(svc, "_execute_push", new=AsyncMock()):
            run(svc.dispatch_pr_fix(task.id, "feedback at cap"))
            self._drain(run, svc, task.id)

        decisions = run(svc.db.get_messages(task.id, msg_type="pr_decision"))
        assert decisions, "cap-fire path must write a pr_decision row"
        cap_row = decisions[-1]
        assert cap_row.metadata.get("decision") == "blocked"
        assert cap_row.metadata.get("round") == 5, (
            f"cap-fire round must report the cap-firing round (5), got {cap_row.metadata.get('round')!r}"
        )
        assert cap_row.metadata.get("commit_sha") is None
        assert cap_row.metadata.get("duration_ms") is None, (
            "cap-fire has no agent run — duration_ms must be None per Phase 2 spec"
        )
        assert cap_row.metadata.get("cost_usd") is None, (
            "cap-fire has no agent run — cost_usd must be None per Phase 2 spec"
        )

    def test_max_pr_fix_rounds_zero_disables_cap(self, pr_fix_phase2_service, run):
        """``max_pr_fix_rounds=0`` lets the counter increment past any value."""
        from unittest.mock import AsyncMock, patch

        svc = pr_fix_phase2_service
        svc.flow.pr_config.max_pr_fix_rounds = 0  # disabled
        svc.runner = FakeRunner(
            AgentResult(
                success=True,
                stdout="PR_FIX_SKIPPED: bot chatter\n",
                stderr="",
                return_code=0,
                duration_ms=10,
                session_id="zd",
            )
        )
        task = self._make_waiting_task(svc, run, pr_fix_round_count=999)

        with patch.object(svc, "_execute_push", new=AsyncMock()):
            run(svc.dispatch_pr_fix(task.id, "feedback well past any sane cap"))
            self._drain(run, svc, task.id)

        updated = run(svc.db.get_task(task.id))
        assert updated.state == "waiting_for_pr", (
            f"with cap disabled, dispatch must proceed normally, got state={updated.state!r}"
        )
        assert updated.metadata.get("pr_fix_round_count") == 1000, (
            "counter must still increment even when cap is disabled"
        )

    # ------------------------------------------------------------------
    # Task 5 — consecutive-skip counter
    # ------------------------------------------------------------------

    def test_consecutive_skipped_increments_on_skip(self, pr_fix_phase2_service, run):
        """Each PR_FIX_SKIPPED outcome bumps ``pr_fix_consecutive_skipped``."""
        from unittest.mock import AsyncMock, patch

        svc = pr_fix_phase2_service
        svc.runner = FakeRunner(
            AgentResult(
                success=True,
                stdout="PR_FIX_SKIPPED: nothing actionable\n",
                stderr="",
                return_code=0,
                duration_ms=10,
                session_id="cs",
            )
        )
        task = self._make_waiting_task(svc, run)

        with patch.object(svc, "_execute_push", new=AsyncMock()):
            run(svc.dispatch_pr_fix(task.id, "feedback 1"))
            self._drain(run, svc, task.id)
            updated = run(svc.db.get_task(task.id))
            assert updated.metadata.get("pr_fix_consecutive_skipped") == 1

            run(svc.dispatch_pr_fix(task.id, "feedback 2"))
            self._drain(run, svc, task.id)
            updated = run(svc.db.get_task(task.id))
            assert updated.metadata.get("pr_fix_consecutive_skipped") == 2

    def test_consecutive_skipped_cap_transitions_to_blocked(self, pr_fix_phase2_service, run):
        """Nth consecutive skip with ``max_consecutive_skipped=N`` transitions to blocked.

        The cap fires AFTER the SKIPPED transition lands (post-CAS). The
        task moves from waiting_for_pr → blocked with a clear human-
        readable message identifying the cap.
        """
        from unittest.mock import AsyncMock, patch

        svc = pr_fix_phase2_service
        svc.flow.pr_config.max_consecutive_skipped = 2
        svc.runner = FakeRunner(
            AgentResult(
                success=True,
                stdout="PR_FIX_SKIPPED: not actionable\n",
                stderr="",
                return_code=0,
                duration_ms=10,
                session_id="csc",
            )
        )
        task = self._make_waiting_task(svc, run)

        with patch.object(svc, "_execute_push", new=AsyncMock()):
            run(svc.dispatch_pr_fix(task.id, "feedback 1"))
            self._drain(run, svc, task.id)
            updated = run(svc.db.get_task(task.id))
            assert updated.state == "waiting_for_pr", "first skip stays at waiting_for_pr"

            run(svc.dispatch_pr_fix(task.id, "feedback 2"))
            self._drain(run, svc, task.id)
            updated = run(svc.db.get_task(task.id))

        assert updated.state == "blocked", (
            f"2nd consecutive skip with cap=2 must transition to blocked, got state={updated.state!r}"
        )
        assert updated.status == "blocked"
        # A status_change message announcing the cap must be present so an
        # operator triaging the block sees *why* it fired.
        status_msgs = run(svc.db.get_messages(task.id, msg_type="status_change"))
        cap_msgs = [m for m in status_msgs if "consecutive" in m.content.lower() or "skipped" in m.content.lower()]
        assert cap_msgs, "consecutive-skip cap must emit a human-readable status_change message"
        # Both the SKIPPED row and the cap-fire BLOCKED row must report the
        # SAME ``round`` — they're attributable to the same dispatch, and
        # this is also what lets the cap-fire path pass ``round_n``
        # to ``_record_pr_decision`` so it can skip the redundant get_task.
        decisions = run(svc.db.get_messages(task.id, msg_type="pr_decision"))
        skipped_rows = [d for d in decisions if d.metadata.get("decision") == "skipped"]
        cap_rows = [d for d in decisions if d.metadata.get("decision") == "blocked"]
        assert skipped_rows and cap_rows, "cap-fire path must write both a SKIPPED and a BLOCKED pr_decision row"
        assert skipped_rows[-1].metadata.get("round") == cap_rows[-1].metadata.get("round") == 2, (
            f"cap-fire BLOCKED row must report the same round as the SKIPPED row that triggered it; got "
            f"skipped_round={skipped_rows[-1].metadata.get('round')!r}, "
            f"cap_round={cap_rows[-1].metadata.get('round')!r}"
        )

    def test_done_resets_consecutive_skipped(self, pr_fix_phase2_service, run):
        """PR_FIX_DONE resets ``pr_fix_consecutive_skipped`` to 0.

        A successful fix means the agent acted on feedback; the previous
        skip streak is no longer evidence the agent is "dismissing
        everything," so the counter starts over.
        """
        from unittest.mock import AsyncMock, patch

        svc = pr_fix_phase2_service
        svc.runner = FakeRunner(
            AgentResult(
                success=True,
                stdout="Fixed the issue.\nPR_FIX_DONE: addressed the brittle test\n",
                stderr="",
                return_code=0,
                duration_ms=200,
                session_id="rd",
            )
        )
        task = self._make_waiting_task(svc, run, pr_fix_consecutive_skipped=2)

        with patch.object(svc, "_execute_push", new=AsyncMock()):
            run(svc.dispatch_pr_fix(task.id, "feedback that triggers a fix"))
            self._drain(run, svc, task.id)

        updated = run(svc.db.get_task(task.id))
        assert updated.metadata.get("pr_fix_consecutive_skipped") == 0, (
            f"PR_FIX_DONE must reset consecutive_skipped, got {updated.metadata.get('pr_fix_consecutive_skipped')!r}"
        )

    def test_blocked_does_not_reset_consecutive_skipped(self, pr_fix_phase2_service, run):
        """PR_FIX_BLOCKED must NOT reset the consecutive-skip counter.

        Design decision flagged in the Phase 2 plan: a manual unblock
        followed by another skip should still count toward the cap.
        Only DONE resets.
        """
        from unittest.mock import AsyncMock, patch

        svc = pr_fix_phase2_service
        svc.runner = FakeRunner(
            AgentResult(
                success=True,
                stdout="PR_FIX_BLOCKED: missing credentials\n",
                stderr="",
                return_code=0,
                duration_ms=10,
                session_id="rb",
            )
        )
        task = self._make_waiting_task(svc, run, pr_fix_consecutive_skipped=2)

        with patch.object(svc, "_execute_push", new=AsyncMock()):
            run(svc.dispatch_pr_fix(task.id, "feedback that blocks"))
            self._drain(run, svc, task.id)

        updated = run(svc.db.get_task(task.id))
        assert updated.metadata.get("pr_fix_consecutive_skipped") == 2, (
            f"BLOCKED must NOT reset consecutive_skipped (design decision), got "
            f"{updated.metadata.get('pr_fix_consecutive_skipped')!r}"
        )

    def test_max_consecutive_skipped_zero_disables_cap(self, pr_fix_phase2_service, run):
        """``max_consecutive_skipped=0`` allows unbounded consecutive skips."""
        from unittest.mock import AsyncMock, patch

        svc = pr_fix_phase2_service
        svc.flow.pr_config.max_consecutive_skipped = 0  # disabled
        svc.runner = FakeRunner(
            AgentResult(
                success=True,
                stdout="PR_FIX_SKIPPED: still nothing\n",
                stderr="",
                return_code=0,
                duration_ms=10,
                session_id="rz",
            )
        )
        task = self._make_waiting_task(svc, run, pr_fix_consecutive_skipped=50)

        with patch.object(svc, "_execute_push", new=AsyncMock()):
            run(svc.dispatch_pr_fix(task.id, "feedback"))
            self._drain(run, svc, task.id)

        updated = run(svc.db.get_task(task.id))
        assert updated.state == "waiting_for_pr", (
            f"with cap disabled, skip stays at waiting_for_pr, got state={updated.state!r}"
        )
        assert updated.metadata.get("pr_fix_consecutive_skipped") == 51

    # ------------------------------------------------------------------
    # Task 6 — pr_decision audit writes
    # ------------------------------------------------------------------

    def test_pr_decision_written_on_done(self, pr_fix_phase2_service, run):
        """PR_FIX_DONE: produces a pr_decision row with decision=done and a commit_sha.

        ``commit_sha`` is read via ``git rev-parse HEAD`` in the worktree.
        The fixture's ``work_dir`` is a plain temp directory (not a git repo)
        so ``_read_head_sha`` returns ``None`` — the row is still written.
        ``test_pr_decision_commit_sha_only_on_done`` pins the inverse
        invariant (SKIPPED/BLOCKED/NEEDS_DECISION rows always have
        ``commit_sha=None``).
        """
        from unittest.mock import AsyncMock, patch

        svc = pr_fix_phase2_service
        svc.runner = FakeRunner(
            AgentResult(
                success=True,
                stdout="Applied fixes.\nPR_FIX_DONE: addressed the lint comments\n",
                stderr="",
                return_code=0,
                duration_ms=345,
                cost_usd=0.012,
                session_id="dn",
            )
        )
        task = self._make_waiting_task(svc, run)

        with patch.object(svc, "_execute_push", new=AsyncMock()):
            run(svc.dispatch_pr_fix(task.id, "fix this please"))
            self._drain(run, svc, task.id)

        decisions = run(svc.db.get_messages(task.id, msg_type="pr_decision"))
        assert decisions, "PR_FIX_DONE must write a pr_decision row"
        row = decisions[-1]
        assert row.metadata.get("decision") == "done"
        assert row.metadata.get("round") == 1
        assert row.metadata.get("duration_ms") == 345
        assert row.metadata.get("cost_usd") == 0.012
        # No git repo in the fixture's work_dir — ``_read_head_sha`` returns
        # ``None`` and the row is still written. Pin the value so a future
        # change that, say, made the helper raise on failure trips this
        # test instead of producing a malformed audit row.
        assert "commit_sha" in row.metadata, "DONE row must always include the commit_sha field, even when None"
        assert row.metadata.get("commit_sha") is None, (
            f"fixture work_dir is not a git repo — commit_sha must be None; got {row.metadata.get('commit_sha')!r}"
        )

    def test_pr_decision_written_on_skipped(self, pr_fix_phase2_service, run):
        """PR_FIX_SKIPPED: produces a pr_decision row with decision=skipped and commit_sha=None."""
        from unittest.mock import AsyncMock, patch

        svc = pr_fix_phase2_service
        svc.runner = FakeRunner(
            AgentResult(
                success=True,
                stdout="Read the feedback.\nPR_FIX_SKIPPED: reviewer approved\n",
                stderr="",
                return_code=0,
                duration_ms=88,
                cost_usd=0.003,
                session_id="sk",
            )
        )
        task = self._make_waiting_task(svc, run)

        with patch.object(svc, "_execute_push", new=AsyncMock()):
            run(svc.dispatch_pr_fix(task.id, "approval-only comment"))
            self._drain(run, svc, task.id)

        decisions = run(svc.db.get_messages(task.id, msg_type="pr_decision"))
        assert decisions, "PR_FIX_SKIPPED must write a pr_decision row"
        row = decisions[-1]
        assert row.metadata.get("decision") == "skipped"
        assert row.metadata.get("commit_sha") is None, "SKIPPED produces no commit"
        assert row.metadata.get("duration_ms") == 88
        assert row.metadata.get("cost_usd") == 0.003

    def test_pr_decision_written_on_agent_blocked(self, pr_fix_phase2_service, run):
        """PR_FIX_BLOCKED: produces a pr_decision row with decision=blocked."""
        from unittest.mock import AsyncMock, patch

        svc = pr_fix_phase2_service
        svc.runner = FakeRunner(
            AgentResult(
                success=True,
                stdout="Cannot proceed.\nPR_FIX_BLOCKED: GITHUB_TOKEN is missing\n",
                stderr="",
                return_code=0,
                duration_ms=42,
                session_id="bk",
            )
        )
        task = self._make_waiting_task(svc, run)

        with patch.object(svc, "_execute_push", new=AsyncMock()):
            run(svc.dispatch_pr_fix(task.id, "feedback"))
            self._drain(run, svc, task.id)

        decisions = run(svc.db.get_messages(task.id, msg_type="pr_decision"))
        assert decisions, "PR_FIX_BLOCKED must write a pr_decision row"
        row = decisions[-1]
        assert row.metadata.get("decision") == "blocked"
        assert row.metadata.get("commit_sha") is None

    def test_pr_decision_written_on_agent_crash(self, pr_fix_phase2_service, run):
        """Agent crash (``success=False``) on a pr-fix step still writes a pr_decision row.

        Acceptance criterion: every pr-fix dispatch produces a pr_decision
        message. A sandboxed OOM, network failure, or unhandled exception
        in the agent process lands in the drainer's ``not result.success``
        branch — without an explicit audit write there, the row is absent
        and the audit trail has a hole. Pin: the row exists with
        ``decision="blocked"``, ``commit_sha=None``, the err_msg captured
        as reasoning, and the task lands in ``state=blocked``.
        """
        from unittest.mock import AsyncMock, patch

        svc = pr_fix_phase2_service
        svc.runner = FakeRunner(
            AgentResult(
                success=False,
                stdout="",
                stderr="OOM killed",
                return_code=1,
                duration_ms=5,
                session_id="crash",
            )
        )
        task = self._make_waiting_task(svc, run)

        with patch.object(svc, "_execute_push", new=AsyncMock()):
            run(svc.dispatch_pr_fix(task.id, "feedback"))
            self._drain(run, svc, task.id)

        after = run(svc.db.get_task(task.id))
        assert after.state == "blocked", (
            f"agent-crash on pr-fix must transition the task to blocked, got state={after.state!r}"
        )
        assert after.status == "blocked"

        decisions = run(svc.db.get_messages(task.id, msg_type="pr_decision"))
        assert decisions, "agent crash on pr-fix must still write a pr_decision row (acceptance criterion #2)"
        row = decisions[-1]
        assert row.metadata.get("decision") == "blocked", (
            f"agent-crash pr_decision must report decision=blocked, got {row.metadata.get('decision')!r}"
        )
        assert row.metadata.get("commit_sha") is None, "crash path produces no commit"
        assert row.metadata.get("duration_ms") == 5
        # Reasoning carries the err_msg the drainer surfaced to the operator,
        # which includes the return_code and the last non-empty stderr line.
        assert "1" in (row.content or ""), (
            f"pr_decision reasoning should carry the return_code in err_msg, got {row.content!r}"
        )
        assert "OOM killed" in (row.content or ""), (
            f"pr_decision reasoning should carry the last stderr line, got {row.content!r}"
        )
        # Round reports the dispatched round (1, post-increment).
        assert row.metadata.get("round") == 1, (
            f"agent-crash row must report the round that ran (1), got {row.metadata.get('round')!r}"
        )

        # The "error" status_change message is still emitted alongside —
        # the pr_decision write is additive, not a replacement.
        errors = run(svc.db.get_messages(task.id, msg_type="error"))
        assert errors, "agent-crash branch must still emit the 'error' message"

    def test_pr_decision_written_on_unrecognized_marker(self, pr_fix_phase2_service, run):
        """Agent succeeds but emits no PR_FIX_* marker → pr_decision row still written.

        Acceptance criterion: every pr-fix dispatch produces a pr_decision
        message. The drainer's ``info.step.rules and rule_target is None``
        branch catches a pr-fix agent that finished cleanly
        (``result.success=True``) without emitting any of the four output
        markers — a misbehaviour, but one the loop must still surface to
        the operator with a durable audit row. Without the explicit write
        in that branch, the row is absent and the audit trail has a hole.
        Pin: the row exists with ``decision="blocked"``, ``commit_sha=None``,
        the marker-fail reasoning, and the task lands in ``state=blocked``.
        """
        from unittest.mock import AsyncMock, patch

        svc = pr_fix_phase2_service
        svc.runner = FakeRunner(
            AgentResult(
                success=True,
                stdout="forgot to emit a marker\n",
                stderr="",
                return_code=0,
                duration_ms=7,
                session_id="no-marker",
            )
        )
        task = self._make_waiting_task(svc, run)

        with patch.object(svc, "_execute_push", new=AsyncMock()):
            run(svc.dispatch_pr_fix(task.id, "feedback"))
            self._drain(run, svc, task.id)

        after = run(svc.db.get_task(task.id))
        assert after.state == "blocked", (
            f"unrecognized-marker on pr-fix must transition to blocked, got state={after.state!r}"
        )
        assert after.status == "blocked"

        decisions = run(svc.db.get_messages(task.id, msg_type="pr_decision"))
        assert decisions, "unrecognized-marker on pr-fix must still write a pr_decision row (acceptance criterion #2)"
        row = decisions[-1]
        assert row.metadata.get("decision") == "blocked", (
            f"unrecognized-marker pr_decision must report decision=blocked, got {row.metadata.get('decision')!r}"
        )
        assert row.metadata.get("commit_sha") is None, "no-marker path produces no commit"
        assert row.metadata.get("duration_ms") == 7
        assert "did not emit a recognized output marker" in (row.content or ""), (
            f"pr_decision reasoning should reference the missing marker, got {row.content!r}"
        )
        # Round reports the dispatched round (1, post-increment).
        assert row.metadata.get("round") == 1, (
            f"unrecognized-marker row must report the round that ran (1), got {row.metadata.get('round')!r}"
        )

        # The operator-facing ``error`` message is still emitted alongside —
        # the pr_decision write is additive, not a replacement.
        errors = run(svc.db.get_messages(task.id, msg_type="error"))
        assert errors, "unrecognized-marker branch must still emit the 'error' message"

    def test_pr_decision_round_reports_dispatched_round(self, pr_fix_phase2_service, run):
        """Normal outcome rows report the round that ran (post-increment).

        Counter starts at 0; after the first dispatch it is 1; the audit
        row's ``round`` field reflects the round that produced the outcome.
        """
        from unittest.mock import AsyncMock, patch

        svc = pr_fix_phase2_service
        svc.runner = FakeRunner(
            AgentResult(
                success=True,
                stdout="PR_FIX_SKIPPED: noise\n",
                stderr="",
                return_code=0,
                duration_ms=10,
                session_id="rdn",
            )
        )
        task = self._make_waiting_task(svc, run, pr_fix_round_count=4)

        with patch.object(svc, "_execute_push", new=AsyncMock()):
            run(svc.dispatch_pr_fix(task.id, "feedback"))
            self._drain(run, svc, task.id)

        decisions = run(svc.db.get_messages(task.id, msg_type="pr_decision"))
        assert decisions
        # Counter was 4 before dispatch, now 5 — the row should report 5.
        assert decisions[-1].metadata.get("round") == 5, (
            f"normal outcome row must report post-increment round (5), got {decisions[-1].metadata.get('round')!r}"
        )

    def test_pr_decision_metadata_includes_triggering_comment_ids(self, pr_fix_phase2_service, run):
        """``triggering_comment_ids`` is a list field on every pr_decision row.

        Phase 2 plumbs comment IDs from the monitor through ``InFlightStep``
        to the audit-write site so the row can be cross-referenced with
        the ``pr_feedback`` rows it responded to.
        """
        from unittest.mock import AsyncMock, patch

        svc = pr_fix_phase2_service
        svc.runner = FakeRunner(
            AgentResult(
                success=True,
                stdout="PR_FIX_SKIPPED: noise\n",
                stderr="",
                return_code=0,
                duration_ms=10,
                session_id="ti",
            )
        )
        task = self._make_waiting_task(svc, run)

        with patch.object(svc, "_execute_push", new=AsyncMock()):
            run(svc.dispatch_pr_fix(task.id, "feedback"))
            self._drain(run, svc, task.id)

        decisions = run(svc.db.get_messages(task.id, msg_type="pr_decision"))
        assert decisions
        meta = decisions[-1].metadata
        assert "triggering_comment_ids" in meta, (
            "Phase 2 pr_decision rows must include triggering_comment_ids in metadata"
        )
        assert isinstance(meta["triggering_comment_ids"], list)

    def test_pr_decision_role_and_step_name(self, pr_fix_phase2_service, run):
        """pr_decision rows are role=agent, step_name=pr-fix per the spec."""
        from unittest.mock import AsyncMock, patch

        svc = pr_fix_phase2_service
        svc.runner = FakeRunner(
            AgentResult(
                success=True,
                stdout="PR_FIX_SKIPPED: noise\n",
                stderr="",
                return_code=0,
                duration_ms=10,
                session_id="rs",
            )
        )
        task = self._make_waiting_task(svc, run)

        with patch.object(svc, "_execute_push", new=AsyncMock()):
            run(svc.dispatch_pr_fix(task.id, "feedback"))
            self._drain(run, svc, task.id)

        decisions = run(svc.db.get_messages(task.id, msg_type="pr_decision"))
        assert decisions
        row = decisions[-1]
        assert row.role == "agent"
        assert row.step_name == "pr-fix"

    def test_pr_decision_reasoning_strips_marker_prefix(self, pr_fix_phase2_service, run):
        """``reasoning`` is the human-readable summary with the ``PR_FIX_<MARKER>:`` prefix stripped.

        Regression test for the PR-review Low finding: DONE/SKIPPED/BLOCKED
        used to write the raw last stdout line into ``reasoning``, while
        NEEDS_DECISION used ``_extract_needs_decision_question`` to strip
        the marker — forcing display/query callers to pattern-match per
        ``decision`` value. The drainer now passes the last line through
        ``_strip_pr_fix_marker_prefix`` so the field is uniformly the
        post-marker text across all decision types.
        """
        from unittest.mock import AsyncMock, patch

        scenarios = [
            ("PR_FIX_DONE: addressed the lint comments\n", "done", "addressed the lint comments"),
            ("PR_FIX_SKIPPED: reviewer approved\n", "skipped", "reviewer approved"),
            ("PR_FIX_BLOCKED: GITHUB_TOKEN is missing\n", "blocked", "GITHUB_TOKEN is missing"),
        ]
        for stdout, expected_decision, expected_reasoning in scenarios:
            svc = pr_fix_phase2_service
            svc.runner = FakeRunner(
                AgentResult(
                    success=True,
                    stdout=stdout,
                    stderr="",
                    return_code=0,
                    duration_ms=10,
                    session_id=f"strip-{expected_decision}",
                )
            )
            task = self._make_waiting_task(svc, run)
            with patch.object(svc, "_execute_push", new=AsyncMock()):
                run(svc.dispatch_pr_fix(task.id, "feedback"))
                self._drain(run, svc, task.id)
            decisions = run(svc.db.get_messages(task.id, msg_type="pr_decision"))
            row = decisions[-1]
            assert row.metadata.get("decision") == expected_decision
            assert row.content == expected_reasoning, (
                f"pr_decision.reasoning must strip the PR_FIX_{expected_decision.upper()}: prefix; "
                f"expected {expected_reasoning!r}, got {row.content!r}"
            )

    # ------------------------------------------------------------------
    # Task 7 — PR_FIX_NEEDS_DECISION escalation
    # ------------------------------------------------------------------

    def test_pr_fix_needs_decision_flips_status_to_needs_input(self, pr_fix_phase2_service, run):
        """PR_FIX_NEEDS_DECISION: flips status=needs_input, state stays at pr-fixing.

        Phase 2 replaces the Phase 1 "alias for blocked" workaround with a
        real escalation: the operator can ``answer()`` the question and
        resume pr-fix, instead of having to unblock and manually re-revise.
        """
        from unittest.mock import AsyncMock, patch

        svc = pr_fix_phase2_service
        svc.runner = FakeRunner(
            AgentResult(
                success=True,
                stdout="Thinking...\nPR_FIX_NEEDS_DECISION: should I also touch module X?\n",
                stderr="",
                return_code=0,
                duration_ms=120,
                session_id="nd",
            )
        )
        task = self._make_waiting_task(svc, run)

        with patch.object(svc, "_execute_push", new=AsyncMock()):
            run(svc.dispatch_pr_fix(task.id, "feedback with judgment call"))
            self._drain(run, svc, task.id)

        updated = run(svc.db.get_task(task.id))
        assert updated.status == "needs_input", (
            f"NEEDS_DECISION must flip status to needs_input, got status={updated.status!r}"
        )
        # State must remain at the pr-fix step's active_state so ``answer()``
        # can re-dispatch the same step. The fixture's pr-fix job has no
        # explicit active_state override, so the active_state defaults to
        # the job name ("pr-fix"); production's flow.yaml sets it to
        # "pr-fixing". The invariant under test is "state did NOT change to
        # blocked / waiting_for_pr / etc." — pin it to the fixture's value.
        pr_fix_step = next(s for s in svc.flow.jobs if s.name == "pr-fix")
        assert updated.state == pr_fix_step.active_state, (
            f"NEEDS_DECISION must NOT change state — answer() resumes pr-fix in place. "
            f"Got state={updated.state!r}, expected {pr_fix_step.active_state!r}"
        )
        assert updated.current_step == "pr-fix"

    def test_pr_fix_needs_decision_persists_question(self, pr_fix_phase2_service, run):
        """The agent's question is persisted as a ``type='question'`` message.

        The React UI's existing chat input renders this prompt — matching
        the existing ``NEEDS_INPUT:`` escalation surface.
        """
        from unittest.mock import AsyncMock, patch

        svc = pr_fix_phase2_service
        svc.runner = FakeRunner(
            AgentResult(
                success=True,
                stdout="PR_FIX_NEEDS_DECISION: rewrite this in Rust per the comment?\n",
                stderr="",
                return_code=0,
                duration_ms=10,
                session_id="pq",
            )
        )
        task = self._make_waiting_task(svc, run)

        with patch.object(svc, "_execute_push", new=AsyncMock()):
            run(svc.dispatch_pr_fix(task.id, "feedback"))
            self._drain(run, svc, task.id)

        questions = run(svc.db.get_messages(task.id, msg_type="question"))
        assert questions, "NEEDS_DECISION must persist the question as a type='question' message"
        assert "Rust" in questions[-1].content, (
            f"persisted question must contain agent's text, got {questions[-1].content!r}"
        )

    def test_pr_fix_needs_decision_writes_pr_decision_audit(self, pr_fix_phase2_service, run):
        """NEEDS_DECISION also produces a pr_decision row with decision=needs_decision."""
        from unittest.mock import AsyncMock, patch

        svc = pr_fix_phase2_service
        svc.runner = FakeRunner(
            AgentResult(
                success=True,
                stdout="PR_FIX_NEEDS_DECISION: should I also touch X?\n",
                stderr="",
                return_code=0,
                duration_ms=44,
                session_id="ndpa",
            )
        )
        task = self._make_waiting_task(svc, run)

        with patch.object(svc, "_execute_push", new=AsyncMock()):
            run(svc.dispatch_pr_fix(task.id, "feedback"))
            self._drain(run, svc, task.id)

        decisions = run(svc.db.get_messages(task.id, msg_type="pr_decision"))
        assert decisions, "NEEDS_DECISION must write a pr_decision row"
        row = decisions[-1]
        assert row.metadata.get("decision") == "needs_decision"
        assert row.metadata.get("commit_sha") is None, "NEEDS_DECISION produces no commit"

    def test_pr_fix_needs_decision_without_question_text_falls_back(self, pr_fix_phase2_service, run):
        """Marker alone (no question text) produces a clear fallback prompt.

        An empty NEEDS_DECISION marker shouldn't leave the operator staring
        at a blank chat input — surface a recognisable placeholder so the
        situation is debuggable.
        """
        from unittest.mock import AsyncMock, patch

        svc = pr_fix_phase2_service
        svc.runner = FakeRunner(
            AgentResult(
                success=True,
                stdout="PR_FIX_NEEDS_DECISION:\n",
                stderr="",
                return_code=0,
                duration_ms=10,
                session_id="fb",
            )
        )
        task = self._make_waiting_task(svc, run)

        with patch.object(svc, "_execute_push", new=AsyncMock()):
            run(svc.dispatch_pr_fix(task.id, "feedback"))
            self._drain(run, svc, task.id)

        questions = run(svc.db.get_messages(task.id, msg_type="question"))
        assert questions, "empty NEEDS_DECISION must still persist a fallback question"
        assert questions[-1].content.strip(), "fallback question must be non-empty"

    def test_answer_to_pr_fix_resumes_with_answer_in_feedback(self, pr_fix_phase2_service, run):
        """``answer()`` on a NEEDS_DECISION pr-fix task resumes with the answer.

        Per the Phase 2 design decision: the operator's answer arrives at
        the resumed pr-fix agent under ``## Revision Feedback`` — the same
        single label used by monitor-driven dispatches. Test name reflects
        the unified label (no separate ``## User Decision`` heading).
        """
        from unittest.mock import AsyncMock, patch

        svc = pr_fix_phase2_service
        # Step 1: drive the task into needs_input via NEEDS_DECISION.
        first_result = AgentResult(
            success=True,
            stdout="PR_FIX_NEEDS_DECISION: should I touch module X?\n",
            stderr="",
            return_code=0,
            duration_ms=10,
            session_id="ar1",
        )
        second_result = AgentResult(
            success=True,
            stdout="Applied per operator decision.\nPR_FIX_DONE: touched module X\n",
            stderr="",
            return_code=0,
            duration_ms=20,
            session_id="ar2",
        )
        svc.runner = SequentialFakeRunner([first_result, second_result])

        task = self._make_waiting_task(svc, run)

        with patch.object(svc, "_execute_push", new=AsyncMock()):
            run(svc.dispatch_pr_fix(task.id, "feedback"))
            self._drain(run, svc, task.id)

            updated = run(svc.db.get_task(task.id))
            assert updated.status == "needs_input", "precondition: task must be in needs_input"

            # Step 2: operator answers — agent should resume with the answer
            # injected as ## Revision Feedback content.
            run(svc.answer(task.id, "yes, also touch module X"))
            self._drain(run, svc, task.id)

        assert len(svc.runner.calls) >= 2, "answer() must trigger a second agent run"
        resumed_prompt = svc.runner.calls[1]["user_prompt"]
        assert "## Revision Feedback" in resumed_prompt, (
            "operator's answer must arrive at the resumed agent under "
            "'## Revision Feedback' — the single feedback label per Phase 2 design"
        )
        assert "yes, also touch module X" in resumed_prompt, (
            "operator's answer text must appear in the resumed agent prompt"
        )

    def test_answer_to_pr_fix_increments_round_counter(self, pr_fix_phase2_service, run):
        """An ``answer()`` resume of pr-fix counts as a new round.

        The operator-driven NEEDS_DECISION → answer round-trip spins up a
        fresh agent run that costs budget; it must increment
        ``pr_fix_round_count`` so the ``max_pr_fix_rounds`` cap applies
        regardless of dispatch entry point.
        """
        from unittest.mock import AsyncMock, patch

        svc = pr_fix_phase2_service
        first_result = AgentResult(
            success=True,
            stdout="PR_FIX_NEEDS_DECISION: should I touch X?\n",
            stderr="",
            return_code=0,
            duration_ms=10,
            session_id="aric1",
        )
        second_result = AgentResult(
            success=True,
            stdout="PR_FIX_DONE: touched X\n",
            stderr="",
            return_code=0,
            duration_ms=20,
            session_id="aric2",
        )
        svc.runner = SequentialFakeRunner([first_result, second_result])
        task = self._make_waiting_task(svc, run)

        with patch.object(svc, "_execute_push", new=AsyncMock()):
            run(svc.dispatch_pr_fix(task.id, "feedback"))
            self._drain(run, svc, task.id)
            after_first = run(svc.db.get_task(task.id))
            assert after_first.metadata.get("pr_fix_round_count") == 1, (
                "precondition: first dispatch must set the counter to 1"
            )
            assert after_first.status == "needs_input", "precondition: NEEDS_DECISION must flip to needs_input"

            run(svc.answer(task.id, "yes, touch X"))
            self._drain(run, svc, task.id)

        after_answer = run(svc.db.get_task(task.id))
        assert after_answer.metadata.get("pr_fix_round_count") == 2, (
            f"answer()-driven resume must bump counter to 2, got {after_answer.metadata.get('pr_fix_round_count')!r}"
        )

    def test_answer_to_pr_fix_round_cap_blocks_resume(self, pr_fix_phase2_service, run):
        """``answer()`` at the round cap transitions to blocked without dispatching.

        Closes the Phase 2 review gap: an operator repeatedly answering
        NEEDS_DECISION on a stuck task must eventually trip the
        ``max_pr_fix_rounds`` cap, not run unbounded.
        """
        from unittest.mock import AsyncMock, patch

        svc = pr_fix_phase2_service
        svc.flow.pr_config.max_pr_fix_rounds = 3
        first_result = AgentResult(
            success=True,
            stdout="PR_FIX_NEEDS_DECISION: a question?\n",
            stderr="",
            return_code=0,
            duration_ms=10,
            session_id="ancap1",
        )
        # Second result should never run — at-cap answer must short-circuit.
        second_result = AgentResult(
            success=True,
            stdout="PR_FIX_DONE: should not happen\n",
            stderr="",
            return_code=0,
            duration_ms=20,
            session_id="ancap2",
        )
        svc.runner = SequentialFakeRunner([first_result, second_result])
        # Pre-load round count to one below cap so the first dispatch hits cap=3.
        task = self._make_waiting_task(svc, run, pr_fix_round_count=2)

        with patch.object(svc, "_execute_push", new=AsyncMock()):
            run(svc.dispatch_pr_fix(task.id, "feedback"))
            self._drain(run, svc, task.id)
            after_first = run(svc.db.get_task(task.id))
            assert after_first.metadata.get("pr_fix_round_count") == 3, "precondition: counter at cap"
            assert after_first.status == "needs_input", "precondition: NEEDS_DECISION must reach needs_input"

            run(svc.answer(task.id, "an answer that should not produce a run"))
            self._drain(run, svc, task.id)

        assert len(svc.runner.calls) == 1, (
            f"answer() at round cap must not run a second agent, got {len(svc.runner.calls)} calls"
        )
        updated = run(svc.db.get_task(task.id))
        assert updated.status == "blocked", f"answer() at cap must transition to blocked, got status={updated.status!r}"
        assert updated.state == "blocked", (
            f"answer() at cap must transition state to blocked, got state={updated.state!r}"
        )
        assert updated.metadata.get("pr_fix_round_count") == 3, "counter must not advance past cap"
        decisions = run(svc.db.get_messages(task.id, msg_type="pr_decision"))
        cap_rows = [d for d in decisions if d.metadata.get("decision") == "blocked" and d.metadata.get("round") == 3]
        assert cap_rows, "answer()-driven cap-fire must write a pr_decision row reporting the cap-firing round"

    def test_answer_to_pr_fix_records_operator_text_on_cap_fire(self, pr_fix_phase2_service, run):
        """Cap-fire branch must persist the operator's answer in the message log.

        Closes a Low-severity audit gap: previously ``answer()`` returned
        early on cap-fire before reaching the ``add_message("answer")``
        call on the normal path, silently dropping the operator's text.
        An operator returning to the task later would see the
        ``pr_decision(blocked)`` row but no record of what they typed.
        """
        from unittest.mock import AsyncMock, patch

        svc = pr_fix_phase2_service
        svc.flow.pr_config.max_pr_fix_rounds = 3
        first_result = AgentResult(
            success=True,
            stdout="PR_FIX_NEEDS_DECISION: a question?\n",
            stderr="",
            return_code=0,
            duration_ms=10,
            session_id="acfa1",
        )
        # Second result must never run — answer() short-circuits at cap.
        second_result = AgentResult(
            success=True,
            stdout="PR_FIX_DONE: should not happen\n",
            stderr="",
            return_code=0,
            duration_ms=20,
            session_id="acfa2",
        )
        svc.runner = SequentialFakeRunner([first_result, second_result])
        task = self._make_waiting_task(svc, run, pr_fix_round_count=2)

        operator_text = "please proceed — I know this will hit the cap"
        with patch.object(svc, "_execute_push", new=AsyncMock()):
            run(svc.dispatch_pr_fix(task.id, "feedback"))
            self._drain(run, svc, task.id)
            after_first = run(svc.db.get_task(task.id))
            assert after_first.status == "needs_input", "precondition: NEEDS_DECISION must reach needs_input"
            assert after_first.metadata.get("pr_fix_round_count") == 3, "precondition: counter at cap"

            run(svc.answer(task.id, operator_text))
            self._drain(run, svc, task.id)

        answer_msgs = run(svc.db.get_messages(task.id, msg_type="answer"))
        assert answer_msgs, "cap-fire must still persist the operator's answer in the message log"
        assert any(m.content == operator_text for m in answer_msgs), (
            f"cap-fire answer must preserve operator text verbatim; got contents={[m.content for m in answer_msgs]!r}"
        )

    def test_answer_to_pr_fix_plumbs_triggering_comment_ids(self, pr_fix_phase2_service, run):
        """``answer()`` resume snapshots triggering comment IDs for the resumed run.

        Closes the Phase 2 review gap: previously the DONE/BLOCKED
        ``pr_decision`` row for an answer-driven resume had an empty
        ``triggering_comment_ids`` list, losing audit-trail continuity.
        """
        from unittest.mock import AsyncMock, patch

        svc = pr_fix_phase2_service

        class _StubMonitor:
            def __init__(self, ids):
                self._ids = ids

            def snapshot_triggering_ids(self, task_id):
                return list(self._ids)

        svc._pr_monitor = _StubMonitor([101, 202, 303])

        first_result = AgentResult(
            success=True,
            stdout="PR_FIX_NEEDS_DECISION: clarify?\n",
            stderr="",
            return_code=0,
            duration_ms=10,
            session_id="tic1",
        )
        second_result = AgentResult(
            success=True,
            stdout="PR_FIX_DONE: applied\n",
            stderr="",
            return_code=0,
            duration_ms=20,
            session_id="tic2",
        )
        svc.runner = SequentialFakeRunner([first_result, second_result])
        task = self._make_waiting_task(svc, run)

        with patch.object(svc, "_execute_push", new=AsyncMock()):
            run(svc.dispatch_pr_fix(task.id, "feedback"))
            self._drain(run, svc, task.id)
            run(svc.answer(task.id, "go ahead"))
            self._drain(run, svc, task.id)

        decisions = run(svc.db.get_messages(task.id, msg_type="pr_decision"))
        done_rows = [d for d in decisions if d.metadata.get("decision") == "done"]
        assert done_rows, "resumed run's DONE outcome must write a pr_decision row"
        assert done_rows[-1].metadata.get("triggering_comment_ids") == [101, 202, 303], (
            f"answer()-driven DONE row must inherit monitor snapshot IDs, got "
            f"{done_rows[-1].metadata.get('triggering_comment_ids')!r}"
        )

    def test_phase1_blocked_alias_for_needs_decision_removed(self, pr_fix_phase2_service, run):
        """Phase 2 must NOT route NEEDS_DECISION to blocked.

        Phase 1's prompt steered the agent to "prefer BLOCKED for judgment
        calls" because NEEDS_DECISION was wired as an alias for blocked.
        Once Phase 2 wires the real escalation, NEEDS_DECISION must land
        the task in needs_input — never blocked.
        """
        from unittest.mock import AsyncMock, patch

        svc = pr_fix_phase2_service
        svc.runner = FakeRunner(
            AgentResult(
                success=True,
                stdout="PR_FIX_NEEDS_DECISION: ambiguous request, how to proceed?\n",
                stderr="",
                return_code=0,
                duration_ms=10,
                session_id="p1r",
            )
        )
        task = self._make_waiting_task(svc, run)

        with patch.object(svc, "_execute_push", new=AsyncMock()):
            run(svc.dispatch_pr_fix(task.id, "feedback"))
            self._drain(run, svc, task.id)

        updated = run(svc.db.get_task(task.id))
        assert updated.status != "blocked", (
            f"Phase 2 must NOT block on NEEDS_DECISION (Phase 1 alias removed), got status={updated.status!r}"
        )
        assert updated.state != "blocked", (
            f"Phase 2 must NOT transition state to blocked on NEEDS_DECISION, got state={updated.state!r}"
        )

    # ------------------------------------------------------------------
    # Task 6 — extra coverage closing review gaps
    # ------------------------------------------------------------------

    def test_pr_decision_commit_sha_only_on_done(self, pr_fix_phase2_service, run):
        """``commit_sha`` is populated *only* on the DONE outcome.

        Plan spec called this out as a required test; it consolidates the
        contract that ``_record_pr_decision``'s SHA capture only fires on
        the DONE branch. Verification splits over two halves:

        * **DONE half** — bypass the orchestrator's dispatch path entirely
          and exercise ``_read_head_sha`` against a real git repo, so the
          test pins both behaviours: SHA capture works when the worktree
          *is* a git repo, AND the value is threaded through to the audit
          row. Going via ``dispatch_pr_fix`` here would force us to also
          spin up a worktree-manager-backed repo (more fixture surface),
          and the worktree-manager codepath is already covered by the
          existing ``TestWorktree`` tests.
        * **Non-DONE half** — SKIPPED + agent-BLOCKED + NEEDS_DECISION,
          each verified produces ``commit_sha=None``. The fixture's
          plain-tmp ``work_dir`` is fine here (it just confirms None is
          used for these branches regardless of git state).
        """
        import subprocess
        from unittest.mock import AsyncMock, patch

        svc = pr_fix_phase2_service

        # ── DONE half — real git repo, real SHA capture ────────────────
        repo_dir = svc.config.work_dir / "_done_sha_repo"
        repo_dir.mkdir(parents=True, exist_ok=True)
        # ``-c`` flags isolate this repo from any global git config (CI
        # boxes sometimes have global commit.gpgsign=true which would
        # break the helper-call below).
        subprocess.run(["git", "init", "-q"], cwd=repo_dir, check=True)
        subprocess.run(
            ["git", "config", "commit.gpgsign", "false"],
            cwd=repo_dir,
            check=True,
        )
        (repo_dir / "README").write_text("seed")
        subprocess.run(["git", "add", "README"], cwd=repo_dir, check=True)
        subprocess.run(
            ["git", "-c", "user.email=t@t", "-c", "user.name=t", "commit", "-q", "-m", "seed"],
            cwd=repo_dir,
            check=True,
        )
        expected_sha = subprocess.check_output(["git", "rev-parse", "HEAD"], cwd=repo_dir, text=True).strip()
        assert expected_sha, "precondition: seeded repo must have a commit"

        # Drive ``_record_pr_decision`` directly with the SHA the helper
        # would return for this repo. This isolates "does the SHA reach
        # the audit row" from any fixture-level worktree-manager wiring.
        from lotsa.orchestrator import _read_head_sha

        done_task = self._make_waiting_task(svc, run)
        captured_sha = run(_read_head_sha(repo_dir))
        assert captured_sha == expected_sha, (
            f"_read_head_sha must return the repo's HEAD SHA; got {captured_sha!r}, expected {expected_sha!r}"
        )
        run(
            svc._record_pr_decision(
                done_task.id,
                decision="done",
                reasoning="addressed feedback",
                triggering_comment_ids=[],
                commit_sha=captured_sha,
                duration_ms=100,
                cost_usd=0.01,
                round_n=1,
            )
        )
        done_rows = run(svc.db.get_messages(done_task.id, msg_type="pr_decision"))
        assert done_rows and done_rows[-1].metadata.get("commit_sha") == expected_sha, (
            f"DONE pr_decision row must carry the captured SHA; got {done_rows[-1].metadata.get('commit_sha')!r}"
        )

        # ── Non-DONE half — each branch produces commit_sha=None ───────
        non_done_cases = [
            ("PR_FIX_SKIPPED: bot chatter\n", "skipped"),
            ("PR_FIX_BLOCKED: missing tool\n", "blocked"),
            ("PR_FIX_NEEDS_DECISION: clarify?\n", "needs_decision"),
        ]
        for stdout, expected_decision in non_done_cases:
            svc.runner = FakeRunner(
                AgentResult(
                    success=True,
                    stdout=stdout,
                    stderr="",
                    return_code=0,
                    duration_ms=10,
                    session_id=f"csod-{expected_decision}",
                )
            )
            task = self._make_waiting_task(svc, run)
            with patch.object(svc, "_execute_push", new=AsyncMock()):
                run(svc.dispatch_pr_fix(task.id, "feedback"))
                self._drain(run, svc, task.id)
            rows = run(svc.db.get_messages(task.id, msg_type="pr_decision"))
            matching = [r for r in rows if r.metadata.get("decision") == expected_decision]
            assert matching, f"{expected_decision} branch must write a pr_decision row"
            assert matching[-1].metadata.get("commit_sha") is None, (
                f"{expected_decision} pr_decision row must have commit_sha=None; "
                f"got {matching[-1].metadata.get('commit_sha')!r}"
            )

    def test_pr_decision_duration_and_cost_optional(self, pr_fix_phase2_service, run):
        """``_record_pr_decision`` accepts ``None`` for duration_ms and cost_usd.

        Plan spec called this out as a required test. The cap-fire paths
        (round-cap pre-dispatch, consecutive-skip cap-fire row) pass
        ``None`` for both fields because there is no agent run to measure
        — the helper must not crash on either value, and the resulting
        row must preserve ``None`` rather than coercing to 0 / "".
        """
        svc = pr_fix_phase2_service
        task = self._make_waiting_task(svc, run)
        run(
            svc._record_pr_decision(
                task.id,
                decision="blocked",
                reasoning="cap fired",
                triggering_comment_ids=[],
                commit_sha=None,
                duration_ms=None,
                cost_usd=None,
                round_n=0,
            )
        )
        rows = run(svc.db.get_messages(task.id, msg_type="pr_decision"))
        assert rows, "row must be written even when duration_ms and cost_usd are None"
        meta = rows[-1].metadata
        assert meta.get("duration_ms") is None, f"duration_ms must round-trip None, got {meta.get('duration_ms')!r}"
        assert meta.get("cost_usd") is None, f"cost_usd must round-trip None, got {meta.get('cost_usd')!r}"
        # And the helper must accept partial-None too — e.g. duration set,
        # cost_usd unset (the agent-run-completed-but-cost-not-available case).
        run(
            svc._record_pr_decision(
                task.id,
                decision="done",
                reasoning="duration only",
                triggering_comment_ids=[],
                commit_sha=None,
                duration_ms=123,
                cost_usd=None,
                round_n=1,
            )
        )
        rows = run(svc.db.get_messages(task.id, msg_type="pr_decision"))
        assert rows[-1].metadata.get("duration_ms") == 123
        assert rows[-1].metadata.get("cost_usd") is None

    # ------------------------------------------------------------------
    # Review gap — revise() on a needs_input pr-fix task must not bypass
    # the round cap and counter bookkeeping.
    # ------------------------------------------------------------------

    def test_revise_on_needs_input_pr_fix_increments_round_counter(self, pr_fix_phase2_service, run):
        """An operator using ``revise()`` instead of ``answer()`` on a stuck
        needs-decision pr-fix task must still increment ``pr_fix_round_count``.

        Pre-fix, ``revise()``'s waiting/needs_input branch called
        ``_dispatch_step`` directly and skipped the pr-fix-specific
        bookkeeping — the operator could trigger unbounded pr-fix rounds
        without tripping the ``max_pr_fix_rounds`` cap.
        """
        from unittest.mock import AsyncMock, patch

        svc = pr_fix_phase2_service
        first_result = AgentResult(
            success=True,
            stdout="PR_FIX_NEEDS_DECISION: what should I do?\n",
            stderr="",
            return_code=0,
            duration_ms=10,
            session_id="rvi1",
        )
        second_result = AgentResult(
            success=True,
            stdout="PR_FIX_DONE: applied per operator decision\n",
            stderr="",
            return_code=0,
            duration_ms=20,
            session_id="rvi2",
        )
        svc.runner = SequentialFakeRunner([first_result, second_result])
        task = self._make_waiting_task(svc, run)

        with patch.object(svc, "_execute_push", new=AsyncMock()):
            run(svc.dispatch_pr_fix(task.id, "feedback"))
            self._drain(run, svc, task.id)
            after_first = run(svc.db.get_task(task.id))
            assert after_first.status == "needs_input", "precondition: NEEDS_DECISION must reach needs_input"
            assert after_first.metadata.get("pr_fix_round_count") == 1, "precondition: first dispatch sets counter to 1"

            # Use revise() (not answer()) — pre-fix this would silently
            # bypass the cap and emit a pr_decision row with empty
            # triggering_comment_ids.
            run(svc.revise(task.id, "go ahead and touch X"))
            self._drain(run, svc, task.id)

        after_revise = run(svc.db.get_task(task.id))
        assert after_revise.metadata.get("pr_fix_round_count") == 2, (
            f"revise()-driven resume must bump counter to 2 like answer() does, "
            f"got {after_revise.metadata.get('pr_fix_round_count')!r}"
        )

    def test_revise_on_needs_input_pr_fix_at_cap_blocks_dispatch(self, pr_fix_phase2_service, run):
        """``revise()`` at the round cap on a needs_input pr-fix task transitions to blocked.

        Closes the Medium review finding: the bypass meant an operator
        repeatedly using ``revise()`` could run unlimited rounds. After
        the fix, ``revise()`` honours the cap exactly like ``answer()``.
        """
        from unittest.mock import AsyncMock, patch

        svc = pr_fix_phase2_service
        svc.flow.pr_config.max_pr_fix_rounds = 3
        first_result = AgentResult(
            success=True,
            stdout="PR_FIX_NEEDS_DECISION: clarify?\n",
            stderr="",
            return_code=0,
            duration_ms=10,
            session_id="rvc1",
        )
        # Second result must never run — revise at cap must short-circuit.
        second_result = AgentResult(
            success=True,
            stdout="PR_FIX_DONE: should not happen\n",
            stderr="",
            return_code=0,
            duration_ms=20,
            session_id="rvc2",
        )
        svc.runner = SequentialFakeRunner([first_result, second_result])
        # Pre-load counter to one below cap so the first dispatch lands at cap=3.
        task = self._make_waiting_task(svc, run, pr_fix_round_count=2)

        with patch.object(svc, "_execute_push", new=AsyncMock()):
            run(svc.dispatch_pr_fix(task.id, "feedback"))
            self._drain(run, svc, task.id)
            after_first = run(svc.db.get_task(task.id))
            assert after_first.metadata.get("pr_fix_round_count") == 3, "precondition: counter at cap"
            assert after_first.status == "needs_input", "precondition: NEEDS_DECISION must reach needs_input"

            run(svc.revise(task.id, "an instruction that should not produce a run"))
            self._drain(run, svc, task.id)

        assert len(svc.runner.calls) == 1, (
            f"revise() at round cap must not run a second agent, got {len(svc.runner.calls)} calls"
        )
        updated = run(svc.db.get_task(task.id))
        assert updated.status == "blocked", f"revise() at cap must transition to blocked, got status={updated.status!r}"
        assert updated.state == "blocked", (
            f"revise() at cap must transition state to blocked, got state={updated.state!r}"
        )
        decisions = run(svc.db.get_messages(task.id, msg_type="pr_decision"))
        cap_rows = [d for d in decisions if d.metadata.get("decision") == "blocked" and d.metadata.get("round") == 3]
        assert cap_rows, "revise()-driven cap-fire must write a pr_decision row reporting the cap-firing round"
        # Operator text must be preserved on the cap-fire path, mirroring
        # the answer()/dispatch_pr_fix() cap-fire behaviour.
        feedback_msgs = run(svc.db.get_messages(task.id, msg_type="feedback"))
        assert any("should not produce a run" in m.content for m in feedback_msgs), (
            "revise() cap-fire must persist the operator's feedback text"
        )

    def test_revise_on_waiting_for_pr_at_cap_persists_operator_feedback(self, pr_fix_phase2_service, run):
        """``revise()`` on a ``waiting_for_pr`` task at the round cap must
        persist the operator's feedback text on cap-fire.

        Closes the Medium review finding: ``revise(waiting_for_pr)`` hands
        the operator's text to ``_dispatch_pr_fix_locked`` as ``user_feedback``,
        which previously persisted that text only AFTER the CAS won — so
        when the round cap fired pre-CAS, the text was silently dropped.
        The other three cap-fire paths (``answer()``,
        ``revise(waiting/needs_input)``, ``send_message()``) all preserve
        the operator's text on cap-fire; this test asserts symmetry for the
        fourth path.
        """
        svc = pr_fix_phase2_service
        svc.flow.pr_config.max_pr_fix_rounds = 3
        # Pre-load counter to cap so the revise dispatch refuses immediately.
        task = self._make_waiting_task(svc, run, pr_fix_round_count=3)

        operator_text = "please look at this comment I just left on the PR"
        run(svc.revise(task.id, operator_text))
        self._drain(run, svc, task.id)

        # No agent run — cap-fire short-circuits before _dispatch_step.
        assert len(svc.runner.calls) == 0, (
            f"revise(waiting_for_pr) at round cap must not run an agent, got {len(svc.runner.calls)} calls"
        )
        updated = run(svc.db.get_task(task.id))
        assert updated.status == "blocked", (
            f"revise(waiting_for_pr) at cap must transition to blocked, got status={updated.status!r}"
        )
        assert updated.state == "blocked", (
            f"revise(waiting_for_pr) at cap must transition state to blocked, got state={updated.state!r}"
        )
        # Audit row reports the cap-firing round (pre-increment value).
        decisions = run(svc.db.get_messages(task.id, msg_type="pr_decision"))
        cap_rows = [d for d in decisions if d.metadata.get("decision") == "blocked" and d.metadata.get("round") == 3]
        assert cap_rows, (
            "revise(waiting_for_pr) cap-fire must write a pr_decision(blocked) row reporting the cap-firing round"
        )
        # Operator's revise text must be preserved verbatim — the bug being
        # fixed dropped this row entirely because the write lived after the
        # CAS in ``_dispatch_pr_fix_locked``.
        feedback_msgs = run(svc.db.get_messages(task.id, msg_type="feedback"))
        assert any(m.content == operator_text for m in feedback_msgs), (
            "revise(waiting_for_pr) cap-fire must persist the operator's feedback text verbatim; "
            f"got contents={[m.content for m in feedback_msgs]!r}"
        )

    def test_revise_from_rebasing_at_cap_does_not_double_persist_feedback(self, pr_fix_phase2_service, run):
        """``revise()`` on a ``rebasing`` task at the round cap must:

        (a) fire the cap inside ``_dispatch_pr_fix_locked``,
        (b) transition the task to ``blocked``,
        (c) preserve the operator's feedback row written at line 702 of
            ``revise()`` (before the helper is called),
        (d) NOT write a duplicate feedback row from ``_dispatch_pr_fix_locked``.

        Background: the rebasing branch calls
        ``_dispatch_pr_fix_locked(task_id, feedback)`` WITHOUT the
        ``user_feedback`` kwarg — the feedback row is written at line 702 of
        ``revise()`` BEFORE the helper runs. The ``user_feedback is not None``
        guard inside ``_dispatch_pr_fix_locked``'s cap-fire branch must
        therefore skip the write, leaving exactly one feedback row in the DB.
        A future re-ordering that moved the helper call before the line-702
        write — or that dropped the ``None`` guard — would silently double
        the feedback (or drop it entirely). This test is the regression net
        for both shapes.
        """
        svc = pr_fix_phase2_service
        svc.flow.pr_config.max_pr_fix_rounds = 3
        # Rebasing task pre-loaded at the cap so the dispatch refuses immediately.
        task = run(
            svc.db.create_task(
                "Rebase + cap",
                state="rebasing",
                metadata={
                    "pr_number": 13,
                    "github_owner": "o",
                    "github_repo": "r",
                    "pr_fix_round_count": 3,
                },
            )
        )

        operator_text = "rebase on main"
        run(svc.revise(task.id, operator_text))
        self._drain(run, svc, task.id)

        # No agent run — cap-fire short-circuits before _dispatch_step.
        assert len(svc.runner.calls) == 0, (
            f"revise(rebasing) at round cap must not run an agent, got {len(svc.runner.calls)} calls"
        )
        updated = run(svc.db.get_task(task.id))
        assert updated.status == "blocked", (
            f"revise(rebasing) at cap must transition to blocked, got status={updated.status!r}"
        )
        assert updated.state == "blocked", (
            f"revise(rebasing) at cap must transition state to blocked, got state={updated.state!r}"
        )
        # Audit row reports the cap-firing round (pre-increment value).
        decisions = run(svc.db.get_messages(task.id, msg_type="pr_decision"))
        cap_rows = [d for d in decisions if d.metadata.get("decision") == "blocked" and d.metadata.get("round") == 3]
        assert cap_rows, (
            "revise(rebasing) cap-fire must write a pr_decision(blocked) row reporting the cap-firing round"
        )
        # Exactly ONE feedback row from line 702 of revise() — the cap-fire
        # branch in _dispatch_pr_fix_locked must NOT double-write because the
        # rebasing path passes user_feedback=None (default).
        feedback_msgs = run(svc.db.get_messages(task.id, msg_type="feedback"))
        matching = [m for m in feedback_msgs if m.content == operator_text]
        assert len(matching) == 1, (
            "revise(rebasing) cap-fire must leave exactly one feedback row (the one written "
            f"at revise()'s line 702); got {len(matching)} matching rows out of "
            f"{len(feedback_msgs)} total feedback rows"
        )

    def test_monitor_driven_dispatch_at_cap_does_not_synthesise_feedback_row(self, pr_fix_phase2_service, run):
        """Monitor-driven ``dispatch_pr_fix`` cap-fire must NOT write a feedback row.

        Guards the ``user_feedback is not None`` predicate added in the
        ``_dispatch_pr_fix_locked`` cap-fire fix. The monitor path passes
        ``user_feedback=None`` because there is no operator text to attribute
        — only the ``pr_decision(blocked)`` audit row should land, never a
        synthesised ``feedback`` row.
        """
        svc = pr_fix_phase2_service
        svc.flow.pr_config.max_pr_fix_rounds = 3
        task = self._make_waiting_task(svc, run, pr_fix_round_count=3)

        # Monitor-driven entry: dispatch_pr_fix() forwards user_feedback=None.
        run(svc.dispatch_pr_fix(task.id, "monitor-supplied review summary"))
        self._drain(run, svc, task.id)

        feedback_msgs = run(svc.db.get_messages(task.id, msg_type="feedback"))
        assert feedback_msgs == [], (
            "monitor-driven cap-fire must not synthesise a feedback row; "
            f"got contents={[m.content for m in feedback_msgs]!r}"
        )
        # Cap-fire audit row still lands.
        decisions = run(svc.db.get_messages(task.id, msg_type="pr_decision"))
        assert any(d.metadata.get("decision") == "blocked" for d in decisions), (
            "monitor-driven cap-fire must still write the pr_decision(blocked) audit row"
        )

    # ------------------------------------------------------------------
    # Review gap — send_message() on a needs_input pr-fix task must not
    # bypass the round cap and counter bookkeeping (fourth entry point).
    # ------------------------------------------------------------------

    def test_send_message_on_needs_input_pr_fix_increments_round_counter(self, pr_fix_phase2_service, run):
        """``send_message()`` on a needs-decision pr-fix task must bump the round counter.

        Closes the Medium review finding on PR #58: ``send_message()`` is a
        fourth entry point (alongside ``answer()``, ``revise()`` and
        ``_dispatch_pr_fix_locked``) that dispatches a new pr-fix agent
        run from ``status in (waiting, needs_input)``. Pre-fix, it skipped
        the pr-fix-specific bookkeeping — the operator could trigger
        unbounded pr-fix rounds without tripping ``max_pr_fix_rounds``.
        """
        from unittest.mock import AsyncMock, patch

        svc = pr_fix_phase2_service
        first_result = AgentResult(
            success=True,
            stdout="PR_FIX_NEEDS_DECISION: what should I do?\n",
            stderr="",
            return_code=0,
            duration_ms=10,
            session_id="smi1",
        )
        second_result = AgentResult(
            success=True,
            stdout="PR_FIX_DONE: applied per operator chat\n",
            stderr="",
            return_code=0,
            duration_ms=20,
            session_id="smi2",
        )
        svc.runner = SequentialFakeRunner([first_result, second_result])
        task = self._make_waiting_task(svc, run)

        with patch.object(svc, "_execute_push", new=AsyncMock()):
            run(svc.dispatch_pr_fix(task.id, "feedback"))
            self._drain(run, svc, task.id)
            after_first = run(svc.db.get_task(task.id))
            assert after_first.status == "needs_input", "precondition: NEEDS_DECISION must reach needs_input"
            assert after_first.metadata.get("pr_fix_round_count") == 1, "precondition: first dispatch sets counter to 1"

            # Use send_message() (not answer() / revise()) — pre-fix this
            # would silently bypass the cap and emit a pr_decision row
            # with empty triggering_comment_ids.
            run(svc.send_message(task.id, "go ahead and touch X"))
            self._drain(run, svc, task.id)

        after_chat = run(svc.db.get_task(task.id))
        assert after_chat.metadata.get("pr_fix_round_count") == 2, (
            f"send_message()-driven resume must bump counter to 2 like answer()/revise() do, "
            f"got {after_chat.metadata.get('pr_fix_round_count')!r}"
        )

    def test_send_message_on_needs_input_pr_fix_at_cap_blocks_dispatch(self, pr_fix_phase2_service, run):
        """``send_message()`` at the round cap on a needs_input pr-fix task transitions to blocked.

        Closes the Medium review finding on PR #58: the bypass meant an
        operator repeatedly using ``send_message()`` could run unlimited
        rounds. After the fix, ``send_message()`` honours the cap exactly
        like ``answer()``/``revise()``.
        """
        from unittest.mock import AsyncMock, patch

        svc = pr_fix_phase2_service
        svc.flow.pr_config.max_pr_fix_rounds = 3
        first_result = AgentResult(
            success=True,
            stdout="PR_FIX_NEEDS_DECISION: clarify?\n",
            stderr="",
            return_code=0,
            duration_ms=10,
            session_id="smc1",
        )
        # Second result must never run — send_message at cap must short-circuit.
        second_result = AgentResult(
            success=True,
            stdout="PR_FIX_DONE: should not happen\n",
            stderr="",
            return_code=0,
            duration_ms=20,
            session_id="smc2",
        )
        svc.runner = SequentialFakeRunner([first_result, second_result])
        # Pre-load counter to one below cap so the first dispatch lands at cap=3.
        task = self._make_waiting_task(svc, run, pr_fix_round_count=2)

        with patch.object(svc, "_execute_push", new=AsyncMock()):
            run(svc.dispatch_pr_fix(task.id, "feedback"))
            self._drain(run, svc, task.id)
            after_first = run(svc.db.get_task(task.id))
            assert after_first.metadata.get("pr_fix_round_count") == 3, "precondition: counter at cap"
            assert after_first.status == "needs_input", "precondition: NEEDS_DECISION must reach needs_input"

            run(svc.send_message(task.id, "a chat message that should not produce a run"))
            self._drain(run, svc, task.id)

        assert len(svc.runner.calls) == 1, (
            f"send_message() at round cap must not run a second agent, got {len(svc.runner.calls)} calls"
        )
        updated = run(svc.db.get_task(task.id))
        assert updated.status == "blocked", (
            f"send_message() at cap must transition to blocked, got status={updated.status!r}"
        )
        assert updated.state == "blocked", (
            f"send_message() at cap must transition state to blocked, got state={updated.state!r}"
        )
        decisions = run(svc.db.get_messages(task.id, msg_type="pr_decision"))
        cap_rows = [d for d in decisions if d.metadata.get("decision") == "blocked" and d.metadata.get("round") == 3]
        assert cap_rows, "send_message()-driven cap-fire must write a pr_decision row reporting the cap-firing round"
        # Operator text must be preserved on the cap-fire path, mirroring
        # the answer()/revise()/dispatch_pr_fix() cap-fire behaviour.
        chat_msgs = run(svc.db.get_messages(task.id, msg_type="chat"))
        assert any("should not produce a run" in m.content for m in chat_msgs), (
            "send_message() cap-fire must persist the operator's chat text"
        )

    def test_send_message_on_needs_input_pr_fix_plumbs_triggering_comment_ids(self, pr_fix_phase2_service, run):
        """``send_message()`` resume snapshots triggering comment IDs for the resumed run.

        Closes the Medium review finding on PR #58: pre-fix, a
        send_message()-driven pr-fix resume produced a pr_decision row
        with ``triggering_comment_ids=[]`` because the snapshot only
        happened in ``_dispatch_pr_fix_locked``/``answer()``/``revise()``.
        After the fix the snapshot fires for ``send_message()`` too,
        preserving audit-trail continuity across every dispatch entry point.
        """
        from unittest.mock import AsyncMock, patch

        svc = pr_fix_phase2_service

        class _StubMonitor:
            def __init__(self, ids):
                self._ids = ids

            def snapshot_triggering_ids(self, task_id):
                return list(self._ids)

        svc._pr_monitor = _StubMonitor([501, 502, 503])

        first_result = AgentResult(
            success=True,
            stdout="PR_FIX_NEEDS_DECISION: clarify?\n",
            stderr="",
            return_code=0,
            duration_ms=10,
            session_id="smti1",
        )
        second_result = AgentResult(
            success=True,
            stdout="PR_FIX_DONE: applied\n",
            stderr="",
            return_code=0,
            duration_ms=20,
            session_id="smti2",
        )
        svc.runner = SequentialFakeRunner([first_result, second_result])
        task = self._make_waiting_task(svc, run)

        with patch.object(svc, "_execute_push", new=AsyncMock()):
            run(svc.dispatch_pr_fix(task.id, "feedback"))
            self._drain(run, svc, task.id)
            run(svc.send_message(task.id, "proceed"))
            self._drain(run, svc, task.id)

        decisions = run(svc.db.get_messages(task.id, msg_type="pr_decision"))
        done_rows = [d for d in decisions if d.metadata.get("decision") == "done"]
        assert done_rows, "send_message()-driven resume's DONE outcome must write a pr_decision row"
        assert done_rows[-1].metadata.get("triggering_comment_ids") == [501, 502, 503], (
            f"send_message()-driven DONE row must inherit monitor snapshot IDs "
            f"(matching answer()/revise()/_dispatch_pr_fix_locked), "
            f"got {done_rows[-1].metadata.get('triggering_comment_ids')!r}"
        )

    def test_revise_on_needs_input_pr_fix_plumbs_triggering_comment_ids(self, pr_fix_phase2_service, run):
        """``revise()`` resume snapshots triggering comment IDs for the resumed run.

        Closes the Medium review finding: pre-fix, a revise()-driven
        pr-fix resume produced a pr_decision row with
        ``triggering_comment_ids=[]`` because the snapshot only happened
        in ``_dispatch_pr_fix_locked``. After the fix the snapshot fires
        for revise() too, preserving audit-trail continuity.
        """
        from unittest.mock import AsyncMock, patch

        svc = pr_fix_phase2_service

        class _StubMonitor:
            def __init__(self, ids):
                self._ids = ids

            def snapshot_triggering_ids(self, task_id):
                return list(self._ids)

        svc._pr_monitor = _StubMonitor([401, 402])

        first_result = AgentResult(
            success=True,
            stdout="PR_FIX_NEEDS_DECISION: clarify?\n",
            stderr="",
            return_code=0,
            duration_ms=10,
            session_id="rvti1",
        )
        second_result = AgentResult(
            success=True,
            stdout="PR_FIX_DONE: applied\n",
            stderr="",
            return_code=0,
            duration_ms=20,
            session_id="rvti2",
        )
        svc.runner = SequentialFakeRunner([first_result, second_result])
        task = self._make_waiting_task(svc, run)

        with patch.object(svc, "_execute_push", new=AsyncMock()):
            run(svc.dispatch_pr_fix(task.id, "feedback"))
            self._drain(run, svc, task.id)
            run(svc.revise(task.id, "proceed"))
            self._drain(run, svc, task.id)

        decisions = run(svc.db.get_messages(task.id, msg_type="pr_decision"))
        done_rows = [d for d in decisions if d.metadata.get("decision") == "done"]
        assert done_rows, "revise()-driven resume's DONE outcome must write a pr_decision row"
        assert done_rows[-1].metadata.get("triggering_comment_ids") == [401, 402], (
            f"revise()-driven DONE row must inherit monitor snapshot IDs (matching answer()/_dispatch_pr_fix_locked), "
            f"got {done_rows[-1].metadata.get('triggering_comment_ids')!r}"
        )

    # ------------------------------------------------------------------
    # Review gap — retry() on a pr-fix task must honour the cap and
    # round counter (fifth entry point, flagged in PR #58 review).
    # ------------------------------------------------------------------

    def test_retry_on_pr_fix_at_cap_writes_fresh_pr_decision_and_refuses(self, pr_fix_phase2_service, run):
        """``retry()`` of a cap-blocked pr-fix task must refuse with a fresh ``pr_decision`` row.

        Closes the Medium review finding on PR #58: pre-fix, ``retry()`` was
        a fifth entry point (alongside ``answer()``/``revise()``/
        ``send_message()``/``_dispatch_pr_fix_locked``) that dispatched a
        new pr-fix agent run without consulting ``_pr_fix_round_cap_blocked``.
        An operator clicking Retry on a cap-blocked task would silently
        bypass the cap, spawn another agent, and emit a ``pr_decision`` row
        with a stale round counter (the old cap-fire value).

        After the fix:
          (a) no agent runs (cap-fire short-circuits before ``_dispatch_step``),
          (b) the task stays in ``(blocked, blocked)`` — no transition needed,
          (c) a fresh ``pr_decision(blocked)`` row is written reporting the
              current cap-firing round (proving the cap helper ran),
          (d) NO redundant ``status_change("PR-fix budget exhausted…")``
              row is written (the ``task_state == "blocked"`` short-circuit
              in ``_pr_fix_round_cap_blocked`` suppresses the no-op CAS +
              status_change so the audit trail stays clean across repeated
              retry attempts).
        """
        svc = pr_fix_phase2_service
        svc.flow.pr_config.max_pr_fix_rounds = 3
        # Cap-blocked pr-fix task: state/status both "blocked", counter at cap.
        task = run(
            svc.db.create_task(
                "Retry cap",
                state="blocked",
                status="blocked",
                current_step="pr-fix",
                metadata={
                    "pr_number": 21,
                    "github_owner": "o",
                    "github_repo": "r",
                    "pr_fix_round_count": 3,
                },
            )
        )

        run(svc.retry(task.id))
        self._drain(run, svc, task.id)

        # (a) Cap-fire short-circuits before any agent run.
        assert len(svc.runner.calls) == 0, (
            f"retry() of cap-blocked pr-fix task must not run an agent, got {len(svc.runner.calls)} calls"
        )
        # (b) Task remains in (blocked, blocked) — counter unchanged.
        updated = run(svc.db.get_task(task.id))
        assert updated.status == "blocked", f"retry() at cap must leave status=blocked, got status={updated.status!r}"
        assert updated.state == "blocked", f"retry() at cap must leave state=blocked, got state={updated.state!r}"
        assert updated.metadata.get("pr_fix_round_count") == 3, (
            f"retry() at cap must not bump the round counter, got {updated.metadata.get('pr_fix_round_count')!r}"
        )
        # (c) Fresh pr_decision row written with the current cap-firing round.
        decisions = run(svc.db.get_messages(task.id, msg_type="pr_decision"))
        cap_rows = [d for d in decisions if d.metadata.get("decision") == "blocked" and d.metadata.get("round") == 3]
        assert cap_rows, (
            "retry() cap-fire must write a fresh pr_decision(blocked) row reporting the cap-firing round; "
            f"got decisions={[d.metadata for d in decisions]!r}"
        )
        # (d) No redundant status_change row — the short-circuit suppresses
        # the no-op CAS + status_change write so repeated retry attempts
        # against a cap-fired task don't accumulate duplicate audit entries.
        status_changes = run(svc.db.get_messages(task.id, msg_type="status_change"))
        budget_msgs = [m for m in status_changes if "PR-fix budget exhausted" in m.content]
        assert budget_msgs == [], (
            "retry() cap-fire must not write a redundant 'PR-fix budget exhausted' status_change; "
            f"got {[m.content for m in status_changes]!r}"
        )

    def test_retry_on_pr_fix_below_cap_bumps_counter(self, pr_fix_phase2_service, run):
        """``retry()`` of a pr-fix task below the cap must bump ``pr_fix_round_count``.

        Closes the Medium review finding: even when retry() is on a task
        that isn't at the cap (e.g. retrying after a transient block from
        agent-emitted ``PR_FIX_BLOCKED:``), the resumed dispatch must
        increment the round counter so the resulting ``pr_decision`` row
        reports the round that actually ran — not the stale pre-retry value.
        Mirrors the post-CAS counter bump in ``answer()``/``revise()``/
        ``send_message()``/``_dispatch_pr_fix_locked``.
        """
        from unittest.mock import AsyncMock, patch

        svc = pr_fix_phase2_service
        svc.flow.pr_config.max_pr_fix_rounds = 10
        # Below-cap pr-fix task: counter=2, cap=10. Retry should bump to 3.
        task = run(
            svc.db.create_task(
                "Retry below cap",
                state="blocked",
                status="blocked",
                current_step="pr-fix",
                metadata={
                    "pr_number": 22,
                    "github_owner": "o",
                    "github_repo": "r",
                    "pr_fix_round_count": 2,
                },
            )
        )

        with patch.object(svc, "_execute_push", new=AsyncMock()):
            run(svc.retry(task.id))
            self._drain(run, svc, task.id)

        after = run(svc.db.get_task(task.id))
        assert after.metadata.get("pr_fix_round_count") == 3, (
            f"retry() below cap must bump pr_fix_round_count from 2 to 3 like the other entry points, "
            f"got {after.metadata.get('pr_fix_round_count')!r}"
        )
        # The agent must have actually run — proves the cap check returned False and dispatch proceeded.
        assert len(svc.runner.calls) >= 1, (
            f"retry() below cap must dispatch a pr-fix agent run, got {len(svc.runner.calls)} calls"
        )

    # ------------------------------------------------------------------
    # Review gap — jump_to_step("pr-fix") must honour the cap and round
    # counter (sixth entry point, flagged in PR #58 review).
    # ------------------------------------------------------------------

    def test_jump_to_pr_fix_at_cap_writes_fresh_pr_decision_and_refuses(self, pr_fix_phase2_service, run):
        """``jump_to_step("pr-fix")`` of a cap-blocked task must refuse with a fresh ``pr_decision`` row.

        Closes the Medium review finding on PR #58: pre-fix, ``jump_to_step``
        was a sixth entry point (alongside ``_dispatch_pr_fix_locked``/
        ``answer()``/``revise()``/``send_message()``/``retry()``) that
        dispatched a new pr-fix agent run without consulting
        ``_pr_fix_round_cap_blocked``. An operator force-jumping a
        cap-blocked task to pr-fix would silently bypass the cap, spawn
        another agent, and emit a ``pr_decision`` row with a stale round
        counter (the old cap-fire value).

        After the fix:
          (a) no agent runs (cap-fire short-circuits before ``_dispatch_step``),
          (b) the task lands in ``(blocked, blocked)`` via the cap-fire CAS,
          (c) a fresh ``pr_decision(blocked)`` row is written reporting the
              current cap-firing round (proving the cap helper ran),
          (d) the counter is NOT bumped (cap-fire short-circuits before the
              post-CAS ``_merge_task_metadata`` increment).
        """
        svc = pr_fix_phase2_service
        svc.flow.pr_config.max_pr_fix_rounds = 3
        # At-cap pr-fix task currently in (waiting_for_pr, waiting_for_pr).
        # The cap-fire CAS will transition it to (blocked, blocked).
        task = self._make_waiting_task(svc, run, pr_fix_round_count=3)

        run(svc.jump_to_step(task.id, "pr-fix"))
        self._drain(run, svc, task.id)

        # (a) Cap-fire short-circuits before any agent run.
        assert len(svc.runner.calls) == 0, (
            f"jump_to_step('pr-fix') of cap-blocked task must not run an agent, got {len(svc.runner.calls)} calls"
        )
        # (b) Task transitioned to (blocked, blocked) via the cap-fire CAS.
        updated = run(svc.db.get_task(task.id))
        assert updated.status == "blocked", (
            f"jump_to_step('pr-fix') at cap must transition status to blocked, got status={updated.status!r}"
        )
        assert updated.state == "blocked", (
            f"jump_to_step('pr-fix') at cap must transition state to blocked, got state={updated.state!r}"
        )
        # (c) Fresh pr_decision row written with the current cap-firing round.
        decisions = run(svc.db.get_messages(task.id, msg_type="pr_decision"))
        cap_rows = [d for d in decisions if d.metadata.get("decision") == "blocked" and d.metadata.get("round") == 3]
        assert cap_rows, (
            "jump_to_step('pr-fix') cap-fire must write a fresh pr_decision(blocked) row reporting "
            f"the cap-firing round; got decisions={[d.metadata for d in decisions]!r}"
        )
        # (d) Counter unchanged — cap-fire short-circuits before the post-CAS bump.
        assert updated.metadata.get("pr_fix_round_count") == 3, (
            f"jump_to_step('pr-fix') at cap must not bump the round counter, "
            f"got {updated.metadata.get('pr_fix_round_count')!r}"
        )

    def test_jump_to_pr_fix_below_cap_bumps_counter_and_dispatches(self, pr_fix_phase2_service, run):
        """``jump_to_step("pr-fix")`` below the cap must bump ``pr_fix_round_count`` and dispatch.

        Closes the Medium review finding: even when ``jump_to_step("pr-fix")``
        is invoked on a task below the cap, the dispatched run must
        increment the round counter so the eventual ``pr_decision`` row
        reports the round that actually ran — not the stale pre-jump value.
        Mirrors the post-CAS counter bump in ``answer()``/``revise()``/
        ``send_message()``/``retry()``/``_dispatch_pr_fix_locked``.
        """
        from unittest.mock import AsyncMock, patch

        svc = pr_fix_phase2_service
        svc.flow.pr_config.max_pr_fix_rounds = 10
        svc.runner = FakeRunner(
            AgentResult(
                success=True,
                stdout="PR_FIX_SKIPPED: nothing actionable\n",
                stderr="",
                return_code=0,
                duration_ms=10,
                session_id="jbc",
            )
        )
        # Below-cap pr-fix task in waiting_for_pr; jump should bump 2 → 3.
        task = self._make_waiting_task(svc, run, pr_fix_round_count=2)

        with patch.object(svc, "_execute_push", new=AsyncMock()):
            run(svc.jump_to_step(task.id, "pr-fix"))
            self._drain(run, svc, task.id)

        after = run(svc.db.get_task(task.id))
        assert after.metadata.get("pr_fix_round_count") == 3, (
            f"jump_to_step('pr-fix') below cap must bump pr_fix_round_count from 2 to 3 like the other "
            f"entry points, got {after.metadata.get('pr_fix_round_count')!r}"
        )
        # The agent must have actually run — proves the cap check returned False and dispatch proceeded.
        assert len(svc.runner.calls) >= 1, (
            f"jump_to_step('pr-fix') below cap must dispatch a pr-fix agent run, got {len(svc.runner.calls)} calls"
        )
        # Every pr-fix dispatch (regardless of entry point) must produce a
        # pr_decision row reporting the round that ran — the acceptance
        # criterion the entry-point gap previously violated.
        decisions = run(svc.db.get_messages(after.id, msg_type="pr_decision"))
        assert decisions, "jump_to_step('pr-fix') dispatch must produce a pr_decision row"
        assert decisions[-1].metadata.get("round") == 3, (
            f"pr_decision row must report the round that ran (3), got {decisions[-1].metadata.get('round')!r}"
        )


class TestMergeTaskMetadata:
    """Regression: drainer metadata writes must not clobber concurrent updates.

    Before the fix, the drainer mutated the in-memory ``item.metadata`` dict
    (captured at dispatch time) and wrote it back, silently overwriting any
    keys added to the DB row in the meantime (e.g. ``pr_comments_since`` set
    by ``PrMonitor._on_feedback``).  ``_merge_task_metadata`` re-reads fresh
    state and merges, preserving concurrent writes.
    """

    def test_merge_preserves_concurrent_metadata(self, service, run):
        from rigg.models import Item

        task = run(service.db.create_task("Merge test", state="coding", metadata={"original": "x"}))
        item = Item(id=task.id, state="coding", metadata={"original": "x"})

        # Simulate a concurrent writer (e.g. PrMonitor) adding a new key
        # to the DB row after dispatch but before the drainer's write.
        run(service.db.update_task(task.id, metadata={"original": "x", "concurrent": "y"}))

        # Drainer-style write that previously would have clobbered "concurrent".
        run(service._merge_task_metadata(item, {"session_id": "s-1"}))

        fresh = run(service.db.get_task(task.id))
        assert fresh.metadata["original"] == "x"
        assert fresh.metadata["concurrent"] == "y"
        assert fresh.metadata["session_id"] == "s-1"
        # In-memory item is also updated so subsequent drainer reads see merged dict.
        assert item.metadata["concurrent"] == "y"
        assert item.metadata["session_id"] == "s-1"

    def test_merge_no_op_when_task_missing(self, service, run):
        """Merging on a deleted task should not raise."""
        from rigg.models import Item

        item = Item(id="missing-task-id", state="coding", metadata={})
        # Should silently no-op rather than raise.
        run(service._merge_task_metadata(item, {"session_id": "s-1"}))


class TestPushDispatchGuard:
    """Regression: concurrent _dispatch_next_step calls must not spawn parallel pushes."""

    def test_concurrent_pushing_dispatch_runs_once(self, service, run):
        """Two back-to-back dispatches for state='pushing' should produce one push task."""
        from rigg.models import Item

        task = run(service.db.create_task("Push guard", state="pushing", metadata={}))
        item1 = Item(id=task.id, state="pushing", metadata={})
        item2 = Item(id=task.id, state="pushing", metadata={})

        # Replace _execute_push with a slow stub so the first invocation is
        # still in-flight when the second tries to dispatch.
        completed = asyncio.Event()
        call_count = {"n": 0}

        async def slow_push(_item: Item) -> None:
            call_count["n"] += 1
            await asyncio.sleep(0.05)
            completed.set()

        service._execute_push = slow_push  # type: ignore[assignment]

        run(service._dispatch_next_step(item1))
        run(service._dispatch_next_step(item2))  # should be a no-op (guard held)

        run(completed.wait())
        run(asyncio.sleep(0.05))  # let the done_callback fire

        assert call_count["n"] == 1
        assert task.id not in service._dispatching_push
        assert task.id not in service._push_tasks


class TestRulesBlockOnNoMatch:
    """Regression: a step with rules but no match should block, not auto-advance."""

    @pytest.fixture()
    def block_service(self, tmp_path, _loop, run):
        """Two-step flow where step1 has a rule that won't match the agent's output."""
        flow_yaml = tmp_path / "block_flow.yaml"
        flow_yaml.write_text(
            "name: block-test\njobs:\n"
            "  - name: gate\n    prompt: gate\n"
            "    queue_state: backlog\n    active_state: gating\n"
            "    rules:\n"
            "      - source: stdout\n        pattern: '^GATE_OK'\n        target: next\n"
            "      - source: stdout\n        pattern: '^GATE_FAIL'\n        target: blocked\n"
            "  - name: tail\n    prompt: tail\n"
            "    queue_state: tailing\n    active_state: tailing\n"
        )
        prompts_dir = tmp_path / "prompts"
        prompts_dir.mkdir()
        for name in ("gate-system", "gate-user", "tail-system", "tail-user"):
            (prompts_dir / f"{name}.md").write_text(f"# {name}\n{{title}}\n{{body}}")
        config = LotsaConfig(
            data_dir=tmp_path / "data",
            work_dir=tmp_path,
            flow="custom",
            flow_file=flow_yaml,
            prompts_dir=prompts_dir,
            model="sonnet",
            budget=5.0,
        )
        (tmp_path / "data").mkdir()
        db = TaskDB(tmp_path / "data" / "lotsa.db")
        run(db.initialize())
        svc = OrchestratorService(config, db)
        # Agent emits no marker — neither GATE_OK nor GATE_FAIL.
        svc.runner = FakeRunner(
            AgentResult(success=True, stdout="forgot to emit marker", stderr="", return_code=0, duration_ms=10)
        )
        run(svc.start())
        yield svc
        run(svc.shutdown())
        run(db.close())

    def test_unmatched_rules_block_rather_than_advance(self, block_service, run):
        """When a step has rules and none match, the task should land in blocked, not the next step."""
        svc = block_service
        run(svc.create_task("Block test"))
        run(asyncio.sleep(0.5))

        tasks = run(svc.db.list_tasks())
        assert tasks[0].state == "blocked", f"Expected blocked when no rule matches, got {tasks[0].state!r}"


class TestConversationalAutoAdvanceDuringPrFix:
    """Conversational steps gate at status=waiting in the initial pipeline,
    but auto-advance on rule match once a PR exists (pr_number set in task
    metadata). This eliminates the manual-approval friction during pr-fix
    loops where the operator already approved the original work.
    """

    @pytest.fixture()
    def two_step_service(self, tmp_path, _loop, run):
        """Conversational verify-like step → non-conversational next step.

        The two-step shape is necessary: after auto-advance, we need a
        sequential next step so dispatch lands somewhere observable.
        """
        flow_yaml = tmp_path / "two_step_flow.yaml"
        flow_yaml.write_text(
            "name: two-step\njobs:\n"
            "  - name: verifylike\n    prompt: verify\n    conversational: true\n"
            "    queue_state: backlog\n    active_state: verifylike\n"
            "    rules:\n"
            '      - source: stdout\n        pattern: "^VERIFIED:"\n        target: next\n'
            "  - name: nextstep\n    prompt: code\n"
            "    queue_state: nextstep_q\n    active_state: nextstep\n"
        )
        prompts_dir = tmp_path / "prompts"
        prompts_dir.mkdir()
        (prompts_dir / "verify-system.md").write_text("# Verify\n{title}\n{body}")
        (prompts_dir / "verify-user.md").write_text("{title}\n{body}")
        (prompts_dir / "code-system.md").write_text("# Code\n{title}\n{body}")
        (prompts_dir / "code-user.md").write_text("{title}\n{body}")
        config = LotsaConfig(
            data_dir=tmp_path / "data",
            work_dir=tmp_path,
            flow="custom",
            flow_file=flow_yaml,
            prompts_dir=prompts_dir,
            model="sonnet",
            budget=5.0,
        )
        (tmp_path / "data").mkdir()
        db = TaskDB(tmp_path / "data" / "lotsa.db")
        run(db.initialize())
        svc = OrchestratorService(config, db)
        svc.runner = FakeRunner(
            AgentResult(
                success=True,
                stdout="VERIFIED: tests pass, lint clean",
                stderr="",
                return_code=0,
                duration_ms=500,
                session_id="sess-verify",
            )
        )
        run(svc.start())
        yield svc
        run(svc.shutdown())
        run(db.close())

    def test_gates_to_waiting_when_pr_number_unset(self, two_step_service, run):
        """Initial pipeline path: pr_number absent → conversational gate fires.

        Regression guard. The pre-PR rollout still wants the operator to
        eyeball verify output before the push step proceeds.
        """
        svc = two_step_service
        task = run(svc.create_task("Initial pipeline"))
        run(asyncio.sleep(0.3))

        row = run(svc.db.get_task(task.id))
        assert row is not None
        assert row.status == "waiting", f"Expected gate at status=waiting, got {row.status!r}"
        assert row.state == "verifylike", f"Expected still in verifylike state, got {row.state!r}"

    def test_auto_advances_when_pr_number_set(self, two_step_service, run):
        """Pr-fix loop path: pr_number set → conversational gate skipped,
        rule's target=next resolves to success_state, dispatch advances.

        Bypasses ``OrchestratorService.create_task`` because that helper
        dispatches the first step immediately — the test needs ``pr_number``
        in the task's metadata *before* dispatch so the in-memory Item the
        drainer sees carries it. Mirrors the production sequence where
        pr_number lands in metadata at push time, well before any
        subsequent verify dispatch.
        """
        from rigg.models import Item

        svc = two_step_service

        async def _setup_and_dispatch():
            task = await svc.db.create_task(
                title="Pr-fix loop",
                flow_name=svc.config.flow or "custom",
                state="backlog",
                metadata={"pr_number": 99},
            )
            first_step = svc.flow.steps[0]
            item = Item(
                id=task.id,
                state=first_step.queue_state,
                title=task.title,
                body="",
                metadata=task.metadata,
            )
            await svc._dispatch_step(item, first_step, feedback=None)
            return task

        task = run(_setup_and_dispatch())
        run(asyncio.sleep(0.5))

        row = run(svc.db.get_task(task.id))
        assert row is not None
        # Positive assertions for the expected post-advance state. Without
        # these the test would silently pass for an unintended outcome
        # (e.g. a task that landed in 'blocked' satisfies status != 'waiting'
        # AND state != 'verifylike' AND would have escaped detection).
        # The flow is two steps: verifylike (conversational, emits
        # VERIFIED → next) → nextstep (no rules, auto-advances on success
        # to complete since it's the last step). FakeRunner is instant
        # so the second step may also have completed within the sleep
        # window — accept both intermediate ("nextstep_q"/"nextstep") and
        # terminal ("complete") states.
        assert row.state in {"nextstep_q", "nextstep", "complete"}, (
            f"Expected state to have advanced to nextstep or complete, got {row.state!r}"
        )
        assert row.status in {"working", "complete"}, (
            f"Expected status working/complete after auto-advance, got {row.status!r}"
        )
        # Defensive negative — the gate must NOT have fired.
        assert row.status != "waiting", (
            f"Expected auto-advance past the gate, got status={row.status!r} (state={row.state!r})"
        )


class TestDispatchSubFlow:
    """``dispatch_sub_flow`` is the ADR-014 Layer B forward-compatible entry
    point engines call to drive into a sub-flow. In Layer A only ``pr_fix``
    is wired; the call forwards to ``dispatch_pr_fix``.
    """

    @pytest.fixture()
    def svc(self, tmp_path, run):
        data_dir = tmp_path / "data"
        data_dir.mkdir(parents=True, exist_ok=True)
        config = LotsaConfig(
            data_dir=data_dir,
            work_dir=tmp_path,
            flow="build",
            model="sonnet",
            budget=5.0,
        )
        db = TaskDB(data_dir / "lotsa.db")
        run(db.initialize())
        svc = OrchestratorService(config, db)
        svc.runner = FakeRunner()
        run(svc.start())
        yield svc
        run(svc.shutdown())
        run(db.close())

    def test_dispatch_sub_flow_forwards_pr_fix_to_dispatch_pr_fix(self, svc, run):
        calls: list[tuple[str, str]] = []

        async def fake_dispatch_pr_fix(task_id: str, feedback: str) -> bool:
            calls.append((task_id, feedback))
            return True

        svc.dispatch_pr_fix = fake_dispatch_pr_fix  # type: ignore[method-assign]
        ok = run(svc.dispatch_sub_flow("task-1", "pr_fix", feedback="hello"))
        assert ok is True
        assert calls == [("task-1", "hello")]

    def test_dispatch_sub_flow_propagates_decline_from_dispatch_pr_fix(self, svc, run):
        """When dispatch_pr_fix declines (CAS lost, cap fired), dispatch_sub_flow
        must surface ``False`` so callers (Layer B engines) can react."""
        calls: list[tuple[str, str]] = []

        async def fake_dispatch_pr_fix(task_id: str, feedback: str) -> bool:
            calls.append((task_id, feedback))
            return False

        svc.dispatch_pr_fix = fake_dispatch_pr_fix  # type: ignore[method-assign]
        ok = run(svc.dispatch_sub_flow("task-1", "pr_fix", feedback="hello"))
        assert ok is False
        assert calls == [("task-1", "hello")]

    def test_dispatch_sub_flow_rejects_unknown_flow_name(self, svc, run):
        ok = run(svc.dispatch_sub_flow("task-1", "no_such_flow"))
        assert ok is False


# ───────────────────────────────────────────────────────────────────────────
# ADR-027 — Operator-driven process promotion (PR 1: mechanism)
# ───────────────────────────────────────────────────────────────────────────


def _load_build_destination(service):
    """Load the bundled ``full`` process into the service catalog as a
    promotion destination, returning it.

    The ``service`` fixture's active process is a single-step ``test`` flow;
    promotion targets must be present in ``_processes`` (ADR-027 §1: the
    destination must be loaded). This mirrors the catalog-insertion shape used
    by ``TestArtifactCapture`` / ``TestArtifactInputValidation``.
    """
    from lotsa.flows import build_process

    build = build_process("build")
    service._processes["build"] = build
    return build


def _load_chat_destination(service):
    """Load the bundled ``chat`` process into the service catalog, returning it.

    Used to exercise the ADR-027 §7 no-demotion guard: ``chat`` is a *loaded*
    process here, so the only thing that may reject ``promote_task(..., "chat")``
    is the explicit destination guard — not the "unknown process" precondition.
    """
    from lotsa.flows import build_process

    chat = build_process("chat")
    service._processes["chat"] = chat
    return chat


def _wait_until_waiting(service, run, task_id):
    """Pump the loop until the just-created source task reaches status='waiting'."""
    for _ in range(10):
        run(asyncio.sleep(0.05))
        row = run(service.db.get_task(task_id))
        if row is not None and row.status == "waiting":
            return row
    return run(service.db.get_task(task_id))


class TestPromote:
    """``OrchestratorService.promote_task`` — the operator-only, CAS-guarded,
    cross-process handover (ADR-027 §1/§5).

    Regression note (CLAUDE.md): ``promote_task`` and ``PromoteNotAllowed`` do
    not exist pre-fix, so every test here fails against the pre-fix tree with
    ``AttributeError`` / ``ImportError``. The reject tests additionally assert
    the row is *unmutated*, exercising the precondition from inside the method
    rather than by pre-flipping external state.
    """

    def test_non_terminal_source_succeeds(self, service, run):
        _load_build_destination(service)
        task = run(service.create_task("Explore an idea"))
        _wait_until_waiting(service, run, task.id)

        run(service.promote_task(task.id, "build"))
        run(asyncio.sleep(0.2))

        row = run(service.db.get_task(task.id))
        assert row is not None
        # Process identity switched to the destination, sub-flow context reset.
        assert row.metadata.get("process_name") == "build"
        assert row.metadata.get("current_flow") == "main"
        # Entered the destination pipeline. ``plan`` is ungated (ADR-043), so with
        # the FakeRunner it auto-advances rather than pausing at the first step —
        # the invariant is that the task now runs a build step, not the chat REPL.
        build_steps = {s.name for s in service._processes["build"].flows["main"].jobs}
        assert row.current_step in build_steps, f"unexpected step {row.current_step!r}"

    def test_promote_updates_flow_name_label(self, service, run):
        """After promotion the ``flow_name`` label column tracks the new process
        (feeds ``TaskDetail.flow_name`` + audit), not just ``metadata``."""
        _load_build_destination(service)
        task = run(service.create_task("Explore an idea"))
        _wait_until_waiting(service, run, task.id)
        run(service.promote_task(task.id, "build"))
        run(asyncio.sleep(0.1))
        assert run(service.db.get_task(task.id)).flow_name == "build"

    def test_task_detail_uses_the_tasks_own_flow_not_the_active_default(self, service, run):
        """Regression: the task-detail endpoint must surface each task's OWN flow
        steps (the header/stage bar), not the server's active flow. Pre-fix every
        task showed the active flow's steps (e.g. the single ``chat`` stage)."""
        from lotsa.server.api_routes import _build_task_detail

        _load_build_destination(service)
        task = run(service.create_task("Explore an idea"))
        _wait_until_waiting(service, run, task.id)
        run(service.promote_task(task.id, "build"))
        run(asyncio.sleep(0.1))

        detail = run(_build_task_detail(service, task.id))
        step_names = [s.name for s in detail.flow.steps]
        assert "plan" in step_names, f"expected the build pipeline, got {step_names}"
        assert step_names != ["chat"], "still showing the chat flow's single stage"

    def test_chat_transcript_renders_user_and_assistant(self, service, run):
        task = run(service.create_task("Explore an idea"))
        run(service.db.add_message(task.id, "user", "chat", "make the columns scroll independently", "chat"))
        run(service.db.add_message(task.id, "agent", "chat", "Fix: set height:100vh on #root", "output"))
        t = run(service._chat_transcript(task.id))
        assert "**User:** make the columns scroll independently" in t
        assert "**Assistant:** Fix: set height:100vh on #root" in t

    def test_promote_from_chat_carries_conversation_as_draft_spec(self, service, run):
        """Regression: promoting from chat must hand the full conversation to the
        destination's first step (not just the truncated title), so `spec` doesn't
        report the request "cut off"."""
        _load_build_destination(service)
        task = run(service.create_task("Explore an idea"))
        full_request = "the full request that the auto-title truncates badly mid-sentence"
        run(service.db.add_message(task.id, "user", "chat", full_request, "chat"))
        _wait_until_waiting(service, run, task.id)

        run(service.promote_task(task.id, "build"))  # no explicit handover
        run(asyncio.sleep(0.1))

        draft = run(service.get_named_artifact(task.id, "draft_spec"))
        assert draft is not None, "promotion from chat should auto-seed draft_spec"
        assert full_request in draft

    @pytest.mark.parametrize("dest", ["build", "fix"])
    def test_bundled_preset_promotable_without_manual_load(self, service, run, dest):
        """ADR-034 §1/§5 acceptance #4 — every bundled preset is a valid
        promotion destination straight out of the box, with no test-only
        ``_load_<x>_destination`` insertion.

        The ``service`` fixture's active process is a single-step custom flow
        loaded via ``--flow-file``. Pre-ADR-034 that means ONLY that process is
        in ``_processes`` — so ``promote_task(..., 'full')`` (and quickfix /
        standard / simple) raises ``PromoteNotAllowed('Unknown process …')``.
        ADR-034's ``start()`` loads the full bundled catalog, so each promotion
        now succeeds. This test deliberately does NOT call
        ``_load_build_destination`` — that is the behaviour under test.
        """
        task = run(service.create_task("Explore an idea"))
        _wait_until_waiting(service, run, task.id)

        # Must not raise PromoteNotAllowed: the destination is auto-loaded.
        run(service.promote_task(task.id, dest))
        run(asyncio.sleep(0.2))

        row = run(service.db.get_task(task.id))
        assert row is not None
        assert row.metadata.get("process_name") == dest, (
            f"promotion to bundled preset {dest!r} must land (ADR-034 §5); got "
            f"process_name={row.metadata.get('process_name')!r}"
        )
        assert row.metadata.get("current_flow") == "main"

    def test_records_process_promotion_audit_with_old_and_new(self, service, run):
        full = _load_build_destination(service)  # noqa: F841 — loaded as destination
        task = run(service.create_task("Explore an idea"))
        _wait_until_waiting(service, run, task.id)
        src_name = run(service.db.get_task(task.id)).metadata.get("process_name")

        run(service.promote_task(task.id, "build"))
        run(asyncio.sleep(0.1))

        promo = run(service.db.get_messages(task.id, msg_type="process_promotion"))
        assert len(promo) == 1, "expected exactly one process_promotion audit row"
        meta = promo[0].metadata
        assert meta.get("old_process") == src_name
        assert meta.get("new_process") == "build"

    def test_unknown_destination_rejected_and_unmutated(self, service, run):
        from lotsa.orchestrator import PromoteNotAllowed

        task = run(service.create_task("Explore an idea"))
        _wait_until_waiting(service, run, task.id)
        before = run(service.db.get_task(task.id))

        with pytest.raises(PromoteNotAllowed):
            run(service.promote_task(task.id, "no_such_process"))

        after = run(service.db.get_task(task.id))
        # Nothing changed — not the process name, not the state.
        assert after.metadata.get("process_name") == before.metadata.get("process_name")
        assert after.state == before.state
        assert after.status == before.status

    def test_unknown_destination_error_omits_chat_from_available(self, service, run):
        """The "unknown process" suggestion list must not include ``chat``:
        even though ``chat`` is loaded, the no-demotion guard rejects it, so
        listing it here would invite a second confusing rejection.

        Regression: pre-fix the list was ``sorted(self._processes.keys())``,
        which includes ``chat`` whenever it is loaded — this assertion fails
        against that code with ``'chat' in "... Available: ['chat', 'full']"``.
        """
        from lotsa.orchestrator import PromoteNotAllowed

        _load_chat_destination(service)
        _load_build_destination(service)
        task = run(service.create_task("Explore an idea"))
        _wait_until_waiting(service, run, task.id)

        with pytest.raises(PromoteNotAllowed) as excinfo:
            run(service.promote_task(task.id, "no_such_process"))

        msg = str(excinfo.value)
        assert "chat" not in msg, f"chat must not be offered as a promotion destination: {msg!r}"
        # A genuinely valid destination is still surfaced.
        assert "build" in msg

    def test_promote_to_chat_rejected_and_unmutated(self, service, run):
        """ADR-027 §7 — no demotion. Even with ``chat`` loaded as a valid
        process, promoting *into* it is refused server-side (the dashboard
        filters it from the picker, but the CLI / raw API must enforce it too).

        Regression: pre-fix there was no destination guard, so with ``chat``
        in the catalog this promotion would *succeed* and mutate the row to
        ``process_name='chat'`` — the test would fail to raise.
        """
        from lotsa.orchestrator import PromoteNotAllowed

        _load_chat_destination(service)
        task = run(service.create_task("Explore an idea"))
        _wait_until_waiting(service, run, task.id)
        before = run(service.db.get_task(task.id))

        with pytest.raises(PromoteNotAllowed):
            run(service.promote_task(task.id, "chat"))

        after = run(service.db.get_task(task.id))
        assert after.metadata.get("process_name") == before.metadata.get("process_name")
        assert after.metadata.get("process_name") != "chat"
        assert after.state == before.state
        assert after.status == before.status

    def test_terminal_source_rejected_and_unmutated(self, service, run):
        from lotsa.orchestrator import PromoteNotAllowed

        _load_build_destination(service)
        task = run(service.create_task("Explore an idea"))
        _wait_until_waiting(service, run, task.id)
        # Drive the source to a terminal state (a legitimate precondition input,
        # not a post-bug state — promotion must refuse any terminal task).
        run(service.db.update_task(task.id, state="complete", status="complete"))

        with pytest.raises(PromoteNotAllowed):
            run(service.promote_task(task.id, "build"))

        after = run(service.db.get_task(task.id))
        assert after.metadata.get("process_name") != "build"
        assert after.state == "complete"

    def test_archived_source_rejected_and_unmutated(self, service, run):
        from lotsa.orchestrator import PromoteNotAllowed

        _load_build_destination(service)
        task = run(service.create_task("Explore an idea"))
        _wait_until_waiting(service, run, task.id)
        # ``archived`` is status-only: ``archive()`` preserves the prior ``state``
        # (here a non-terminal ``speccing``), so a guard that only checks the two
        # core terminal names on both columns would let this through — but the
        # task's worktree + branch are gone, so promotion must refuse it.
        prior_state = run(service.db.get_task(task.id)).state
        run(service.db.update_task(task.id, status="archived"))

        with pytest.raises(PromoteNotAllowed):
            run(service.promote_task(task.id, "build"))

        after = run(service.db.get_task(task.id))
        assert after.metadata.get("process_name") != "build"
        assert after.status == "archived"
        assert after.state == prior_state

    def test_seeds_initial_artifacts_and_audit(self, service, run):
        _load_build_destination(service)
        task = run(service.create_task("Explore an idea"))
        _wait_until_waiting(service, run, task.id)

        draft = "# Draft spec\n\nBuild a cache layer."
        run(service.promote_task(task.id, "build", {"draft_spec": draft}))
        run(asyncio.sleep(0.1))

        # The artifact is readable under its declared name (so the destination's
        # first step can inject it).
        content = run(service.get_named_artifact(task.id, "draft_spec"))
        assert content == draft

        # And an ``artifact_seeded`` audit event records the seeding with source.
        seeded = run(service.db.get_messages(task.id, msg_type="artifact_seeded"))
        assert len(seeded) == 1
        assert seeded[0].metadata.get("artifact_name") == "draft_spec"
        assert seeded[0].metadata.get("source") == "promotion"

    def test_dispatches_destination_first_step(self, service, run):
        _load_build_destination(service)
        task = run(service.create_task("Explore an idea"))
        _wait_until_waiting(service, run, task.id)

        before = len(service.runner.calls)
        run(service.promote_task(task.id, "build"))
        run(asyncio.sleep(0.2))

        assert len(service.runner.calls) > before, "destination first step was not dispatched"

    def test_worktree_and_messages_preserved(self, service, run):
        """Promotion is not task creation — the message log accumulates across
        the switch (ADR-027 §1). The pre-promotion chat/feedback rows survive."""
        _load_build_destination(service)
        task = run(service.create_task("Explore an idea"))
        _wait_until_waiting(service, run, task.id)
        before_count = len(run(service.db.get_messages(task.id)))

        run(service.promote_task(task.id, "build"))
        run(asyncio.sleep(0.1))

        after_count = len(run(service.db.get_messages(task.id)))
        assert after_count >= before_count, "messages must be append-only across promotion"


class TestAvailableProcessesRendering:
    """ADR-027 §3 — the chat agent's prompt gets a data-driven *available
    processes* block rendered from loaded processes' ``description`` fields.

    Fails pre-fix: ``_render_available_processes`` does not exist
    (``AttributeError``)."""

    def test_renders_loaded_process_descriptions(self, service, run):
        full = _load_build_destination(service)
        full.description = "Full SDLC: spec, plan, test, code, review, verify, push."

        rendered = service._render_available_processes()
        assert "build" in rendered
        assert "Full SDLC" in rendered

    def test_excludes_processes_without_a_description(self, service, run):
        full = _load_build_destination(service)
        full.description = None  # no description → not surfaced to triage

        rendered = service._render_available_processes()
        # Match the rendered entry marker ``- build:`` rather than the bare word
        # "build": ADR-034 auto-loads the whole catalog, and other presets'
        # descriptions legitimately contain the substring "build". The invariant
        # under test is that the description-less ``build`` process has no entry
        # of its own.
        assert "- build:" not in rendered

    def test_excludes_the_chat_process_itself(self, service, run, tmp_path):
        from lotsa.flows import build_process_from_inline

        chat = build_process_from_inline(
            "chat",
            {"steps": [{"name": "chat", "conversational": True}]},
            base_dir=tmp_path,
        )
        chat.description = "Exploration and triage."
        service._processes["chat"] = chat

        rendered = service._render_available_processes()
        # The chat agent should not be offered the option of promoting to chat.
        assert "Exploration and triage" not in rendered


class TestListProcessesSummaryPromotionFields:
    """ADR-027 §3/§4 — the process catalog surfaced to the dashboard carries
    ``description`` and ``promotion_inputs`` so the promotion modal can render
    per-destination input fields.

    Fails pre-fix: the summary dict has no ``description`` key (``KeyError``)
    and ``PromotionInput`` is not importable (``ImportError``)."""

    def test_summary_includes_description_and_promotion_inputs(self, service, run):
        from lotsa.flows import PromotionInput, build_process

        full = build_process("build")
        full.description = "Full SDLC"
        full.promotion_inputs = [
            PromotionInput(name="draft_spec", description="A spec to verify rather than re-elicit.")
        ]
        service._processes["build"] = full

        summaries = service.list_processes_summary()
        full_summary = next(s for s in summaries if s["name"] == "build")
        assert full_summary["description"] == "Full SDLC"
        assert full_summary["promotion_inputs"] == [
            {"name": "draft_spec", "description": "A spec to verify rather than re-elicit."}
        ]


# ---------------------------------------------------------------------------
# ADR-015 Phase 1 — orchestrator syncs the task branch to main before pr-fix.
#
# The orchestrator must, as a precondition of every pr-fix dispatch:
#   1. ``git fetch origin main`` (unconditional — the local ``origin/main``
#      tracking ref is otherwise stale and the divergence count reads zero).
#   2. ``git rev-list --count HEAD..origin/main`` to measure divergence.
#   3. zero  → ``already_current`` (no merge, no push).
#   4. >zero → ``git merge origin/main --no-edit``; on a clean merge push the
#      merged ref via ``execute_push`` (the PR already exists), then dispatch.
#   5. on conflict → block the task (interim Phase 1 behaviour) with a message
#      naming the conflicting files; consume no pr-fix round; do not dispatch
#      the agent; leave the worktree clean (``git merge --abort``).
#   6. on fetch/push error → block the task; Retry re-runs the sync.
#
# These tests fail against pre-fix code: the helper-level tests raise
# ``AttributeError`` (``_sync_branch_to_main`` does not exist yet), and the
# funnel-level tests observe the OLD behaviour (no push for a behind branch;
# the agent dispatched instead of the task blocking on a conflict / error).
#
# The git scaffolding is real (house convention — git is never mocked); only
# the authenticated push (``execute_push``) is replaced by a recorder so the
# tests need no GitHub remote or credentials.
# ---------------------------------------------------------------------------


class _RecordingHangRunner:
    """Agent runner that records each dispatch then hangs forever.

    Hanging keeps a dispatched pr-fix agent ``in_flight`` (status=working)
    without completing, so a test can distinguish "agent was dispatched"
    from "task was blocked before dispatch". ``shutdown()`` cancels it.
    """

    def __init__(self) -> None:
        self.calls: list[dict] = []
        self._gate = asyncio.Event()  # never set

    def dispatch_shape_prompt(self) -> str:
        return ""

    async def run(self, system_prompt, user_prompt, work_dir, **kwargs) -> AgentResult:
        self.calls.append({"work_dir": work_dir, "system_prompt": system_prompt, "user_prompt": user_prompt, **kwargs})
        await self._gate.wait()
        return AgentResult(success=True, stdout="", stderr="", return_code=0, duration_ms=1)


class _PushRecorder:
    """Stand-in for ``lotsa.push_step.execute_push``.

    Records each call so a test can assert the merged ref WAS (behind) or was
    NOT (already-current / conflict) pushed, and optionally raises to simulate
    a push failure. Signature matches ``execute_push``.
    """

    def __init__(self, exc: Exception | None = None) -> None:
        self.calls: list[dict] = []
        self._exc = exc

    async def __call__(self, work_dir, task_id, pr_number, base_branch, title=None, body=None):
        self.calls.append(
            {"work_dir": work_dir, "task_id": task_id, "pr_number": pr_number, "base_branch": base_branch}
        )
        if self._exc is not None:
            raise self._exc
        return (pr_number or 1, "https://github.com/o/r/pull/1", "o", "r")


def _patch_execute_push(monkeypatch, recorder: _PushRecorder) -> None:
    """Patch ``execute_push`` at both plausible bind sites.

    ``_execute_push`` imports ``execute_push`` function-locally from
    ``lotsa.push_step`` (orchestrator.py:2735); ``_sync_branch_to_main`` is
    expected to reuse the same helper the same way, so patching the source
    module intercepts it. The orchestrator-namespace patch (``raising=False``)
    additionally covers a module-level ``from lotsa.push_step import
    execute_push`` import, should the implementer choose that form.
    """
    import lotsa.orchestrator as orch
    import lotsa.push_step as push_step

    monkeypatch.setattr(push_step, "execute_push", recorder)
    monkeypatch.setattr(orch, "execute_push", recorder, raising=False)


def _setup_sync_worktree(tmp_path, task_id: str, scenario: str):
    """Build a real ``origin`` + task worktree and return the worktree path.

    ``scenario``:
      - ``"current"``  — branch is level with origin/main.
      - ``"behind"``   — origin/main has one commit the branch lacks
                         (a non-conflicting change to ``file.txt``).
      - ``"conflict"`` — origin/main and the branch edited the same line of
                         ``shared.txt`` (auto-merge conflicts).

    The worktree is cloned BEFORE origin/main is advanced, so its
    ``origin/main`` remote-tracking ref is stale until fetched — this is what
    makes the unconditional ``git fetch`` load-bearing.
    """
    import subprocess

    base = tmp_path / "git" / task_id
    base.mkdir(parents=True, exist_ok=True)
    origin = base / "origin.git"
    seed = base / "seed"
    wt = base / "wt"

    def git(args, cwd):
        subprocess.run(["git", *args], cwd=str(cwd), capture_output=True, check=True)

    subprocess.run(["git", "init", "--bare", str(origin)], capture_output=True, check=True)
    # Portable default-branch=main without relying on ``init -b`` flag support.
    subprocess.run(
        ["git", "-C", str(origin), "symbolic-ref", "HEAD", "refs/heads/main"], capture_output=True, check=True
    )

    subprocess.run(["git", "clone", str(origin), str(seed)], capture_output=True, check=True)
    git(["config", "user.email", "t@t.com"], seed)
    git(["config", "user.name", "T"], seed)
    (seed / "file.txt").write_text("base\n")
    (seed / "shared.txt").write_text("alpha\nbeta\ngamma\n")
    git(["add", "."], seed)
    git(["commit", "-m", "init"], seed)
    git(["push", "origin", "main"], seed)

    subprocess.run(["git", "clone", str(origin), str(wt)], capture_output=True, check=True)
    git(["config", "user.email", "t@t.com"], wt)
    git(["config", "user.name", "T"], wt)
    git(["checkout", "-b", f"lotsa/{task_id}"], wt)

    if scenario in ("behind", "conflict"):
        if scenario == "conflict":
            (seed / "shared.txt").write_text("alpha\nMAIN\ngamma\n")
        else:
            (seed / "file.txt").write_text("base\nmain-change\n")
        git(["add", "."], seed)
        git(["commit", "-m", "advance main"], seed)
        git(["push", "origin", "main"], seed)

    if scenario == "conflict":
        (wt / "shared.txt").write_text("alpha\nBRANCH\ngamma\n")
        git(["add", "."], wt)
        git(["commit", "-m", "branch edit"], wt)

    return wt


def _git_porcelain(wt) -> str:
    import subprocess

    return subprocess.run(
        ["git", "-C", str(wt), "status", "--porcelain"], capture_output=True, text=True, check=True
    ).stdout


@pytest.fixture()
def full_service(tmp_path, _loop, run):
    """A started OrchestratorService on the bundled ``build`` process.

    ``build`` (ADR-043, the Execute-at-full-depth process that replaced ``full``)
    carries the pr_fix sub-flow (and thus the ``pr-fix`` step and the
    ``wait_for_pr_signal`` monitor state) that the sync precedes. ``push_pr``
    (the action tool) is stubbed so nothing reaches the real ``execute_push``
    via the action path; the autouse ``_isolated_registry`` fixture restores
    the built-in afterwards. The runner records + hangs so a dispatched
    pr-fix agent is observable without completing.
    """
    from lotsa import registry as reg
    from lotsa.registry import register_tool
    from lotsa.tools import ToolResult

    reg._TOOLS.pop("push_pr", None)

    async def _stub_push_pr(ctx, config):
        return ToolResult(success=True, output="stub", metadata={})

    register_tool("push_pr", _stub_push_pr)

    (tmp_path / "data").mkdir()
    config = LotsaConfig(
        data_dir=tmp_path / "data",
        work_dir=tmp_path,
        flow="build",
        model="sonnet",
        budget=5.0,
    )
    db = TaskDB(tmp_path / "data" / "lotsa.db")
    run(db.initialize())

    svc = OrchestratorService(config, db)
    svc.runner = _RecordingHangRunner()
    run(svc.start())
    yield svc
    run(svc.shutdown())
    run(db.close())


def _stage_waiting_pr_task(svc, run, *, pr_number: int):
    """Stage a task at ``(state=wait_for_pr_signal, status=waiting_for_pr)``.

    Seeds the spec/plan artifacts pr-fix declares as inputs so that — pre-fix
    — the dispatch proceeds all the way to the agent (rather than blocking on
    the missing-artifact check, which would otherwise make a conflict/error
    test pass for the WRONG reason).
    """
    task = run(svc.db.create_task("Sync test", state="wait_for_pr_signal", metadata={"pr_number": pr_number}))
    run(svc.db.add_message(task.id, "agent", "spec", "spec content", "artifact", metadata={"artifact_name": "spec"}))
    run(svc.db.add_message(task.id, "agent", "plan", "plan content", "artifact", metadata={"artifact_name": "plan"}))
    run(
        svc.db.claim_task_transition(
            task.id,
            from_status=task.status,
            from_state=task.state,
            to_state="wait_for_pr_signal",
            to_status="waiting_for_pr",
            to_current_step="wait_for_pr_signal",
        )
    )
    return task


class TestSyncBranchToMainHelper:
    """Unit coverage for ``OrchestratorService._sync_branch_to_main`` (ADR-015).

    Each test fails pre-fix with ``AttributeError`` — the helper does not yet
    exist — which is the intended "red": the spec (the helper and its
    ``SyncResult`` contract) is unimplemented.
    """

    def test_already_current_returns_status_and_skips_push(self, full_service, tmp_path, run, monkeypatch):
        """A level branch reports ``already_current`` and performs no merge/push."""
        svc = full_service
        task = run(svc.db.create_task("current", state="wait_for_pr_signal", metadata={"pr_number": 5}))
        wt = _setup_sync_worktree(tmp_path, task.id, "current")
        svc._worktree_managers["default"].get_path = lambda _tid, _wt=wt: _wt
        rec = _PushRecorder()
        _patch_execute_push(monkeypatch, rec)

        result = run(svc._sync_branch_to_main(task.id))

        assert result.status == "already_current"
        assert rec.calls == [], "an already-current branch must not push"
        assert (wt / "file.txt").read_text() == "base\n", "no merge should have touched the worktree"

    def test_behind_main_merges_and_pushes(self, full_service, tmp_path, run, monkeypatch):
        """A behind branch fetches, auto-merges origin/main, and pushes once.

        The worktree's ``origin/main`` ref is deliberately stale at setup
        time, so a merge that brings in ``main-change`` proves the
        unconditional fetch ran first.
        """
        svc = full_service
        task = run(svc.db.create_task("behind", state="wait_for_pr_signal", metadata={"pr_number": 7}))
        wt = _setup_sync_worktree(tmp_path, task.id, "behind")
        svc._worktree_managers["default"].get_path = lambda _tid, _wt=wt: _wt
        rec = _PushRecorder()
        _patch_execute_push(monkeypatch, rec)

        result = run(svc._sync_branch_to_main(task.id))

        assert result.status == "clean"
        assert "main-change" in (wt / "file.txt").read_text(), "origin/main must be merged into the worktree"
        assert len(rec.calls) == 1, "the merged ref must be pushed exactly once"
        assert rec.calls[0]["pr_number"] == 7, "the push must target the task's existing PR"

    def test_merge_conflict_returns_conflicts_leaves_markers(self, full_service, tmp_path, run, monkeypatch):
        """A conflicting auto-merge reports ``conflicts`` with the file list,
        leaves conflict markers in the worktree (Phase 2 — no abort), and does not push."""
        svc = full_service
        task = run(svc.db.create_task("conflict", state="wait_for_pr_signal", metadata={"pr_number": 11}))
        wt = _setup_sync_worktree(tmp_path, task.id, "conflict")
        svc._worktree_managers["default"].get_path = lambda _tid, _wt=wt: _wt
        rec = _PushRecorder()
        _patch_execute_push(monkeypatch, rec)

        result = run(svc._sync_branch_to_main(task.id))

        assert result.status == "conflicts"
        assert "shared.txt" in result.conflicting_files, f"conflicting files not named: {result.conflicting_files!r}"
        assert rec.calls == [], "a conflicted sync must not push"
        content = (wt / "shared.txt").read_text()
        assert "<<<<<<<" in content, "conflict markers must remain in the worktree (no merge --abort)"

    def test_push_error_propagates(self, full_service, tmp_path, run, monkeypatch):
        """A ``PushError`` from the push surfaces as an exception (no swallow).

        Pre-fix the ``_sync_branch_to_main`` attribute access raises
        ``AttributeError``, which escapes ``pytest.raises(PushError)`` and so
        still fails the test — the intended red.
        """
        from lotsa.push_step import PushError

        svc = full_service
        task = run(svc.db.create_task("pusherr", state="wait_for_pr_signal", metadata={"pr_number": 3}))
        wt = _setup_sync_worktree(tmp_path, task.id, "behind")
        svc._worktree_managers["default"].get_path = lambda _tid, _wt=wt: _wt
        rec = _PushRecorder(exc=PushError("push rejected"))
        _patch_execute_push(monkeypatch, rec)

        with pytest.raises(PushError):
            run(svc._sync_branch_to_main(task.id))


class TestSyncBranchToMainDispatch:
    """The sync is wired into the pr-fix dispatch funnel and the retry path.

    These tests drive the real entry points (``dispatch_pr_fix`` / ``retry``)
    and assert the resulting task state, so they fail against pre-fix code by
    observing the OLD behaviour (no push for a behind branch; the agent
    dispatched and a round consumed instead of the task blocking).
    """

    def test_behind_main_pushes_then_dispatches_pr_fix(self, full_service, tmp_path, run, monkeypatch):
        """dispatch_pr_fix on a behind task merges+pushes, then dispatches pr-fix.

        Pre-fix: no sync runs, so ``execute_push`` is never called — the
        ``len(rec.calls) == 1`` assertion fails.
        """
        svc = full_service
        task = _stage_waiting_pr_task(svc, run, pr_number=21)
        wt = _setup_sync_worktree(tmp_path, task.id, "behind")
        svc._worktree_managers["default"].get_path = lambda _tid, _wt=wt: _wt
        rec = _PushRecorder()
        _patch_execute_push(monkeypatch, rec)

        dispatched = run(svc.dispatch_pr_fix(task.id, "address the review"))
        run(asyncio.sleep(0.1))

        assert dispatched is True
        assert len(rec.calls) == 1, "a behind branch must be merged and pushed before pr-fix dispatch"
        assert "main-change" in (wt / "file.txt").read_text(), "origin/main must be merged into the worktree"
        row = run(svc.db.get_task(task.id))
        assert row.status == "working" and row.state == "pr-fixing"
        assert len(svc.runner.calls) == 1, "pr-fix agent must be dispatched after the sync"

    def test_merge_conflict_dispatches_resolve_conflicts(self, full_service, tmp_path, run, monkeypatch):
        """A conflict dispatches resolve_conflicts (Phase 2 behaviour), consumes
        one pr-fix round, and does not push.

        Phase 1 behaviour (blocking) is preserved for processes without a
        resolve_conflicts job — tested separately in
        test_adr015_phase2.py::test_conflict_blocks_when_process_has_no_resolve_conflicts_step.
        """
        svc = full_service
        task = _stage_waiting_pr_task(svc, run, pr_number=22)
        wt = _setup_sync_worktree(tmp_path, task.id, "conflict")
        svc._worktree_managers["default"].get_path = lambda _tid, _wt=wt: _wt
        rec = _PushRecorder()
        _patch_execute_push(monkeypatch, rec)

        run(svc.dispatch_pr_fix(task.id, "address the review"))
        run(asyncio.sleep(0.1))

        row = run(svc.db.get_task(task.id))
        assert row.status == "working", f"Phase 2 conflict must dispatch resolve_conflicts, got status={row.status!r}"
        assert row.state == "resolving_conflicts"
        assert int(row.metadata.get("pr_fix_round_count", 0)) == 1, "conflict dispatch must consume one pr-fix round"
        assert len(svc.runner.calls) == 1, "resolve_conflicts agent must be dispatched on a conflict"
        assert rec.calls == [], "a conflicted sync must not push"

    def test_revise_conflict_dispatches_resolve_conflicts_and_feedback(self, full_service, tmp_path, run, monkeypatch):
        """An operator revise whose branch-sync conflicts dispatches
        resolve_conflicts (Phase 2) and still records the operator's feedback
        text before dispatching.

        ``revise()`` on a ``waiting_for_pr`` task hands its text to
        ``_dispatch_pr_fix_locked`` as ``user_feedback``, deliberately deferring
        the audit write to inside the locked dispatch. The feedback is written
        BEFORE the sync runs, so even when a conflict routes to resolve_conflicts,
        the operator's text is durably recorded.
        """
        svc = full_service
        task = _stage_waiting_pr_task(svc, run, pr_number=25)
        wt = _setup_sync_worktree(tmp_path, task.id, "conflict")
        svc._worktree_managers["default"].get_path = lambda _tid, _wt=wt: _wt
        rec = _PushRecorder()
        _patch_execute_push(monkeypatch, rec)

        run(svc.revise(task.id, "please also address the security comment"))
        run(asyncio.sleep(0.1))

        row = run(svc.db.get_task(task.id))
        assert row.status == "working", f"Phase 2 conflict must dispatch resolve_conflicts, got status={row.status!r}"
        assert row.state == "resolving_conflicts"
        assert len(svc.runner.calls) == 1, "resolve_conflicts agent must be dispatched"

        feedback = run(svc.db.get_messages(task.id, msg_type="feedback"))
        joined = " ".join(m.content for m in feedback)
        assert "security comment" in joined, (
            f"operator revise text must be persisted even when conflict routes to resolve_conflicts; got: {joined!r}"
        )

    def test_fetch_error_blocks_task(self, full_service, tmp_path, run, monkeypatch):
        """A fetch failure blocks the task (no bespoke retry), leaving the
        agent undispatched.

        Pre-fix: no sync runs, the broken remote is never touched, and the
        dispatch proceeds to the agent — ``status`` is ``working``, not
        ``blocked``.
        """
        import subprocess

        svc = full_service
        task = _stage_waiting_pr_task(svc, run, pr_number=23)
        wt = _setup_sync_worktree(tmp_path, task.id, "behind")
        # Break the origin remote so ``git fetch origin main`` fails.
        subprocess.run(
            ["git", "-C", str(wt), "remote", "set-url", "origin", str(tmp_path / "nonexistent.git")],
            capture_output=True,
            check=True,
        )
        svc._worktree_managers["default"].get_path = lambda _tid, _wt=wt: _wt
        rec = _PushRecorder()
        _patch_execute_push(monkeypatch, rec)

        run(svc.dispatch_pr_fix(task.id, "address the review"))
        run(asyncio.sleep(0.1))

        row = run(svc.db.get_task(task.id))
        assert row.status == "blocked", f"a fetch error must block the task, got status={row.status!r}"
        assert svc.runner.calls == [], "the pr-fix agent must not be dispatched when the sync errors"

    def test_retry_blocked_pr_fix_reruns_sync(self, full_service, tmp_path, run, monkeypatch):
        """Retry of a blocked pr-fix task re-runs the sync before dispatching.

        Staged behind, so a re-run sync must merge+push (``execute_push``
        called). Pre-fix: ``retry()`` dispatches ``pr-fix`` directly via
        ``_dispatch_step`` without syncing, so ``execute_push`` is never
        called — the assertion fails.
        """
        svc = full_service
        task = run(
            svc.db.create_task(
                "retry sync",
                state="pr-fixing",
                metadata={"pr_number": 24, "current_flow": "pr_fix"},
            )
        )
        run(svc.db.add_message(task.id, "agent", "spec", "spec", "artifact", metadata={"artifact_name": "spec"}))
        run(svc.db.add_message(task.id, "agent", "plan", "plan", "artifact", metadata={"artifact_name": "plan"}))
        run(
            svc.db.claim_task_transition(
                task.id,
                from_status=task.status,
                from_state=task.state,
                to_state="pr-fixing",
                to_status="blocked",
                to_current_step="pr-fix",
            )
        )
        wt = _setup_sync_worktree(tmp_path, task.id, "behind")
        svc._worktree_managers["default"].get_path = lambda _tid, _wt=wt: _wt
        rec = _PushRecorder()
        _patch_execute_push(monkeypatch, rec)

        run(svc.retry(task.id))
        run(asyncio.sleep(0.1))

        assert len(rec.calls) == 1, "retry must re-run the sync (which merges+pushes a behind branch) before pr-fix"
        assert "main-change" in (wt / "file.txt").read_text(), "retry's sync must merge origin/main into the worktree"

    def test_retry_blocked_pr_fix_forwards_gathered_feedback_to_dispatch(
        self, full_service, tmp_path, run, monkeypatch
    ):
        """``retry()`` of a blocked pr-fix task forwards the gathered PR feedback
        into the dispatch — it does not re-dispatch with ``feedback=None``.

        A retry carries no operator input, so ``retry()`` resolves the PR's
        current feedback via ``_gather_pending_pr_feedback(row)`` and passes it
        as ``_dispatch_step(..., feedback=...)``. Without it, the pr-fix agent
        re-runs with nothing to do, immediately re-skips, and (with the old skip
        accounting) re-blocks — the failure mode this fix closes (an internal task).

        Pre-fix red: reverting the injection to ``feedback=None`` (the change the
        existing isolation tests in ``TestGatherPendingPrFeedback`` and the
        drainer integration do NOT catch) leaves ``captured["feedback"] is None``
        and this assertion fails. Sync and dispatch are stubbed so the test
        isolates the forwarding contract from the sync/worktree machinery.
        """
        from lotsa.orchestrator import SyncResult

        svc = full_service
        sentinel = "### PR Comments\n\nMedium: tighten the guard at orchestrator.py:42"

        task = run(
            svc.db.create_task(
                "retry feedback forwarding",
                state="pr-fixing",
                metadata={"pr_number": 26, "current_flow": "pr_fix"},
            )
        )
        run(
            svc.db.claim_task_transition(
                task.id,
                from_status=task.status,
                from_state=task.state,
                to_state="pr-fixing",
                to_status="blocked",
                to_current_step="pr-fix",
            )
        )

        async def _clean_sync(_task_id):
            return SyncResult(status="already_current")

        async def _stub_gather(_row):
            return sentinel

        captured: dict = {}

        async def _capture_dispatch(item, step, feedback=None, triggering_comment_ids=None):
            captured["feedback"] = feedback
            captured["step"] = step.name

        monkeypatch.setattr(svc, "_sync_branch_to_main", _clean_sync)
        monkeypatch.setattr(svc, "_gather_pending_pr_feedback", _stub_gather)
        monkeypatch.setattr(svc, "_dispatch_step", _capture_dispatch)

        run(svc.retry(task.id))

        assert captured.get("step") == "pr-fix", (
            f"retry() of a blocked pr-fix task must dispatch the pr-fix step, got {captured.get('step')!r}"
        )
        assert captured.get("feedback") == sentinel, (
            "retry() must forward the gathered PR feedback to _dispatch_step; "
            f"pre-fix this was feedback=None. Got {captured.get('feedback')!r}"
        )

    # ------------------------------------------------------------------
    # ADR-018 — pre-retry-from-blocked / pre-rebase-after-restart sync.
    #
    # Both unimplemented ADR-018 rows collapse to ``retry()``'s push-retry
    # branch: a task blocked after a push failure, and a push-state task
    # flipped to ``blocked`` by restart recovery (which preserves
    # ``state="pushing"``), both route through the ``push_retry`` predicate.
    # These tests drive the real ``retry()`` entry point with a real,
    # behind/conflicting worktree so they fail against pre-fix code by
    # observing the OLD behaviour: the push branch re-pushes WITHOUT first
    # syncing (no merge of origin/main, no conflict handling, no block on a
    # fetch error). ``_execute_push`` is stubbed so the legacy push step
    # never reaches a real remote; the sync's own push goes through the
    # ``execute_push`` recorder.
    # ------------------------------------------------------------------

    def _stage_push_blocked_task(self, svc, run, *, pr_number, current_step, metadata_extra=None):
        """Create a push-blocked task in the shape restart recovery leaves it.

        ``state="pushing"``, ``status="blocked"``. ``current_step`` is
        ``"push"`` for a task that crashed mid-``_execute_push`` and
        ``"pushing"`` for the restart-recovery shape (``_set_status`` writes
        ``current_step = row.current_step or row.state`` when the prior step
        was unset). Both must funnel into the synced push-retry branch.

        ``metadata_extra`` merges additional metadata (e.g. a pre-accrued
        ``pr_fix_round_count``) onto the base ``{"pr_number": ...}``.
        """
        metadata = {"pr_number": pr_number}
        if metadata_extra:
            metadata.update(metadata_extra)
        return run(
            svc.db.create_task(
                "retry push sync",
                state="pushing",
                status="blocked",
                current_step=current_step,
                metadata=metadata,
            )
        )

    def test_retry_push_behind_syncs_before_repush(self, full_service, tmp_path, run, monkeypatch):
        """A push-blocked task behind origin/main syncs (merge+push) before re-pushing.

        Clean path: the sync merges origin/main into the worktree and pushes
        the merged ref (recorder called once), then the push branch proceeds to
        ``_execute_push`` with ``item.state == "pushing"``.

        Pre-fix red: ``retry()``'s push branch dispatches ``_execute_push``
        directly with no sync — the worktree is never merged (``file.txt`` lacks
        ``"main-change"``) and the ``execute_push`` recorder is never called.
        """
        from unittest.mock import AsyncMock

        svc = full_service
        task = self._stage_push_blocked_task(svc, run, pr_number=31, current_step="push")
        wt = _setup_sync_worktree(tmp_path, task.id, "behind")
        svc._worktree_managers["default"].get_path = lambda _tid, _wt=wt: _wt
        rec = _PushRecorder()
        _patch_execute_push(monkeypatch, rec)

        # Stub the legacy push step so it never reaches a real remote; capture
        # the state it was dispatched with to prove the clean path re-pushes.
        pushed: dict[str, object] = {}

        async def fake_execute_push(item):
            pushed["called"] = True
            pushed["state"] = item.state

        monkeypatch.setattr(svc, "_execute_push", AsyncMock(side_effect=fake_execute_push))

        run(svc.retry(task.id))
        run(asyncio.sleep(0.1))

        assert "main-change" in (wt / "file.txt").read_text(), (
            "a behind push-retry must merge origin/main into the worktree before re-pushing"
        )
        assert len(rec.calls) == 1, "the sync must push the merged ref exactly once"
        assert pushed.get("called"), "the push branch must still re-push after a clean sync"
        assert pushed.get("state") == "pushing", "re-push must go through the pushing-state path"

    def test_retry_push_conflict_dispatches_resolve_conflicts(self, full_service, tmp_path, run, monkeypatch):
        """A push-retry whose sync conflicts dispatches resolve_conflicts (no push).

        The ``(pushing, resolving_conflicts)`` transition is not in any state
        machine, so the push branch must re-anchor the row at ``pr-fixing``
        (the pr_fix sub-flow's entry state) before delegating to
        ``_handle_conflict_dispatch`` — mirroring the pr-fix retry conflict
        path. The task lands at ``resolving_conflicts`` with the agent
        dispatched and one pr-fix round consumed.

        Pre-fix red: no sync runs, so the conflict is never detected — the
        task re-pushes (``_execute_push`` stub) and never reaches
        ``resolving_conflicts``.
        """
        from unittest.mock import AsyncMock

        svc = full_service
        task = self._stage_push_blocked_task(svc, run, pr_number=32, current_step="push")
        wt = _setup_sync_worktree(tmp_path, task.id, "conflict")
        svc._worktree_managers["default"].get_path = lambda _tid, _wt=wt: _wt
        rec = _PushRecorder()
        _patch_execute_push(monkeypatch, rec)
        monkeypatch.setattr(svc, "_execute_push", AsyncMock())

        run(svc.retry(task.id))
        run(asyncio.sleep(0.1))

        row = run(svc.db.get_task(task.id))
        assert row.status == "working", (
            f"a conflicted push-retry must dispatch resolve_conflicts, got status={row.status!r}"
        )
        assert row.state == "resolving_conflicts", f"expected resolving_conflicts, got state={row.state!r}"
        assert len(svc.runner.calls) == 1, "the resolve_conflicts agent must be dispatched on a conflict"
        assert int(row.metadata.get("pr_fix_round_count", 0)) == 1, "conflict dispatch must consume one pr-fix round"
        assert rec.calls == [], "a conflicted sync must not push"

    def test_retry_push_conflict_preserves_accrued_round_count(self, full_service, tmp_path, run, monkeypatch):
        """A conflicted push-retry advances the accrued pr-fix round count, not resets it.

        The conflict re-anchor dispatches into the pr_fix sub-flow via
        ``_handle_conflict_dispatch``, which writes ``current_rounds + 1`` to
        ``pr_fix_round_count``. The push branch must pass the task's *accrued*
        count so the ``max_pr_fix_rounds`` budget keeps shrinking across a
        push → pr-fix → conflict lifecycle — not reset it to 1.

        Pre-fix red: the push branch passed a hardcoded ``0``, so a task that
        had already burned 5 pr-fix rounds lands back at ``pr_fix_round_count=1``
        instead of ``6`` — silently re-granting the round budget.
        """
        from unittest.mock import AsyncMock

        svc = full_service
        task = self._stage_push_blocked_task(
            svc, run, pr_number=35, current_step="push", metadata_extra={"pr_fix_round_count": 5}
        )
        wt = _setup_sync_worktree(tmp_path, task.id, "conflict")
        svc._worktree_managers["default"].get_path = lambda _tid, _wt=wt: _wt
        rec = _PushRecorder()
        _patch_execute_push(monkeypatch, rec)
        monkeypatch.setattr(svc, "_execute_push", AsyncMock())

        run(svc.retry(task.id))
        run(asyncio.sleep(0.1))

        row = run(svc.db.get_task(task.id))
        assert int(row.metadata.get("pr_fix_round_count", 0)) == 6, (
            "conflict dispatch must advance the accrued round count (5 → 6), not reset it to 1; "
            f"got {row.metadata.get('pr_fix_round_count')!r}"
        )

    def test_retry_push_fetch_error_blocks_and_stays_push_routable(self, full_service, tmp_path, run, monkeypatch):
        """A fetch failure during the push-retry sync blocks the task, leaving it
        re-routable into the push funnel.

        The broken remote makes ``git fetch`` fail; the push branch blocks the
        task via ``_block_after_sync`` but keeps ``current_step="push"`` (the
        parameterized landing) so a subsequent Retry re-enters the synced push
        branch rather than the generic pr-fix path.

        Pre-fix red: no sync runs, the broken remote is never touched, and the
        push branch re-pushes (``_execute_push`` stub) — ``status`` stays
        ``working``, not ``blocked``.
        """
        import subprocess
        from unittest.mock import AsyncMock

        svc = full_service
        task = self._stage_push_blocked_task(svc, run, pr_number=33, current_step="push")
        wt = _setup_sync_worktree(tmp_path, task.id, "behind")
        # Break the origin remote so ``git fetch origin <default_branch>`` fails.
        subprocess.run(
            ["git", "-C", str(wt), "remote", "set-url", "origin", str(tmp_path / "nonexistent.git")],
            capture_output=True,
            check=True,
        )
        svc._worktree_managers["default"].get_path = lambda _tid, _wt=wt: _wt
        rec = _PushRecorder()
        _patch_execute_push(monkeypatch, rec)
        monkeypatch.setattr(svc, "_execute_push", AsyncMock())

        run(svc.retry(task.id))
        run(asyncio.sleep(0.1))

        row = run(svc.db.get_task(task.id))
        assert row.status == "blocked", (
            f"a fetch error during push-retry sync must block the task, got status={row.status!r}"
        )
        assert row.current_step == "push", (
            f"the block must keep current_step='push' so retry re-enters the push funnel, got {row.current_step!r}"
        )
        assert svc.runner.calls == [], "no agent must be dispatched when the sync errors"

    def test_retry_restart_recovered_push_state_syncs(self, full_service, tmp_path, run, monkeypatch):
        """A push-state task flipped to blocked by restart recovery syncs on retry.

        Restart recovery flips a ``state="pushing"`` task to ``status="blocked"``
        and writes ``current_step="pushing"`` (``row.current_step or row.state``
        with the step unset). That shape must funnel into the SAME synced
        push-retry branch as a ``current_step="push"`` block — verified by the
        worktree being merged before re-push.

        Pre-fix red: the restart-recovered shape re-pushes without syncing — the
        worktree is never merged.
        """
        from unittest.mock import AsyncMock

        svc = full_service
        # current_step="pushing" is the value restart recovery writes when the
        # prior step was unset (distinct from the "push" sentinel test above).
        task = self._stage_push_blocked_task(svc, run, pr_number=34, current_step="pushing")
        wt = _setup_sync_worktree(tmp_path, task.id, "behind")
        svc._worktree_managers["default"].get_path = lambda _tid, _wt=wt: _wt
        rec = _PushRecorder()
        _patch_execute_push(monkeypatch, rec)
        monkeypatch.setattr(svc, "_execute_push", AsyncMock())

        run(svc.retry(task.id))
        run(asyncio.sleep(0.1))

        assert "main-change" in (wt / "file.txt").read_text(), (
            "a restart-recovered push-state task must sync (merge origin/main) on retry"
        )
        assert len(rec.calls) == 1, "the sync must push the merged ref once for the restart-recovered shape too"


class TestPerStepModelSelection:
    """ADR-022 — the orchestrator resolves ``step.model or self.config.model``
    and threads it through ``runner.run`` and both ``agent_model`` audit-row
    write sites.
    """

    def _retarget(self, service, body: str, prompts_dir=None):
        """Swap the service's active process for a custom single-/multi-step one."""
        from lotsa.flows import build_process

        flow_yaml = service.config.work_dir / "model_flow.yaml"
        flow_yaml.write_text(body)
        proc = build_process("custom", process_file=flow_yaml, prompts_dir=prompts_dir)
        service.process = proc
        service._processes[service._active_process_name] = proc
        service.flow = proc.flows.get("main") or next(iter(proc.flows.values()))
        return proc

    def test_agent_job_model_overrides_global_at_dispatch(self, service, run):
        """A job with ``model: opus`` dispatches with ``opus`` even though
        ``config.model`` is ``sonnet``."""
        self._retarget(
            service,
            "name: model-test\njobs:\n  - name: coding\n    model: opus\n    evaluate: true\n",
        )
        service.runner = FakeRunner()
        run(service.create_task("Model override dispatch"))
        run(asyncio.sleep(0.3))

        assert service.runner.calls, "agent was never dispatched"
        assert service.runner.calls[0]["model"] == "opus"

    def test_agent_job_model_recorded_in_output_metadata(self, service, run):
        """The resolved model (after override) is recorded in
        ``output_meta['agent_model']`` — not the global default."""
        self._retarget(
            service,
            "name: model-test\njobs:\n  - name: coding\n    model: opus\n    evaluate: true\n",
        )
        service.runner = FakeRunner()
        task = run(service.create_task("Model override metadata"))
        run(asyncio.sleep(0.3))

        messages = run(service.db.get_messages(task.id, msg_type="output"))
        assert messages, "no output message persisted"
        assert messages[-1].metadata.get("agent_model") == "opus"

    def test_job_without_model_falls_back_to_config_model_at_dispatch(self, service, run):
        """A job with no ``model:`` dispatches with ``config.model`` (sonnet).

        The default ``service`` fixture flow declares a bare ``coding`` job, so
        this exercises the fallback branch of ``step.model or self.config.model``.
        """
        run(service.create_task("Model fallback dispatch"))
        run(asyncio.sleep(0.3))

        assert service.runner.calls, "agent was never dispatched"
        assert service.runner.calls[0]["model"] == "sonnet"

    def test_conversational_step_records_resolved_model_in_chat_meta(self, service, run):
        """The chat/drainer path (``_completion_drainer``) records the resolved
        model in ``chat_meta['agent_model']``. The adjacent ``chat_meta['model']``
        — the runner-reported actual model ID — is a different field and stays
        whatever the runner reported."""
        prompts_dir = service.config.work_dir / "chat_prompts"
        prompts_dir.mkdir(exist_ok=True)
        for name in ("spec-system", "spec-user"):
            (prompts_dir / f"{name}.md").write_text(f"# {name}\n{{title}}\n{{body}}")
        self._retarget(
            service,
            "name: chat-model-test\n"
            "jobs:\n"
            "  - name: spec\n"
            "    prompt: spec\n"
            "    conversational: true\n"
            "    output: spec\n"
            "    model: opus\n"
            "    queue_state: speccing\n"
            "    active_state: spec\n"
            "    rules:\n"
            "      - source: stdout\n        pattern: '^SPEC_COMPLETE:'\n        target: next\n",
            prompts_dir=prompts_dir,
        )
        service.runner = FakeRunner(
            AgentResult(
                success=True,
                stdout="Here is the spec\nSPEC_COMPLETE: Build X",
                stderr="",
                return_code=0,
                duration_ms=100,
                model="claude-actual-runner-id",
            )
        )
        task = run(service.create_task("Chat model metadata"))
        run(asyncio.sleep(0.3))

        chat_messages = run(service.db.get_messages(task.id, msg_type="chat"))
        agent_chats = [m for m in chat_messages if m.role == "agent"]
        assert agent_chats, "no agent chat message persisted"
        # Resolved (configured-alias) model — the field ADR-022 changes.
        assert agent_chats[-1].metadata.get("agent_model") == "opus"
        # Runner-reported actual model ID — a different field, out of scope.
        assert agent_chats[-1].metadata.get("model") == "claude-actual-runner-id"


class TestBenignSkipDrainerIntegration:
    """Drainer-level integration for the benign-skip guard (an internal task).

    Unlike TestFeedbackIsActionable (which unit-tests the helper in
    isolation), these drive a real pr-fix dispatch → drainer cycle on the
    bundled ``full`` process, so a regression that bypassed
    ``_feedback_is_actionable`` at its drainer call site would be caught.
    A clean ("current") sync worktree makes dispatch_pr_fix proceed straight
    to the pr-fix agent (no resolve_conflicts detour).
    """

    def _arm(self, svc, tmp_path, run, monkeypatch, *, pr_number: int, skipped_stdout: str):
        svc.runner = FakeRunner(
            AgentResult(success=True, stdout=skipped_stdout, stderr="", return_code=0, duration_ms=10)
        )
        task = _stage_waiting_pr_task(svc, run, pr_number=pr_number)
        wt = _setup_sync_worktree(tmp_path, task.id, "current")
        svc._worktree_managers["default"].get_path = lambda _tid, _wt=wt: _wt

        async def _create(task_id, _wt=wt):
            return _wt

        svc._worktree_managers["default"].create = _create
        _patch_execute_push(monkeypatch, _PushRecorder())
        return task

    def test_benign_skip_empty_feedback_does_not_increment_or_block(self, full_service, tmp_path, run, monkeypatch):
        svc = full_service
        task = self._arm(
            svc, tmp_path, run, monkeypatch, pr_number=88, skipped_stdout="PR_FIX_SKIPPED: review still in progress\n"
        )
        # Empty feedback (e.g. an empty retry / in-progress review) → benign.
        run(svc.dispatch_pr_fix(task.id, ""))
        run(asyncio.sleep(0.6))
        row = run(svc.db.get_task(task.id))
        assert row.metadata.get("pr_fix_consecutive_skipped", 0) == 0, (
            "a PR_FIX_SKIPPED with no feedback delivered must not increment the cap counter at the drainer"
        )
        assert row.status != "blocked", "a benign skip must never trip max_consecutive_skipped"

    def test_actionable_skip_with_feedback_increments(self, full_service, tmp_path, run, monkeypatch):
        svc = full_service
        task = self._arm(
            svc, tmp_path, run, monkeypatch, pr_number=89, skipped_stdout="PR_FIX_SKIPPED: reviewer approved\n"
        )
        # Real feedback delivered but the agent skipped → counts (the cap's
        # actual target: skipping despite real feedback).
        run(svc.dispatch_pr_fix(task.id, "### PR Comments\n\nReviewer: please rename X to Y"))
        run(asyncio.sleep(0.6))
        row = run(svc.db.get_task(task.id))
        assert row.metadata.get("pr_fix_consecutive_skipped", 0) == 1, (
            "a PR_FIX_SKIPPED against actionable feedback must increment the cap counter at the drainer"
        )


# ===========================================================================
# ADR-030 — PR-lifetime monitoring (orchestrator-side behaviour)
#
# An opened PR is watched until terminal regardless of the task's local
# status. Coverage:
#   * discovery widens (``list_waiting_pr_tasks`` returns every non-terminal
#     PR-bearing task, carrying ``status``);
#   * terminal completion works from a parked status whose ``state`` has no
#     registered terminal edge (the incident shape: ``state="blocked"``);
#   * the drainer applies a deferred terminal after the agent's own routing.
#
# Each "red" test fails against pre-fix code for the reason in its docstring.
# ===========================================================================


class TestPrLifetimeMonitoringDiscovery:
    def test_list_waiting_pr_tasks_widens_to_all_non_terminal_pr_tasks(self, full_service, run):
        """``list_waiting_pr_tasks`` must return every non-terminal task that has
        a ``pr_number``, carrying ``status`` — not just ``waiting_for_pr`` rows.

        Pre-fix: the query is ``status="waiting_for_pr"`` and the returned dicts
        omit ``status``. So the ``blocked``/``needs_input`` PR tasks are missing
        (the zombie shape) and the ``status`` key is absent — both assertions
        fail.
        """
        svc = full_service
        waiting = run(
            svc.db.create_task(
                "waiting", state="wait_for_pr_signal", status="waiting_for_pr", metadata={"pr_number": 1}
            )
        )
        blocked = run(svc.db.create_task("blocked", state="blocked", status="blocked", metadata={"pr_number": 2}))
        needs = run(svc.db.create_task("needs", state="pr-fixing", status="needs_input", metadata={"pr_number": 3}))
        # Excluded: terminal status even though pr_number is set.
        run(svc.db.create_task("done", state="complete", status="complete", metadata={"pr_number": 4}))
        # Excluded: no pr_number.
        run(svc.db.create_task("nopr", state="coding", status="working", metadata={}))

        tasks = run(svc.list_waiting_pr_tasks())
        by_id = {t["id"]: t for t in tasks}

        assert set(by_id) == {waiting.id, blocked.id, needs.id}, (
            "discovery must include every non-terminal PR-bearing task and exclude terminal / pr_number-less tasks"
        )
        assert all("status" in t for t in tasks), "each returned dict must carry observed status"
        assert by_id[blocked.id]["status"] == "blocked"
        assert by_id[needs.id]["status"] == "needs_input"


class TestPrLifetimeMonitoringTerminalCompletion:
    def test_transition_task_completes_a_blocked_task(self, full_service, run):
        """A merged PR must complete a task parked at ``state="blocked"``.

        This is the PR #116 incident shape: the cap/operator block sets
        ``state="blocked"``, a SM sink with no ``(blocked, complete)`` edge.

        Pre-fix: ``transition_task`` rejects the missing edge and log-and-returns,
        leaving the task ``blocked`` forever (the zombie that needed a manual SQL
        flip). Post-fix terminal completion is not gated on the flow edge, so the
        task completes and the audit row names the prior status.
        """
        svc = full_service
        task = _stage_waiting_pr_task(svc, run, pr_number=116)
        # Drive into blocked via the real action — NOT a pre-flip — so the row
        # carries the genuine sink ``state="blocked"``.
        run(svc.block(task.id))
        blocked_row = run(svc.db.get_task(task.id))
        assert blocked_row.status == "blocked" and blocked_row.state == "blocked"
        assert ("blocked", "complete") not in svc.flow.state_machine.transitions, (
            "precondition: blocked is a SM sink — this is why the edge-gated path fails"
        )

        run(svc.transition_task(task.id, "complete"))

        fresh = run(svc.db.get_task(task.id))
        assert fresh.status == "complete", "a merged PR must complete a blocked task"
        status_changes = run(svc.db.get_messages(task.id, msg_type="status_change"))
        assert any("PR #" in m.content and "blocked" in m.content for m in status_changes), (
            "the completion audit row must name the parked status (Req 3)"
        )

    def test_transition_task_abandons_a_needs_input_task(self, full_service, run):
        """A closed PR must abandon a task parked at ``needs_input``.

        The NEEDS_DECISION park preserves ``state="pr-fixing"`` (the pr_fix
        active state), which has no ``(pr-fixing, abandoned)`` edge.

        Pre-fix: the missing edge → log-and-return → the task stays
        ``needs_input``. Post-fix it abandons.
        """
        svc = full_service
        task = run(
            svc.db.create_task(
                "needs-input PR task",
                state="pr-fixing",
                status="needs_input",
                metadata={"pr_number": 200, "current_flow": "pr_fix"},
            )
        )
        assert ("pr-fixing", "abandoned") not in svc.process.flows["pr_fix"].state_machine.transitions

        run(svc.transition_task(task.id, "abandoned"))

        fresh = run(svc.db.get_task(task.id))
        assert fresh.status == "abandoned", "a closed PR must abandon a needs_input task"
        status_changes = run(svc.db.get_messages(task.id, msg_type="status_change"))
        assert any("PR #" in m.content and "needs_input" in m.content for m in status_changes), (
            "the abandon audit row must name the parked status (Req 3)"
        )


class TestPrLifetimeMonitoringDeferredCompletion:
    def test_drainer_applies_deferred_terminal_after_routing(self, full_service, tmp_path, run):
        """A terminal deferred while ``working`` is applied by the drainer once
        the agent's own completion routing has run (Req 4).

        The monitor records ``terminal_pending`` on a ``working`` task instead
        of transitioning mid-dispatch; the drainer must consume it (via
        ``engine.take_terminal_pending``) after routing and complete the task.

        Here the engine's consume hook is stubbed to return ``"complete"`` for
        this task, isolating the drainer's apply step. Pre-fix the drainer never
        calls the hook, so the task lands wherever its routing left it
        (``waiting_for_pr``) and never completes — the assertion fails.
        """
        from lotsa.orchestrator import InFlightStep
        from rigg.models import AgentResult, Item

        svc = full_service
        task = run(
            svc.db.create_task(
                "deferred complete",
                state="pr-fixing",
                status="working",
                metadata={"current_flow": "pr_fix", "pr_number": 77},
            )
        )
        item = Item(
            id=task.id,
            state="pr-fixing",
            title="deferred complete",
            metadata={"current_flow": "pr_fix", "pr_number": 77},
        )
        pr_fix_step = next(j for j in svc.process.flows["pr_fix"].jobs if j.name == "pr-fix")

        # Stub the engine's consume hook so the drainer sees a pending terminal
        # for this task regardless of the monitor's in-memory tracking state.
        engine = svc._monitor_engine_for(item)
        assert engine is not None
        engine.take_terminal_pending = lambda task_id, _tid=task.id: "complete" if task_id == _tid else None

        info = InFlightStep(item=item, step=pr_fix_step, feedback=None, step_work_dir=tmp_path)
        info.agent_result = AgentResult(
            success=True, stdout="PR_FIX_SKIPPED: nothing to address", stderr="", return_code=0, duration_ms=1
        )
        svc._in_flight[item.id] = info
        svc._completions.put_nowait(info)

        for _ in range(40):
            run(asyncio.sleep(0.01))

        fresh = run(svc.db.get_task(task.id))
        assert fresh.status == "complete", (
            "the drainer must apply the deferred terminal after the agent's routing; "
            f"got status={fresh.status!r} state={fresh.state!r}"
        )


class TestAttachmentMaterialization:
    """Dispatch-time materialization of prompt attachments (Path A).

    At every agent dispatch the orchestrator copies the task's durable
    attachments into ``{work_dir}/.lotsa/attachments/``, git-excludes them, and
    appends a prompt block listing the relative paths. Written before the
    feature exists, so ``_run_agent`` injects nothing yet — the prompt-block
    assertions fail as the expected red.
    """

    @staticmethod
    def _seed_attachment(service, run, task_id, name="bug.png", data=b"PNGDATA"):
        """Place a durable attachment on disk + in metadata, as the upload
        endpoint would, without depending on that endpoint existing yet."""
        attach_dir = service.config.data_dir / "attachments" / "default" / task_id
        attach_dir.mkdir(parents=True, exist_ok=True)
        (attach_dir / name).write_bytes(data)
        row = run(service.db.get_task(task_id))
        meta = dict(row.metadata)
        meta.setdefault("attachments", [])
        meta["attachments"].append(
            {
                "filename": name,
                "rel_path": f".lotsa/attachments/{name}",
                "mime": "image/png",
                "size_bytes": len(data),
                "created_at": "2026-07-02T00:00:00+00:00",
            }
        )
        run(service.db.update_task(task_id, metadata=meta))

    def test_no_attachments_leaves_prompt_untouched(self, service, run):
        run(service.create_task("Plain task"))
        run(asyncio.sleep(0.2))
        assert service.runner.calls, "first step should have dispatched"
        assert ".lotsa/attachments" not in service.runner.calls[0]["user_prompt"]

    def test_attachment_copied_into_worktree_and_listed_in_prompt(self, service, run):
        run(service.create_task("Attach task"))
        run(asyncio.sleep(0.2))
        task_id = run(service.list_tasks_async())[0].id
        assert run(service.db.get_task(task_id)).status == "waiting"

        self._seed_attachment(service, run, task_id, data=b"PNGDATA")

        before = len(service.runner.calls)
        run(service.revise(task_id, "Please look at the screenshot"))
        run(asyncio.sleep(0.2))
        assert len(service.runner.calls) > before, "revise must re-dispatch the agent"

        call = service.runner.calls[-1]
        # The prompt block names the worktree-relative path for the Read tool.
        assert ".lotsa/attachments/bug.png" in call["user_prompt"]
        assert "Read" in call["user_prompt"]

        # The file was copied into the worktree the agent actually ran in.
        work_dir = Path(call["work_dir"])
        copied = work_dir / ".lotsa" / "attachments" / "bug.png"
        assert copied.exists()
        assert copied.read_bytes() == b"PNGDATA"
        # And the managed ignore is in place so it never gets committed.
        assert "*" in (work_dir / ".lotsa" / ".gitignore").read_text()

    def test_deferred_create_holds_first_dispatch_until_attachments_uploaded(self, service, run):
        """The create-then-upload flow must reach the FIRST agent step.

        Regression for the ordering bug: ``create_task`` used to dispatch the
        first step immediately, before the empty-state form could POST its
        files — so ``_run_agent`` read ``tasks.metadata`` with no attachment
        records and the first prompt silently missed them. With
        ``defer_dispatch=True`` the dispatch is held until ``dispatch_created``.

        Fails against the pre-fix code: without deferral the first step
        dispatches at create time, so ``runner.calls`` is non-empty right after
        create (the ``assert not service.runner.calls`` line) and that first
        prompt never lists the attachment (uploaded only afterwards).
        """
        task = run(service.create_task("Attach task", defer_dispatch=True))
        run(asyncio.sleep(0.2))
        # Deferred: nothing dispatched yet, so the operator's upload can land
        # before the first agent ever runs.
        assert not service.runner.calls, "deferred create must NOT dispatch the first step"

        # Upload happens now (as the endpoint would), THEN we release dispatch.
        self._seed_attachment(service, run, task.id, data=b"PNGDATA")
        run(service.dispatch_created(task.id))
        run(asyncio.sleep(0.2))

        assert len(service.runner.calls) == 1, "dispatch_created must run the first step exactly once"
        call = service.runner.calls[0]
        assert ".lotsa/attachments/bug.png" in call["user_prompt"]
        assert "Read" in call["user_prompt"]
        # The file reached the worktree the first step actually ran in.
        copied = Path(call["work_dir"]) / ".lotsa" / "attachments" / "bug.png"
        assert copied.read_bytes() == b"PNGDATA"

    def test_deferred_conversational_create_replays_message_on_dispatch(self, tmp_path, _loop, run):
        """A deferred conversational first step (the default ``chat`` new-task
        flow) still replays the operator's message as feedback on dispatch.

        ``dispatch_created`` recovers the original message from the chat log and
        passes it as ``feedback`` — matching what ``create_task`` would have done
        inline — so the held conversational step receives both the message and
        the attachment path when it finally runs.
        """
        data_dir = tmp_path / "tasks"
        data_dir.mkdir()
        flow_yaml = tmp_path / "chat_flow.yaml"
        # Use the ``coding`` prompt name (which resolves in the bundled prompts)
        # for a conversational first step — mirrors the ``chat`` process shape.
        flow_yaml.write_text("name: test\njobs:\n  - name: coding\n    type: agent\n    conversational: true\n")
        config = LotsaConfig(
            data_dir=data_dir,
            work_dir=data_dir.parent,
            flow="custom",
            flow_file=flow_yaml,
            model="sonnet",
            budget=5.0,
        )
        db = TaskDB(data_dir / "lotsa.db")
        run(db.initialize())
        svc = OrchestratorService(config, db)
        svc.runner = FakeRunner()
        run(svc.start())
        try:
            task = run(svc.create_task(message="Build from this mockup", defer_dispatch=True))
            run(asyncio.sleep(0.2))
            assert not svc.runner.calls, "deferred conversational create must not dispatch"

            self._seed_attachment(svc, run, task.id, data=b"PNGDATA")
            run(svc.dispatch_created(task.id))
            run(asyncio.sleep(0.2))

            assert len(svc.runner.calls) == 1
            prompt = svc.runner.calls[0]["user_prompt"]
            assert "Build from this mockup" in prompt, "the original message must be replayed as feedback"
            assert ".lotsa/attachments/bug.png" in prompt
        finally:
            run(svc.shutdown())
            run(db.close())

    def test_dispatch_created_refuses_second_call_on_active_task(self, tmp_path, _loop, run):
        """A repeat ``/dispatch`` on an already-dispatched, still-running task must
        NOT spawn a second concurrent agent against the same worktree.

        Regression for the missing precondition on ``dispatch_created``. Uses a
        hang runner so the first step stays ``in_flight`` (status=working,
        state=active_state) when the second call arrives — the exact window in
        which ``_dispatch_next_step``'s "active state — re-dispatch" self-loop
        CAS (``from_state == to_state``, ``working → working``) *always* wins.

        Fails against the pre-fix code: without the guard the second call
        re-enters ``_dispatch_next_step``, the self-loop CAS wins, a second
        ``_run_agent`` is scheduled (``runner.calls`` → 2, overwriting
        ``_in_flight[id]`` and orphaning the first agent), and no exception is
        raised — so both the ``pytest.raises`` and the ``== 1`` assertion below
        trip.
        """
        from lotsa.orchestrator import DispatchNotAllowed

        data_dir = tmp_path / "tasks"
        data_dir.mkdir()
        flow_yaml = tmp_path / "flow.yaml"
        # Non-conversational first step: state goes backlog → active_state on
        # the first dispatch, which is where the dangerous self-loop lives.
        flow_yaml.write_text("name: test\njobs:\n  - name: coding\n    type: agent\n")
        config = LotsaConfig(
            data_dir=data_dir,
            work_dir=data_dir.parent,
            flow="custom",
            flow_file=flow_yaml,
            model="sonnet",
            budget=5.0,
        )
        db = TaskDB(data_dir / "lotsa.db")
        run(db.initialize())
        svc = OrchestratorService(config, db)
        svc.runner = _RecordingHangRunner()
        run(svc.start())
        try:
            task = run(svc.create_task(message="Build it", defer_dispatch=True))
            run(asyncio.sleep(0.2))
            assert not svc.runner.calls, "deferred create must not dispatch"

            # First release: the agent is now recorded and hanging in-flight.
            run(svc.dispatch_created(task.id))
            run(asyncio.sleep(0.2))
            assert len(svc.runner.calls) == 1
            fresh = run(svc.db.get_task(task.id))
            assert fresh.status == "working" and fresh.current_step == "coding"
            assert task.id in svc._in_flight

            # Second release on the still-active task must be refused, not
            # re-dispatched.
            with pytest.raises(DispatchNotAllowed, match="already been dispatched"):
                run(svc.dispatch_created(task.id))
            run(asyncio.sleep(0.2))
            assert len(svc.runner.calls) == 1, "a second /dispatch must NOT spawn a second agent"
        finally:
            run(svc.shutdown())
            run(db.close())

    def test_materialization_is_idempotent_across_redispatch(self, service, run):
        run(service.create_task("Attach task"))
        run(asyncio.sleep(0.2))
        task_id = run(service.list_tasks_async())[0].id
        self._seed_attachment(service, run, task_id, data=b"PNGDATA")

        run(service.revise(task_id, "first pass"))
        run(asyncio.sleep(0.2))
        run(service.revise(task_id, "second pass"))
        run(asyncio.sleep(0.2))

        call = service.runner.calls[-1]
        work_dir = Path(call["work_dir"])
        attach_dir = work_dir / ".lotsa" / "attachments"
        # Re-dispatch neither duplicated the file nor errored the task.
        assert [p.name for p in attach_dir.iterdir()] == ["bug.png"]
        assert ".lotsa/attachments/bug.png" in call["user_prompt"]
        assert run(service.db.get_task(task_id)).status == "waiting"

    def test_restart_recovery_starts_never_dispatched_deferred_task(self, service, run):
        """A deferred task stranded across a restart must be STARTED by the
        recovery sweep, not blocked with a misleading "couldn't resume" message.

        A task created with ``defer_dispatch=True`` sits at ``status='working'``,
        ``current_step=None`` until ``dispatch_created()`` releases it. If the
        server restarts inside that window there is no interrupted step to
        resume — the first step never ran — so the sweep's
        ``_classify_and_resume`` must recognise the never-dispatched shape and
        release the first step (materializing whatever attachments landed).

        Fails against the pre-fix code: ``_classify_and_resume`` fell into
        ``_redispatch_current_step``, found no step for ``current_step=None``,
        and parked the task at ``blocked`` with "Couldn't resolve the
        interrupted step to resume — click Retry." (the runner never ran).
        """
        task = run(service.create_task("Attach task", defer_dispatch=True))
        run(asyncio.sleep(0.2))
        assert not service.runner.calls, "deferred create must not dispatch"
        # Attachments landed before the (simulated) restart.
        self._seed_attachment(service, run, task.id, data=b"PNGDATA")

        # Simulate the startup recovery sweep encountering this working row.
        row = run(service.db.get_task(task.id))
        assert row.status == "working" and row.current_step is None
        run(service._classify_and_resume(row))
        run(asyncio.sleep(0.2))

        # The first step actually ran (with the attachment), and the task is NOT
        # stuck at an un-resumable blocked.
        assert len(service.runner.calls) == 1, "recovery must release the held first step"
        assert ".lotsa/attachments/bug.png" in service.runner.calls[0]["user_prompt"]
        fresh = run(service.db.get_task(task.id))
        assert fresh.status != "blocked", "never-dispatched deferred task must not be parked as un-resumable"

    def test_stop_parks_never_dispatched_deferred_task_for_retry(self, service, run):
        """If the operator's browser closes between ``create(defer)`` and the
        follow-up ``dispatch()`` (no restart to auto-heal it), the task is stuck
        at ``status='working'`` with no in-flight agent. ``stop()`` must park it
        so ``retry()`` can start it — otherwise it shows "Agent is working…"
        forever with no recovery path.

        Fails against the pre-fix code: ``stop()``'s guard raised
        ``StopNotAllowed`` for any non-in-flight task, so the deferred-and-
        abandoned task had no recovery path (``retry()`` requires ``blocked``;
        ``stop()`` required an in-flight agent).
        """
        task = run(service.create_task("Attach task", defer_dispatch=True))
        run(asyncio.sleep(0.2))
        assert not service.runner.calls
        assert task.id not in service._in_flight, "deferred task has no in-flight agent"

        # No restart — the operator hits Stop on the wedged task.
        run(service.stop(task.id))
        parked = run(service.db.get_task(task.id))
        assert parked.status == "blocked", "stop() must park the stuck deferred task at blocked"

        # And Retry now re-dispatches the held first step (with attachments).
        self._seed_attachment(service, run, task.id, data=b"PNGDATA")
        run(service.retry(task.id))
        run(asyncio.sleep(0.2))
        assert len(service.runner.calls) == 1, "retry must start the parked deferred task"
        assert ".lotsa/attachments/bug.png" in service.runner.calls[0]["user_prompt"]

    def test_stop_does_not_park_interrupted_mid_flow_row(self, service, run):
        """The deferred-shape park in ``stop()`` must NOT swallow a genuinely-
        interrupted mid-flow row that happens to carry ``current_step=None``.

        A working row parked in an *active* state (here ``coding``, not the
        initial ``backlog``) with no in-flight agent is an interrupted/legacy
        task, not a never-dispatched deferred one — ``stop()`` must keep raising
        ``StopNotAllowed`` for it (restart recovery owns that shape), rather than
        parking it as though its first step had never run.

        Fails against a too-broad ``current_step is None`` discriminator: that
        form parks this row at ``blocked`` and the ``pytest.raises`` below never
        fires (the same over-broad match that mis-routed the ADR-021 recovery
        sweep rows).
        """
        from lotsa.orchestrator import StopNotAllowed

        # A working row in an active (non-initial) state, current_step unset,
        # never entered _in_flight — the interrupted-mid-flow shape.
        row = run(service.db.create_task("interrupted mid-flow", state="coding", status="working"))
        assert row.current_step is None and row.id not in service._in_flight

        with pytest.raises(StopNotAllowed, match="actively-working agent"):
            run(service.stop(row.id))
        # Untouched — not parked at blocked by the deferred branch.
        assert run(service.db.get_task(row.id)).status == "working"


class TestAttachmentMessageLinkage:
    """Stamp uploaded attachments onto the specific message they rode in with, at
    INSERT time — the ``messages`` table is append-only (no UPDATE), so the
    linkage can only be set when the row is created.

    Written before the feature exists: the message-creation paths do not accept
    or record attachment filenames yet, and the deferred first-message insert is
    not stamped — so the ``metadata['attachments']`` assertions below fail as the
    expected red.
    """

    @staticmethod
    def _seed_attachment(service, run, task_id, name="bug.png", data=b"PNGDATA"):
        """Place a durable attachment on disk + in ``tasks.metadata`` exactly as
        the upload endpoint would (without depending on the HTTP layer)."""
        attach_dir = service.config.data_dir / "attachments" / "default" / task_id
        attach_dir.mkdir(parents=True, exist_ok=True)
        (attach_dir / name).write_bytes(data)
        row = run(service.db.get_task(task_id))
        meta = dict(row.metadata)
        meta.setdefault("attachments", [])
        meta["attachments"].append(
            {
                "filename": name,
                "rel_path": f".lotsa/attachments/{name}",
                "mime": "image/png",
                "size_bytes": len(data),
                "created_at": "2026-07-02T00:00:00+00:00",
            }
        )
        run(service.db.update_task(task_id, metadata=meta))

    def test_send_message_stamps_attachment_records(self, service, run):
        """``send_message`` records the named attachments onto the chat message it
        inserts, resolving them from the task's attachment metadata.

        Red pre-fix with a TypeError (``send_message`` has no
        ``attachment_filenames`` parameter yet); once the parameter exists the
        stamped-metadata assertion is the behavioural check.
        """
        task = run(service.create_task(message="Look at the screenshot"))
        run(asyncio.sleep(0.2))
        assert run(service.db.get_task(task.id)).status == "waiting"

        self._seed_attachment(service, run, task.id, name="bug.png")
        run(service.send_message(task.id, "here it is", attachment_filenames=["bug.png"]))
        run(asyncio.sleep(0.2))

        msgs = run(service.db.get_messages(task.id))
        mine = [m for m in msgs if m.role == "user" and m.content == "here it is"]
        assert len(mine) == 1
        names = [a["filename"] for a in (mine[0].metadata.get("attachments") or [])]
        assert names == ["bug.png"]

    def test_send_message_without_attachments_leaves_metadata_clean(self, service, run):
        """The stamp is opt-in: a message sent with no attachment filenames must
        not accrue an ``attachments`` key (so the bubble renders no strip)."""
        task = run(service.create_task(message="Plain message"))
        run(asyncio.sleep(0.2))
        assert run(service.db.get_task(task.id)).status == "waiting"

        run(service.send_message(task.id, "no files here", attachment_filenames=None))
        run(asyncio.sleep(0.2))

        msgs = run(service.db.get_messages(task.id))
        mine = [m for m in msgs if m.role == "user" and m.content == "no files here"]
        assert len(mine) == 1
        assert not (mine[0].metadata.get("attachments"))

    def test_deferred_first_message_stamped_with_attachments(self, service, run):
        """The empty-state first-message path: attachments upload after the task
        is created but before its first step dispatches. The first ``You`` chat
        message must carry them once ``dispatch_created`` releases the step.

        Red pre-fix: the first message is inserted at create time (before the
        upload), unstamped, and nothing stamps it afterwards.
        """
        task = run(service.create_task(message="Build from this mockup", defer_dispatch=True))
        run(asyncio.sleep(0.2))
        # Attachment lands during the deferred window, as the upload endpoint would.
        self._seed_attachment(service, run, task.id, name="mockup.png")

        run(service.dispatch_created(task.id))
        run(asyncio.sleep(0.2))

        chats = run(service.db.get_messages(task.id, msg_type="chat"))
        mine = [m for m in chats if m.role == "user" and m.content == "Build from this mockup"]
        # Exactly one — append-only, no second stamped copy alongside an
        # unstamped create-time row.
        assert len(mine) == 1, "the first operator message must exist exactly once"
        names = [a["filename"] for a in (mine[0].metadata.get("attachments") or [])]
        assert names == ["mockup.png"]

    def test_deferred_first_message_stamped_via_recovery_release(self, service, run):
        """Restart recovery releases a stranded deferred task through
        ``_release_first_step`` (not ``dispatch_created``). That path must stamp
        the first message too, so a crash between upload and dispatch doesn't
        lose the linkage.
        """
        task = run(service.create_task(message="Ship the mockup", defer_dispatch=True))
        run(asyncio.sleep(0.2))
        self._seed_attachment(service, run, task.id, name="mockup.png")

        # The recovery sweep calls _release_first_step directly on the fresh row.
        row = run(service.db.get_task(task.id))
        run(service._release_first_step(row))
        run(asyncio.sleep(0.2))

        chats = run(service.db.get_messages(task.id, msg_type="chat"))
        mine = [m for m in chats if m.role == "user" and m.content == "Ship the mockup"]
        assert len(mine) == 1
        names = [a["filename"] for a in (mine[0].metadata.get("attachments") or [])]
        assert names == ["mockup.png"]
