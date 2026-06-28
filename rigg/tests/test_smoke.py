"""End-to-end smoke tests for the Rigg SDK.

Wires up all real SDK components with minimal in-memory fakes for the
three protocols that require external infrastructure (ItemSource,
AgentRunner, Notifier). Exercises the full dispatch lifecycle.
"""

import logging

import pytest

from rigg.blocking import BlockingProtocol
from rigg.models import AgentResult, BlockingReason, Item
from rigg.orchestration import DispatchRule, OrchestrationEngine
from rigg.prompt_registry import PromptRegistry
from rigg.proof_collector import ProofValidator
from rigg.review_pipeline import ReviewPipeline
from rigg.state_machine import InvalidTransition, StateMachine, TransitionRule

# ---------------------------------------------------------------------------
# Fakes — minimal implementations of the three infrastructure protocols
# ---------------------------------------------------------------------------


class InMemoryItemSource:
    """Serves items from a dict, grouped by state."""

    def __init__(self) -> None:
        self.items: list[Item] = []

    async def items_in_state(self, state: str) -> list[Item]:
        return [i for i in self.items if i.state == state]


class RecordingRunner:
    """Records every agent invocation and returns a configurable result."""

    def __init__(self, result: AgentResult | None = None) -> None:
        self.calls: list[dict] = []
        self._result = result or AgentResult(success=True, stdout="done", stderr="", return_code=0, duration_ms=50)

    async def run(self, system_prompt, user_prompt, work_dir, **kwargs):
        self.calls.append(
            {
                "system_prompt": system_prompt,
                "user_prompt": user_prompt,
                "work_dir": work_dir,
                **kwargs,
            }
        )
        return self._result


class RecordingNotifier:
    """Records notifications; optionally raises to test secondary failure."""

    def __init__(self, *, should_fail: bool = False) -> None:
        self.calls: list[tuple[str, BlockingReason]] = []
        self._should_fail = should_fail

    async def notify(self, item_id: str, reason: BlockingReason) -> None:
        self.calls.append((item_id, reason))
        if self._should_fail:
            raise RuntimeError("Notification service unavailable")


# ---------------------------------------------------------------------------
# Shared fixtures
# ---------------------------------------------------------------------------


def bot_state_machine() -> StateMachine:
    """5-state machine mirroring the bot lifecycle."""
    return StateMachine(
        states=["backlog", "coding", "review", "complete", "blocked"],
        transitions={
            ("backlog", "coding"): TransitionRule(),
            ("coding", "review"): TransitionRule(),
            ("review", "complete"): TransitionRule(),
            ("review", "coding"): TransitionRule(),  # fix loop
            ("coding", "blocked"): TransitionRule(),
            ("backlog", "blocked"): TransitionRule(),
        },
        initial_state="backlog",
    )


def make_engine(
    source: InMemoryItemSource,
    runner: RecordingRunner,
    notifier: RecordingNotifier,
    sm: StateMachine | None = None,
    rules: list[DispatchRule] | None = None,
) -> OrchestrationEngine:
    sm = sm or bot_state_machine()
    blocking = BlockingProtocol(state_machine=sm, notifier=notifier)
    rules = rules or [
        DispatchRule(
            queue_state="backlog",
            active_state="coding",
            job_type="coding",
            build_prompts=lambda item: (
                f"You are a coding agent for {item.title}.",
                f"Implement: {item.body}",
            ),
        ),
    ]
    return OrchestrationEngine(
        state_machine=sm,
        item_source=source,
        agent_runner=runner,
        blocking=blocking,
        dispatch_rules=rules,
    )


