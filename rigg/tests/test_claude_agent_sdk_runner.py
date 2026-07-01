"""Tests for ClaudeAgentSDKRunner (ADR-028 Phase 1) — claude-agent-sdk mocked.

Mirrors ``test_agent_runner.py`` but for the SDK-shaped runner. The
``claude-agent-sdk`` package is mocked via a fake module injected into
``sys.modules`` so no real Claude/Anthropic calls are made (rigg's
test-isolation rule: no real subprocesses / no real API).

The runner is expected to lazily ``import claude_agent_sdk`` *inside*
``run()`` so the module stays importable when the package is absent, and
so the missing-dependency path raises ``AgentRunnerError`` cleanly. These
tests inject a fake module under that name; the runner's lazy import picks
it up.

All references to the not-yet-implemented ``ClaudeAgentSDKRunner`` go
through ``_runner_cls()`` (a local import) so the module still collects and
each test reports its own failure (ImportError) until Phase 1 lands.
"""

from __future__ import annotations

import inspect
import sys
import types

import pytest

from rigg.agent_runner import AgentRunnerError
from rigg.models import AgentResult


def _runner_cls():
    """Local import of the runner under construction (ADR-028 Phase 1)."""
    from rigg.claude_agent_sdk_runner import ClaudeAgentSDKRunner

    return ClaudeAgentSDKRunner


# ---------------------------------------------------------------------------
# Fake claude-agent-sdk surface
#
# These stand in for the real SDK's message/option types. The runner is
# expected to ``isinstance``-check against ``ResultMessage`` / ``SystemMessage``
# imported from the package, so the fakes must be the same classes the fake
# module exposes (they are — see the ``fake_sdk`` fixture).
# ---------------------------------------------------------------------------


class FakeResultMessage:
    """Terminal message carrying cost/usage/session/result text."""

    def __init__(
        self,
        *,
        is_error: bool = False,
        result: str | None = "output",
        total_cost_usd: float | None = 0.01,
        usage: dict | None = None,
        session_id: str | None = "ses-default",
        duration_ms: int = 10,
        subtype: str = "success",
    ) -> None:
        self.is_error = is_error
        self.result = result
        self.total_cost_usd = total_cost_usd
        self.usage = {"input_tokens": 1, "output_tokens": 1} if usage is None else usage
        self.session_id = session_id
        self.duration_ms = duration_ms
        self.subtype = subtype


class FakeSystemMessage:
    """``init`` system message carrying the session id at stream start."""

    def __init__(self, *, subtype: str = "init", data: dict | None = None) -> None:
        self.subtype = subtype
        self.data = data or {}


class FakeAssistantMessage:
    def __init__(self, *, content=None) -> None:
        self.content = content or []


class FakeUserMessage:
    def __init__(self, *, content=None) -> None:
        self.content = content or []


class FakeClaudeAgentOptions:
    """Records whatever kwargs the runner passes so tests can assert on them."""

    def __init__(self, **kwargs) -> None:
        self.kwargs = dict(kwargs)
        self.__dict__.update(kwargs)


class FakeCLIConnectionError(Exception):
    pass


class FakeCLINotFoundError(FakeCLIConnectionError):
    pass


class FakeProcessError(Exception):
    pass


class FakeCLIJSONDecodeError(Exception):
    pass


class _SDKController:
    """Drives the fake ``query`` behaviour and captures the call."""

    def __init__(self) -> None:
        self.messages: list = []
        self.raise_exc: Exception | None = None
        self.hang_seconds: float = 0.0
        self.last_prompt = None
        self.last_options = None


@pytest.fixture
def fake_sdk(monkeypatch):
    """Inject a fake ``claude_agent_sdk`` module; return its controller."""
    import asyncio

    controller = _SDKController()
    mod = types.ModuleType("claude_agent_sdk")

    async def query(prompt=None, options=None, **kwargs):
        controller.last_prompt = prompt
        controller.last_options = options
        if controller.raise_exc is not None:
            raise controller.raise_exc
        if controller.hang_seconds:
            await asyncio.sleep(controller.hang_seconds)
        for message in controller.messages:
            yield message

    mod.query = query
    mod.ClaudeAgentOptions = FakeClaudeAgentOptions
    mod.ResultMessage = FakeResultMessage
    mod.SystemMessage = FakeSystemMessage
    mod.AssistantMessage = FakeAssistantMessage
    mod.UserMessage = FakeUserMessage
    mod.CLINotFoundError = FakeCLINotFoundError
    mod.CLIConnectionError = FakeCLIConnectionError
    mod.ProcessError = FakeProcessError
    mod.CLIJSONDecodeError = FakeCLIJSONDecodeError

    monkeypatch.setitem(sys.modules, "claude_agent_sdk", mod)
    return controller


@pytest.fixture
def runner():
    """A runner with an API key supplied via credentials (no env reliance)."""
    Runner = _runner_cls()
    return Runner(model="sonnet", budget_usd=2.0, credentials={"ANTHROPIC_API_KEY": "sk-test-key"})


# ---------------------------------------------------------------------------
# Result mapping
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_run_success_maps_result_fields(runner, fake_sdk, tmp_path):
    """A successful ResultMessage maps into a successful AgentResult with
    cost, tokens, session, and model populated."""
    fake_sdk.messages = [
        FakeSystemMessage(subtype="init", data={"session_id": "ses-1"}),
        FakeResultMessage(
            is_error=False,
            result="final text",
            total_cost_usd=0.0234,
            usage={"input_tokens": 1500, "output_tokens": 300},
            session_id="ses-1",
            duration_ms=4321,
        ),
    ]

    result = await runner.run("sys", "usr", tmp_path)

    assert isinstance(result, AgentResult)
    assert result.success is True
    assert result.stdout == "final text"
    assert result.cost_usd == 0.0234
    assert result.input_tokens == 1500
    assert result.output_tokens == 300
    assert result.model == "sonnet"
    assert result.session_id == "ses-1"
    assert result.return_code == 0
    assert result.duration_ms >= 0


@pytest.mark.asyncio
async def test_run_failure_maps_to_unsuccessful_result(runner, fake_sdk, tmp_path):
    """A ResultMessage with is_error=True maps to an unsuccessful AgentResult."""
    fake_sdk.messages = [FakeResultMessage(is_error=True, result="boom")]

    result = await runner.run("sys", "usr", tmp_path)

    assert result.success is False
    assert result.return_code == 1


@pytest.mark.asyncio
async def test_run_timeout_returns_unsuccessful_result_not_raise(runner, fake_sdk, tmp_path):
    """A timeout returns an unsuccessful AgentResult (return_code=-1) and
    does NOT raise — matching ClaudeCodeRunner's TimeoutExpired path."""
    fake_sdk.hang_seconds = 10.0

    result = await runner.run("sys", "usr", tmp_path, timeout_seconds=0.05)

    assert result.success is False
    assert result.return_code == -1
    assert "timeout" in result.stderr.lower()


@pytest.mark.asyncio
async def test_run_missing_sdk_raises_agent_runner_error(runner, monkeypatch, tmp_path):
    """When claude-agent-sdk is not importable, run() raises AgentRunnerError
    (the runner could not start)."""
    # Setting the module to None makes ``import claude_agent_sdk`` raise
    # ImportError, simulating the package not being installed.
    monkeypatch.setitem(sys.modules, "claude_agent_sdk", None)

    with pytest.raises(AgentRunnerError):
        await runner.run("sys", "usr", tmp_path)


@pytest.mark.asyncio
async def test_run_missing_api_key_raises_agent_runner_error(fake_sdk, monkeypatch, tmp_path):
    """No ANTHROPIC_API_KEY in merged env (no keychain fallback) → raises."""
    monkeypatch.delenv("ANTHROPIC_API_KEY", raising=False)
    Runner = _runner_cls()
    runner = Runner(model="sonnet", credentials=None)

    with pytest.raises(AgentRunnerError):
        await runner.run("sys", "usr", tmp_path)


@pytest.mark.asyncio
async def test_run_cli_not_found_raises_agent_runner_error(runner, fake_sdk, tmp_path):
    """CLINotFoundError from the SDK (runner can't start) → AgentRunnerError."""
    fake_sdk.raise_exc = FakeCLINotFoundError("claude binary missing")

    with pytest.raises(AgentRunnerError):
        await runner.run("sys", "usr", tmp_path)


@pytest.mark.asyncio
async def test_run_no_result_message_returns_unsuccessful_result(runner, fake_sdk, tmp_path):
    """A stream that ends without a terminal ResultMessage returns an
    unsuccessful AgentResult (not a raise) — the init session_id is still
    carried through so a resume id isn't lost."""
    fake_sdk.messages = [FakeSystemMessage(subtype="init", data={"session_id": "ses-x"})]

    result = await runner.run("sys", "usr", tmp_path)

    assert result.success is False
    assert result.return_code == 1
    assert "no result message" in result.stderr.lower()
    assert result.session_id == "ses-x"