# ---------------------------------------------------------------------------
# Scenarios
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_happy_path():
    """Item in backlog → dispatched → agent succeeds → item now coding."""
    source = InMemoryItemSource()
    source.items = [Item(id="TASK-1", state="backlog", title="Add login", body="OAuth flow")]
    runner = RecordingRunner()
    notifier = RecordingNotifier()

    engine = make_engine(source, runner, notifier)
    result = await engine.dispatch()

    assert result.job_type == "coding"
    assert result.item.id == "TASK-1"
    assert result.item.state == "coding"  # transitioned from backlog
    assert result.agent_result.success is True
    assert len(runner.calls) == 1
    assert "Add login" in runner.calls[0]["system_prompt"]
    assert "OAuth flow" in runner.calls[0]["user_prompt"]
    assert len(notifier.calls) == 0  # no failure, no notification


@pytest.mark.asyncio
async def test_priority_ordering():
    """Engine picks the item with lowest priority value."""
    source = InMemoryItemSource()
    source.items = [
        Item(id="LOW", state="backlog", priority=10, title="Low"),
        Item(id="HIGH", state="backlog", priority=1, title="High"),
        Item(id="MED", state="backlog", priority=5, title="Med"),
    ]
    runner = RecordingRunner()
    notifier = RecordingNotifier()

    engine = make_engine(source, runner, notifier)
    result = await engine.dispatch()

    assert result.item.id == "HIGH"


@pytest.mark.asyncio
async def test_agent_failure_blocks_item():
    """Agent failure → item transitions to blocked, notifier called."""
    source = InMemoryItemSource()
    source.items = [Item(id="TASK-2", state="backlog", title="Broken")]
    runner = RecordingRunner(AgentResult(success=False, stdout="", stderr="segfault", return_code=139, duration_ms=10))
    notifier = RecordingNotifier()

    engine = make_engine(source, runner, notifier)
    result = await engine.dispatch()

    assert result.item.state == "blocked"
    assert result.agent_result.success is False
    assert len(notifier.calls) == 1
    assert notifier.calls[0][0] == "TASK-2"
    assert notifier.calls[0][1].code == "AGENT_FAILURE"


@pytest.mark.asyncio
async def test_notification_failure_secondary_pattern(caplog):
    """Notifier throws → item still blocked, error logged, no re-raise."""
    source = InMemoryItemSource()
    source.items = [Item(id="TASK-3", state="backlog", title="Fail notify")]
    runner = RecordingRunner(AgentResult(success=False, stdout="", stderr="crash", return_code=1, duration_ms=10))
    notifier = RecordingNotifier(should_fail=True)

    engine = make_engine(source, runner, notifier)

    with caplog.at_level(logging.ERROR):
        result = await engine.dispatch()

    # Item still transitioned to blocked despite notification failure
    assert result.item.state == "blocked"
    assert "Notification failed" in caplog.text


@pytest.mark.asyncio
async def test_empty_queues_idle():
    """No items in any queue → idle result."""
    source = InMemoryItemSource()
    runner = RecordingRunner()
    notifier = RecordingNotifier()

    engine = make_engine(source, runner, notifier)
    result = await engine.dispatch()

    assert result.job_type is None
    assert result.item is None
    assert result.agent_result is None
    assert len(runner.calls) == 0


@pytest.mark.asyncio
async def test_multi_rule_fallthrough():
    """First rule's queue empty → falls through to second rule."""
    source = InMemoryItemSource()
    # Item is in "review" state, not "backlog"
    source.items = [Item(id="TASK-4", state="review", title="Review me")]

    sm = StateMachine(
        states=["backlog", "coding", "review", "fixing", "complete", "blocked"],
        transitions={
            ("backlog", "coding"): TransitionRule(),
            ("review", "fixing"): TransitionRule(),
            ("coding", "blocked"): TransitionRule(),
            ("fixing", "blocked"): TransitionRule(),
        },
        initial_state="backlog",
    )

    rules = [
        DispatchRule(
            queue_state="backlog",
            active_state="coding",
            job_type="coding",
            build_prompts=lambda item: ("code system", "code user"),
        ),
        DispatchRule(
            queue_state="review",
            active_state="fixing",
            job_type="fix",
            build_prompts=lambda item: ("fix system", "fix user"),
        ),
    ]

    runner = RecordingRunner()
    notifier = RecordingNotifier()
    engine = make_engine(source, runner, notifier, sm=sm, rules=rules)
    result = await engine.dispatch()

    assert result.job_type == "fix"
    assert result.item.state == "fixing"