@pytest.mark.asyncio
async def test_run_empty_stream_returns_unsuccessful_result(runner, fake_sdk, tmp_path):
    """An entirely empty stream (no messages at all) also maps to a failure
    AgentResult rather than raising."""
    fake_sdk.messages = []

    result = await runner.run("sys", "usr", tmp_path)

    assert result.success is False
    assert result.return_code == 1
    assert result.stdout == ""


@pytest.mark.asyncio
@pytest.mark.parametrize("exc_attr", ["CLIConnectionError", "ProcessError", "CLIJSONDecodeError"])
async def test_run_mid_run_sdk_error_returns_unsuccessful_result(runner, fake_sdk, tmp_path, exc_attr):
    """A mid-run SDK error (connection drop / process error / undecodable
    stream) returns an unsuccessful AgentResult with the runner's standard
    stderr format, rather than propagating a raw exception. The runner started,
    so this mirrors ClaudeCodeRunner's non-fatal-subprocess-error path — unlike
    CLINotFoundError, which raises AgentRunnerError (runner couldn't start)."""
    import claude_agent_sdk

    exc_cls = getattr(claude_agent_sdk, exc_attr)
    fake_sdk.raise_exc = exc_cls("mid-stream failure")

    result = await runner.run("sys", "usr", tmp_path)

    assert result.success is False
    assert result.return_code == 1
    assert exc_attr in result.stderr
    assert "claude-agent-sdk error" in result.stderr


@pytest.mark.asyncio
async def test_run_missing_usage_keys_map_to_none(runner, fake_sdk, tmp_path):
    """Missing usage/cost on the ResultMessage map to None on AgentResult."""
    fake_sdk.messages = [
        FakeResultMessage(is_error=False, result="x", usage={}, total_cost_usd=None, session_id="s"),
    ]

    result = await runner.run("sys", "usr", tmp_path)

    assert result.input_tokens is None
    assert result.output_tokens is None
    assert result.cost_usd is None


@pytest.mark.asyncio
async def test_session_id_falls_back_to_init_message(runner, fake_sdk, tmp_path):
    """When the ResultMessage carries no session_id, the init SystemMessage's
    session_id is used (``result_msg.session_id or session_out``)."""
    fake_sdk.messages = [
        FakeSystemMessage(subtype="init", data={"session_id": "ses-init"}),
        FakeResultMessage(is_error=False, result="x", session_id=None),
    ]

    result = await runner.run("sys", "usr", tmp_path)

    assert result.session_id == "ses-init"


# ---------------------------------------------------------------------------
# Options construction (auth, resume, system prompt, cwd)
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_run_passes_session_id_as_resume(runner, fake_sdk, tmp_path):
    """session_id is forwarded to the SDK as the resume option."""
    fake_sdk.messages = [FakeResultMessage(is_error=False, result="x")]

    await runner.run("sys", "usr", tmp_path, session_id="ses-123")

    assert getattr(fake_sdk.last_options, "resume", None) == "ses-123"


@pytest.mark.asyncio
async def test_run_without_session_id_sets_resume_none(runner, fake_sdk, tmp_path):
    """No session_id → no resume requested."""
    fake_sdk.messages = [FakeResultMessage(is_error=False, result="x")]

    await runner.run("sys", "usr", tmp_path)

    assert getattr(fake_sdk.last_options, "resume", None) in (None, "")


@pytest.mark.asyncio
async def test_run_injects_credentials_into_env_and_overrides_environ(fake_sdk, monkeypatch, tmp_path):
    """credentials win over os.environ when merged into the SDK env, matching
    ClaudeCodeRunner's ``{**os.environ, **credentials}`` precedence."""
    monkeypatch.setenv("ANTHROPIC_API_KEY", "from-env")
    Runner = _runner_cls()
    runner = Runner(model="sonnet", credentials={"ANTHROPIC_API_KEY": "from-creds"})
    fake_sdk.messages = [FakeResultMessage(is_error=False, result="x")]

    await runner.run("sys", "usr", tmp_path)

    env = getattr(fake_sdk.last_options, "env", {})
    assert env.get("ANTHROPIC_API_KEY") == "from-creds"


@pytest.mark.asyncio
async def test_run_forwards_anthropic_base_url(runner, fake_sdk, monkeypatch, tmp_path):
    """ANTHROPIC_BASE_URL (self-hosted proxy) is forwarded, not stripped."""
    monkeypatch.setenv("ANTHROPIC_BASE_URL", "https://proxy.internal")
    fake_sdk.messages = [FakeResultMessage(is_error=False, result="x")]

    await runner.run("sys", "usr", tmp_path)

    env = getattr(fake_sdk.last_options, "env", {})
    assert env.get("ANTHROPIC_BASE_URL") == "https://proxy.internal"


@pytest.mark.asyncio
async def test_run_forwards_max_output_tokens_into_env(fake_sdk, tmp_path):
    """max_output_tokens is forwarded into the SDK env as
    CLAUDE_CODE_MAX_OUTPUT_TOKENS, mirroring the CLI runner (the SDK drives the
    same Claude Code runtime, which reads that var)."""
    Runner = _runner_cls()
    runner = Runner(
        model="sonnet",
        credentials={"ANTHROPIC_API_KEY": "sk-test-key"},
        max_output_tokens=4096,
    )
    fake_sdk.messages = [FakeResultMessage(is_error=False, result="x")]

    await runner.run("sys", "usr", tmp_path)

    env = getattr(fake_sdk.last_options, "env", {})
    assert env.get("CLAUDE_CODE_MAX_OUTPUT_TOKENS") == "4096"


@pytest.mark.asyncio
async def test_run_omits_max_output_tokens_when_unset(runner, fake_sdk, tmp_path):
    """When max_output_tokens is unset (None), the env var is not injected."""
    fake_sdk.messages = [FakeResultMessage(is_error=False, result="x")]

    await runner.run("sys", "usr", tmp_path)

    env = getattr(fake_sdk.last_options, "env", {})
    assert "CLAUDE_CODE_MAX_OUTPUT_TOKENS" not in env


@pytest.mark.asyncio
async def test_run_uses_preset_append_system_prompt(runner, fake_sdk, tmp_path):
    """ADR-025 parity: Lotsa's rules append on top of the claude_code preset
    rather than replacing it (the SDK's preset-append form)."""
    fake_sdk.messages = [FakeResultMessage(is_error=False, result="x")]

    await runner.run("MY SYSTEM PROMPT", "usr", tmp_path)

    sp = getattr(fake_sdk.last_options, "system_prompt", None)
    assert isinstance(sp, dict)
    assert sp.get("type") == "preset"
    assert sp.get("preset") == "claude_code"
    assert sp.get("append") == "MY SYSTEM PROMPT"


@pytest.mark.asyncio
async def test_run_sets_cwd_model_and_setting_sources(runner, fake_sdk, tmp_path):
    """cwd is the worktree, model is forwarded, and only project-level
    settings are loaded (ADR-025: isolate operator user/local settings)."""
    fake_sdk.messages = [FakeResultMessage(is_error=False, result="x")]

    await runner.run("sys", "usr", tmp_path)

    opts = fake_sdk.last_options
    assert str(opts.cwd) == str(tmp_path)
    assert opts.model == "sonnet"
    assert opts.setting_sources == ["project"]


@pytest.mark.asyncio
async def test_run_forwards_model_override_to_sdk_options(runner, fake_sdk, tmp_path):
    """ADR-022: a per-call ``model=`` override reaches the SDK as the
    ``ClaudeAgentOptions.model`` kwarg, overriding the construction-time model.

    Unlike the CLI runners (which inject ``--model`` as a command-line flag),
    the SDK runner forwards the resolved model through the options object's
    ``model=`` kwarg on the ``query()`` call — this pins that path."""
    fake_sdk.messages = [FakeResultMessage(is_error=False, result="x")]

    result = await runner.run("sys", "usr", tmp_path, model="opus")

    # The override reaches the SDK's options object...
    assert fake_sdk.last_options.model == "opus"
    # ...and the AgentResult reports the resolved model that actually ran.
    assert result.model == "opus"


@pytest.mark.asyncio
async def test_run_uses_construction_model_when_no_override(runner, fake_sdk, tmp_path):
    """With no per-call ``model=``, the SDK runner forwards its construction-time
    model (``sonnet`` for this fixture) on the options object."""
    fake_sdk.messages = [FakeResultMessage(is_error=False, result="x")]

    result = await runner.run("sys", "usr", tmp_path)

    assert fake_sdk.last_options.model == "sonnet"
    assert result.model == "sonnet"