@pytest.mark.asyncio
async def test_guard_blocks_transition():
    """Guard rejects transition → InvalidTransition raised."""

    class RequiresBody:
        def check(self, item, context):
            return bool(item.body)

    sm = StateMachine(
        states=["backlog", "coding", "blocked"],
        transitions={
            ("backlog", "coding"): TransitionRule(guards=[RequiresBody()]),
            ("backlog", "blocked"): TransitionRule(),
        },
        initial_state="backlog",
    )

    source = InMemoryItemSource()
    source.items = [Item(id="TASK-5", state="backlog", title="No body", body="")]
    runner = RecordingRunner()
    notifier = RecordingNotifier()

    engine = make_engine(source, runner, notifier, sm=sm)

    with pytest.raises(InvalidTransition, match="guard"):
        await engine.dispatch()

    # Agent was never called
    assert len(runner.calls) == 0


@pytest.mark.asyncio
async def test_prompt_registry_integration(tmp_path):
    """PromptRegistry loads real files, prompts flow into agent call."""
    prompts_dir = tmp_path / "prompts"
    prompts_dir.mkdir()
    (prompts_dir / "coding-system.md").write_text("You are a careful coding agent.")
    (prompts_dir / "coding-user.md").write_text("Build the feature described below.")

    registry = PromptRegistry(search_paths=[prompts_dir])

    source = InMemoryItemSource()
    source.items = [Item(id="TASK-6", state="backlog", title="With prompts")]
    runner = RecordingRunner()
    notifier = RecordingNotifier()

    rules = [
        DispatchRule(
            queue_state="backlog",
            active_state="coding",
            job_type="coding",
            build_prompts=lambda item: (
                registry.load("coding-system"),
                registry.load("coding-user") + f"\n\nTask: {item.title}",
            ),
        ),
    ]

    engine = make_engine(source, runner, notifier, rules=rules)
    await engine.dispatch()

    assert "careful coding agent" in runner.calls[0]["system_prompt"]
    assert "Build the feature" in runner.calls[0]["user_prompt"]
    assert "With prompts" in runner.calls[0]["user_prompt"]


# ---------------------------------------------------------------------------
# Bonus: ReviewPipeline + ProofValidator as standalone components
# ---------------------------------------------------------------------------


def test_review_pipeline_standalone():
    """ReviewPipeline assesses review bodies without needing any wiring."""
    pipeline = ReviewPipeline()

    from rigg.models import ReviewStatus

    # Clean review
    assert pipeline.assess("## Low\n- minor", "2026-03-17T12:00:00Z", "2026-03-17T11:00:00Z") == ReviewStatus.CLEAN

    # Review with medium issues
    feedback_body = "## Medium\n- bug found"
    assert pipeline.assess(feedback_body, "2026-03-17T12:00:00Z", "2026-03-17T11:00:00Z") == ReviewStatus.FEEDBACK

    # Stale review
    assert pipeline.assess("## Low\n- minor", "2026-03-17T10:00:00Z", "2026-03-17T11:00:00Z") == ReviewStatus.PENDING


def test_proof_validator_standalone():
    """ProofValidator checks proof sections without any wiring."""
    validator = ProofValidator()

    body = "## Summary\nDone.\n\n### Tests pass\nAll green.\n\n### API works\n200 OK."
    result = validator.validate(body, required_sections=["Tests pass", "API works"])
    assert result.valid is True

    result = validator.validate(body, required_sections=["Tests pass", "Missing section"])
    assert result.valid is False
    assert "Missing section" in result.missing_sections