@pytest.mark.asyncio
async def test_run_pins_pwd_to_work_dir(runner, fake_sdk, monkeypatch, tmp_path):
    """PWD in the forwarded env is overridden to the worktree, not left as the
    orchestrator's cwd — mirrors ClaudeCodeRunner's worktree-escape guard. A
    leaked PWD makes the agent's tools treat the orchestrator's checkout as the
    project root and commit outside the assigned worktree."""
    # Simulate the orchestrator's own cwd leaking through os.environ.
    monkeypatch.setenv("PWD", "/some/other/orchestrator/cwd")
    fake_sdk.messages = [FakeResultMessage(is_error=False, result="x")]

    await runner.run("sys", "usr", tmp_path)

    env = getattr(fake_sdk.last_options, "env", {})
    assert env.get("PWD") == str(tmp_path)


@pytest.mark.asyncio
async def test_run_passes_user_prompt_as_query(runner, fake_sdk, tmp_path):
    """The user prompt is the query the SDK runs."""
    fake_sdk.messages = [FakeResultMessage(is_error=False, result="x")]

    await runner.run("sys", "the user prompt", tmp_path)

    assert fake_sdk.last_prompt == "the user prompt"


# ---------------------------------------------------------------------------
# Protocol conformance & constructor parity
# ---------------------------------------------------------------------------


def test_runner_satisfies_agent_runner_protocol_structurally():
    """ClaudeAgentSDKRunner has the AgentRunner.run() shape plus the new
    dispatch_shape_prompt() method."""
    runner = _runner_cls()()
    assert inspect.iscoroutinefunction(runner.run)
    params = inspect.signature(runner.run).parameters
    for name in ("system_prompt", "user_prompt", "work_dir", "allowed_tools", "timeout_seconds", "session_id"):
        assert name in params, f"run() missing parameter {name!r}"
    assert callable(getattr(runner, "dispatch_shape_prompt", None))


def test_constructor_mirrors_claude_code_runner():
    """Same constructor kwargs as ClaudeCodeRunner (requirement #2)."""
    Runner = _runner_cls()
    runner = Runner(
        model="opus",
        budget_usd=9.0,
        credentials={"ANTHROPIC_API_KEY": "k"},
        max_output_tokens=128000,
    )
    assert runner is not None


# ---------------------------------------------------------------------------
# SDK dispatch-shape fragment must be honest to wired capability (Phase 2,
# requirement #13 / acceptance #5). These don't touch the SDK (no run()).
# ---------------------------------------------------------------------------


def test_dispatch_shape_prompt_returns_nonempty_str():
    frag = _runner_cls()().dispatch_shape_prompt()
    assert isinstance(frag, str)
    assert frag.strip()


def test_sdk_fragment_does_not_falsely_advertise_cross_turn_tools():
    """Interception/cross-turn lifecycle is NOT wired in this cut, so the SDK
    fragment must not advertise the un-wired capabilities (the ADR's
    illustrative Phase-2 advertising language)."""
    frag = _runner_cls()().dispatch_shape_prompt()
    forbidden = [
        "Lotsa routes it to the dashboard",
        "Lotsa polls and re-engages",
        "Lotsa fires it via SDK resume",
        "keeps them alive across turns",
        "survive the turn boundary",
    ]
    for phrase in forbidden:
        assert phrase not in frag, f"SDK fragment falsely advertises un-wired capability: {phrase!r}"


def test_sdk_fragment_preserves_needs_input_channel():
    """NEEDS_INPUT is the blocking-question channel across every runner shape."""
    frag = _runner_cls()().dispatch_shape_prompt()
    assert "NEEDS_INPUT" in frag


# ---------------------------------------------------------------------------
# ADR-040 — resume-capability signal
# ---------------------------------------------------------------------------


def test_sdk_runner_reports_no_resume_support():
    """Cross-restart session durability for the SDK runner is unverified
    (ADR-028 Phase 4 deferred), so it reports *no* resume support and the
    orchestrator routes its interrupted steps to safe idempotent re-run-from-
    start rather than ``--resume`` (ADR-040 R3, conservative default)."""
    runner = _runner_cls()()
    assert runner.supports_resume is False
