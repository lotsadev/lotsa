"""Flow presets — YAML-driven typed-job model with derived state machines.

ADR-014 Layer A: a process is a state machine whose states are jobs, and
each job declares its execution mode via ``type:`` (``agent`` / ``action``
/ ``monitor``). The state machine is fully derived from the job list with
no synthetic state names — there is no longer a ``pr:`` block, and the
``pushing`` / ``waiting_for_pr`` / ``rebasing`` synthetic states are gone.

The schema:

    process: build
    jobs:
      - { name: code, type: agent, prompt: coding }
      - { name: push_pr, type: action, tool: push_pr }
      - { name: wait, type: monitor, engine: pr_monitor, config: { ... } }
    flows:
      main:
        steps:
          - code
          - { name: review, rules: [ ... ] }   # per-flow rule override
          - push_pr
          - wait
"""

from __future__ import annotations

import logging
import re
from collections.abc import Callable
from dataclasses import dataclass, field
from pathlib import Path
from typing import TYPE_CHECKING, Any, Literal, Protocol, runtime_checkable

import yaml as _yaml

from lotsa.agents import AGENT_OUTCOMES, AGENTS_DIR
from rigg import DispatchRule, StateMachine, TransitionRule
from rigg.models import Item
from rigg.prompt_registry import PromptNotFound

if TYPE_CHECKING:
    from lotsa.agents import Agent
    from rigg.models import AgentResult

logger = logging.getLogger(__name__)

BUNDLED_PROMPTS = Path(__file__).parent / "prompts"


@runtime_checkable
class PromptLoader(Protocol):
    """The prompt-resolution surface the flow/orchestrator layer depends on.

    Both :class:`rigg.PromptRegistry` and the catalog-aware
    :class:`AgentPromptRegistry` (below) satisfy it — callers only ever invoke
    ``load`` / ``load_optional``, so the process/flow config is typed against
    this Protocol rather than either concrete class.
    """

    def load(self, name: str) -> str: ...

    def load_optional(self, name: str) -> str | None: ...


class AgentPromptRegistry:
    """Load prompts from the ADR-044 agent catalog (``agents/<name>/<kind>.md``).

    Presents the same ``load`` / ``load_optional`` surface as
    :class:`rigg.PromptRegistry` so callers (the orchestrator, dispatch-rule
    builders) are unchanged: they still ask for ``"<agent>-system"`` /
    ``"<agent>-user"``. Resolution order for ``"<agent>-<kind>"``:

    1. operator override dir, flat legacy name ``<agent>-<kind>.md`` (keeps
       pre-catalog custom ``--prompts-dir`` layouts and inline-process prompts
       working);
    2. operator override dir, catalog layout ``<agent>/<kind>.md``;
    3. the bundled catalog ``<catalog_dir>/<agent>/<kind>.md``.
    """

    def __init__(self, override_dir: Path | None, catalog_dir: Path = AGENTS_DIR) -> None:
        self.override_dir = override_dir
        self.catalog_dir = catalog_dir

    @staticmethod
    def _split(name: str) -> tuple[str, str | None]:
        for suffix in ("-system", "-user"):
            if name.endswith(suffix):
                return name[: -len(suffix)], suffix[1:]
        return name, None

    def _candidates(self, name: str) -> list[Path]:
        agent, kind = self._split(name)
        out: list[Path] = []
        if self.override_dir is not None:
            out.append(self.override_dir / f"{name}.md")
            if kind is not None:
                out.append(self.override_dir / agent / f"{kind}.md")
        if kind is not None:
            out.append(self.catalog_dir / agent / f"{kind}.md")
        out.append(self.catalog_dir / f"{name}.md")
        return out

    def load(self, name: str) -> str:
        if not name or "/" in name or "\\" in name or ".." in name or Path(name).is_absolute():
            raise PromptNotFound(f"Invalid prompt name {name!r} — must be a plain basename")
        for candidate in self._candidates(name):
            if candidate.is_file():
                return candidate.read_text()
        raise PromptNotFound(f"Prompt {name!r} not found in override {self.override_dir} or catalog {self.catalog_dir}")

    def load_optional(self, name: str) -> str | None:
        try:
            return self.load(name)
        except PromptNotFound:
            return None

    def load_agent_optional(self, name: str) -> Agent | None:
        """Resolve the agent-catalog properties for prompt/agent *name* (ADR-044 Phase 2).

        Mirrors :meth:`load`'s resolution roots — an operator-supplied
        ``<override_dir>/<name>/agent.yaml`` wins over the bundled
        ``<catalog_dir>/<name>/agent.yaml``. Returns ``None`` when neither
        exists (a non-catalog prompt / inline process step), so the
        property-derived ``commit`` posthook is simply a no-op there.

        Used by :func:`_resolve_jobs` (to fold ``commit`` in when
        ``produces_changes`` is true) and by
        :func:`_validate_posthook_property_consistency` (to reject an explicit
        ``commit`` on a non-producing agent). A malformed ``agent.yaml`` raises
        ``ValueError`` via ``_parse_agent`` — fail-loud at build time, matching
        the rest of the module.
        """
        from lotsa.agents import _parse_agent  # local import mirrors the validators' style

        if not name or "/" in name or "\\" in name or ".." in name or Path(name).is_absolute():
            return None
        roots = ([self.override_dir] if self.override_dir is not None else []) + [self.catalog_dir]
        for root in roots:
            candidate = root / name / "agent.yaml"
            if candidate.is_file():
                return _parse_agent(name, _yaml.safe_load(candidate.read_text()) or {})
        return None


JobType = Literal["agent", "action", "monitor"]


# ---------------------------------------------------------------------------
# Data model
# ---------------------------------------------------------------------------


@dataclass
class OutputRule:
    """Automatic output-based routing after an agent completes."""

    source: str
    pattern: str
    target: str = "next"


@dataclass
class PromotionInput:
    """A named input the promotion modal collects for a destination process.

    ADR-027 §4: a process declares the artifact name(s) its first step is
    prepared to read on promotion. The dashboard renders one form field per
    declared input and keys the operator's content into ``initial_artifacts``
    under ``name``.
    """

    name: str
    description: str


@dataclass
class Job:
    """Input/definition format for a single job in a process.

    Common fields:
        name:    used for files, display, prompts, event logs
        type:    "agent" (default), "action", or "monitor"
        config:  tool/engine-specific parameters; merged with flow-binding overrides

    ``agent`` fields:
        prompt:         prompt file prefix (defaults to ``name``). None = no agent
        resume:         --resume from stored session_id
        evaluate:       human evaluates output before advancing
        rules:          automatic output-based routing. May be declared directly
                        (``rules:`` — the escape hatch for file-source / raw-regex
                        patterns) OR via the ``routes:`` sugar (ADR-044 Phase 4),
                        a ``{OUTCOME: target}`` map that desugars into
                        ``^AGENT_RESULT: <OUTCOME>`` stdout rules at parse time.
                        A step declares one or the other, never both.
        conversational: chat-style step with iterative messages
        output:         artifact name this step produces
        inputs:         artifact names required before dispatch
        model:          per-step model override (ADR-022); falls back to the
                        global ``LotsaConfig.model`` when unset. Silently
                        ignored on ``action``/``monitor`` jobs.

    ``action`` fields:
        tool:    name registered via ``lotsa.registry.register_tool``

    ``monitor`` fields:
        engine:  name registered via ``lotsa.registry.register_engine``

    ADR-024 step posthooks (applies to ``agent`` steps):
        posthooks:      names of posthooks (registered via
                        ``lotsa.registry.register_posthook``) the orchestrator
                        runs after this agent step succeeds, before the
                        success-state transition. ``[commit]`` is the built-in.
        commit_prefix:  Conventional-Commits prefix for the ``commit`` posthook's
                        deterministic message (default ``chore``).

    ADR-044 Phase 3 step prehooks (applies to ``agent``/``action`` steps):
        prehooks:       names of prehooks (registered via
                        ``lotsa.registry.register_prehook``) the orchestrator
                        runs before this step dispatches. ``[worktree]`` is the
                        built-in. Usually left unset — ``worktree`` is *derived*
                        from the agent's ``needs_worktree`` property at
                        process-build time (opt-out: worktree is the universal
                        default; only a ``needs_worktree: false`` agent, e.g.
                        ``chat``, drops it). A per-binding ``prehooks:`` value
                        (including ``[]``) fully replaces the derived base.

    ADR-016 schema slots (parser only; write path deferred):
        output_file:  worktree-relative path to persist agent stdout to
        commit:       whether to commit ``output_file`` after the step

    NOTE — ``commit`` (ADR-016, a bool: "commit this step's ``output_file``")
    is **unrelated** to ``posthooks: [commit]`` (ADR-024, the orchestrator-run
    step posthook that stages + commits the whole worktree). Do not conflate
    them; they are independent fields with independent semantics.
    """

    name: str
    type: JobType = "agent"
    prompt: str | None = None
    resume: bool = False
    evaluate: bool = False
    rules: list[OutputRule] = field(default_factory=list)
    queue_state: str | None = None
    active_state: str | None = None
    gate_state: str | None = None
    conversational: bool = False
    output: str | None = None
    inputs: list[str] = field(default_factory=list)
    tool: str | None = None
    engine: str | None = None
    config: dict[str, Any] = field(default_factory=dict)
    output_file: str | None = None
    commit: bool = False
    posthooks: list[str] = field(default_factory=list)
    prehooks: list[str] = field(default_factory=list)
    commit_prefix: str | None = None
    model: str | None = None
    runner: str | None = None
    # ADR-017 soft-timeout indicator. Both optional; when set, the orchestrator
    # surfaces a yellow (warn) / red (over) dot once a step's elapsed time
    # crosses the threshold. Informational only — no auto-kill.
    timeout_warn_seconds: int | None = None
    timeout_kill_seconds: int | None = None


@dataclass
class ResolvedJob:
    """Runtime representation of a job — derived from a :class:`Job`."""

    name: str
    prompt_name: str
    resume_session: bool
    evaluate: bool
    queue_state: str
    active_state: str
    success_state: str
    type: JobType = "agent"
    rules: list[OutputRule] = field(default_factory=list)
    conversational: bool = False
    output: str | None = None
    inputs: list[str] = field(default_factory=list)
    tool: str | None = None
    engine: str | None = None
    config: dict[str, Any] = field(default_factory=dict)
    output_file: str | None = None
    commit: bool = False
    posthooks: list[str] = field(default_factory=list)
    prehooks: list[str] = field(default_factory=list)
    commit_prefix: str | None = None
    model: str | None = None
    runner: str | None = None
    # ADR-017 — see Job above; carried through to dispatch so the orchestrator
    # can read the active step's thresholds when computing ``timeout_status``.
    timeout_warn_seconds: int | None = None
    timeout_kill_seconds: int | None = None

    @property
    def job_type(self) -> str:
        """Backward compat alias — used as the ``job_type`` discriminator for events."""
        return self.name

    @property
    def is_approval_gate(self) -> bool:
        """Whether the operator can **Accept** this step to advance it.

        True for a step that produces an ``output`` artifact (e.g. spec), an
        ``evaluate`` gate (e.g. plan), or a *conversational* step with a forward
        (``next``) advance rule (e.g. verify). A plain conversational REPL
        (``chat``) has none of these — it is ended by promote/abandon, not
        Accept.

        Regression note: the Accept-on-chat fix narrowed the gate test to
        ``output or evaluate``, which also excluded verify (conversational, no
        output, not evaluate) and removed its Accept button. The
        ``conversational + next-rule`` clause restores verify without
        re-showing Accept on the rule-less chat REPL.
        """
        return (
            self.output is not None
            or self.evaluate
            or (self.conversational and any(r.target == "next" for r in self.rules))
        )


# Backward compat alias for the orchestrator's import surface.
FlowStep = ResolvedJob


@dataclass
class FlowBinding:
    """A single step in a flow — references a job, optionally overriding rules / config.

    A bare-string step (e.g. ``steps: [code, review]``) parses as a
    binding with ``rules=None`` (use job defaults) and ``config={}``. A
    dict-form step (``{name: review, rules: [...]}``) overrides those
    fields for this flow only — the underlying ``Job`` is unchanged.
    """

    name: str
    rules: list[OutputRule] | None = None
    config: dict[str, Any] = field(default_factory=dict)
    posthooks: list[str] | None = None
    prehooks: list[str] | None = None


@dataclass
class FlowConfig:
    """One named flow inside a process."""

    name: str
    state_machine: StateMachine
    jobs: list[ResolvedJob]
    bindings: list[FlowBinding]
    registry: PromptLoader
    gate_states: set[str] = field(default_factory=set)

    @property
    def steps(self) -> list[ResolvedJob]:
        """Backward compat alias — returns the bindings as ResolvedJobs in flow order."""
        by_name = {j.name: j for j in self.jobs}
        return [by_name[b.name] for b in self.bindings if b.name in by_name]

    def binding_for(self, job_name: str) -> FlowBinding | None:
        for b in self.bindings:
            if b.name == job_name:
                return b
        return None


@dataclass
class Process:
    """A loaded process — job catalog plus one or more named flows.

    ADR-027 catalog fields (both optional, both at the process root):
        description:       human text the chat agent's triage block renders so
                           it can match operator intent against the catalog.
        promotion_inputs:  artifact inputs the destination's first step reads on
                           promotion; the dashboard renders a form field each.
    Processes that omit them load unchanged (``None`` / empty list).

    ADR-044 Phase 4 — ``invocable`` declares *where* a workflow may be selected,
    driving the chat de-special-casing (option ii): a declared property replaces
    the hardcoded ``name == "chat"`` checks. Each entry is one of ``"start"``
    (offerable at task creation) / ``"hand-off"`` (offerable as a promotion
    destination). Omitting it defaults to both, so existing processes advertise
    everywhere unchanged; the bundled ``chat`` declares ``[start]`` only (a
    Think-phase entry point, never a hand-off target). This gates *advertising*
    (the picker + the chat agent's suggest-catalog), NOT enforcement — the
    hard "cannot promote into chat" rule was dropped (an operator may have a
    reason), amending ADR-027 §7.
    """

    name: str
    jobs: list[ResolvedJob]
    flows: dict[str, FlowConfig]
    registry: PromptLoader
    description: str | None = None
    promotion_inputs: list[PromotionInput] = field(default_factory=list)
    invocable: tuple[str, ...] = ("start", "hand-off")


# ---------------------------------------------------------------------------
# Bundled presets
# ---------------------------------------------------------------------------

PRESET_NAMES = ("chat", "build", "fix")


# ---------------------------------------------------------------------------
# YAML parsing
# ---------------------------------------------------------------------------


def _parse_promotion_inputs(raw: Any) -> list[PromotionInput]:
    """Parse the root-level ``promotion_inputs:`` block (ADR-027 §4).

    Each entry must be a mapping with ``name`` and ``description``. ``None``
    (the field omitted) yields an empty list, so existing processes load
    unchanged. Validation mirrors the field-level checks elsewhere in this
    module: a malformed entry fails loudly at build time rather than silently
    dropping the input.
    """
    if raw is None:
        return []
    if not isinstance(raw, list):
        raise ValueError(f"promotion_inputs must be a list, got {type(raw).__name__}")
    inputs: list[PromotionInput] = []
    for entry in raw:
        if not isinstance(entry, dict) or "name" not in entry or "description" not in entry:
            raise ValueError(f"Each promotion_inputs entry must have 'name' and 'description'; got {entry!r}")
        inputs.append(PromotionInput(name=entry["name"], description=entry["description"]))
    return inputs


def _parse_invocable(raw: Any) -> tuple[str, ...]:
    """Parse the root-level ``invocable:`` block (ADR-044 Phase 4).

    ``None`` (the field omitted) defaults to both options so existing processes
    are selectable everywhere unchanged. Otherwise it must be a list whose
    entries are each ``"start"`` or ``"hand-off"``; anything else fails loudly at
    build time, matching the field-level validators elsewhere in this module.
    """
    if raw is None:
        return ("start", "hand-off")
    if not isinstance(raw, list):
        raise ValueError(f"invocable must be a list, got {type(raw).__name__}")
    allowed = {"start", "hand-off"}
    for entry in raw:
        if entry not in allowed:
            raise ValueError(f"invocable entry {entry!r} must be one of {sorted(allowed)}")
    return tuple(raw)


def _parse_rules(raw: list[dict] | None) -> list[OutputRule] | None:
    if raw is None:
        return None
    return [
        OutputRule(
            source=r["source"],
            pattern=r["pattern"],
            target=r.get("target", "next"),
        )
        for r in raw
    ]


# ADR-044 Phase 4 — the canonical desugared pattern for an ``AGENT_RESULT:``
# outcome edge. ``routes: {FAILED: code}`` compiles to
# ``OutputRule("stdout", "^AGENT_RESULT: FAILED", "code")``; the gate-only
# derived ``FAILED → blocked`` default (in ``_resolve_jobs``) recognises an
# already-routed outcome by matching this exact pattern.
def _agent_result_pattern(outcome: str) -> str:
    return f"^AGENT_RESULT: {outcome}"


def _parse_routes(raw: Any, *, where: str) -> list[OutputRule] | None:
    """Desugar a ``routes:`` map (outcome → target) into stdout AGENT_RESULT rules.

    ``{PASSED: next, FAILED: code}`` becomes two ``OutputRule``s with the
    canonical ``^AGENT_RESULT: <OUTCOME>`` stdout patterns, preserving the
    declared (dict insertion) order. ``None`` (the key omitted) returns ``None``
    — distinct from ``[]`` — mirroring ``_parse_rules`` so "no routes declared"
    stays distinguishable from "declared empty". Keys must be in the closed
    :data:`~lotsa.agents.AGENT_OUTCOMES` vocabulary; an unknown key fails loudly
    at build time (naming ``where`` and the bad key), never silently dropping a
    routing edge.

    ``routes:`` is the concise sugar for the common stdout-``AGENT_RESULT`` case
    (ADR-044 Phase 4 — routing lives on the edge); ``rules:`` remains for the
    rare file-source / raw-regex case. A step declares one or the other, never
    both (guarded by the callers).
    """
    if raw is None:
        return None
    if not isinstance(raw, dict):
        raise ValueError(f"{where}: 'routes:' must be a mapping of outcome → target, got {type(raw).__name__}")
    rules: list[OutputRule] = []
    for outcome, target in raw.items():
        if outcome not in AGENT_OUTCOMES:
            raise ValueError(
                f"{where}: 'routes:' key {outcome!r} is not a valid AGENT_RESULT outcome; "
                f"allowed: {list(AGENT_OUTCOMES)}"
            )
        rules.append(OutputRule(source="stdout", pattern=_agent_result_pattern(outcome), target=str(target)))
    return rules


def _combine_routes_and_rules(
    routes: list[OutputRule] | None,
    rules: list[OutputRule] | None,
    *,
    where: str,
) -> list[OutputRule] | None:
    """Resolve a step's ``routes:`` / ``rules:`` into a single rule list.

    A step declares ``routes:`` OR ``rules:``, not both — the both-declared
    contradiction fails loudly at build time (the coexistence rule). Returns the
    desugared routes when present, else the parsed rules (which may itself be
    ``None`` = "unset", preserving the job-default fallback semantics that
    ``_resolve_jobs`` / the binding lookup rely on).
    """
    if routes is not None and rules is not None:
        raise ValueError(f"{where}: declare 'routes:' OR 'rules:', not both (routes is the sugar for the common case)")
    if routes is not None:
        return routes
    return rules


def _parse_job(jd: dict) -> Job:
    job_type: JobType = jd.get("type", "agent")
    if job_type not in ("agent", "action", "monitor"):
        raise ValueError(f"Unknown job type {job_type!r} for job {jd.get('name')!r}")

    if job_type == "action" and not jd.get("tool"):
        raise ValueError(f"Job {jd.get('name')!r} has type: action but no tool: <name> set")
    if job_type == "monitor" and not jd.get("engine"):
        raise ValueError(f"Job {jd.get('name')!r} has type: monitor but no engine: <name> set")

    commit = bool(jd.get("commit", False))
    output_file = jd.get("output_file")
    if commit and not output_file:
        raise ValueError(f"Job {jd.get('name')!r}: commit: true requires output_file: <path>")

    raw_inputs = jd.get("inputs", [])
    if isinstance(raw_inputs, str):
        raw_inputs = [raw_inputs]

    # ``posthooks`` accepts a list of names or a single bare string (sugar for
    # one posthook), mirroring the ``inputs`` shim above.
    raw_posthooks = jd.get("posthooks", []) or []
    if isinstance(raw_posthooks, str):
        raw_posthooks = [raw_posthooks]

    # ``prehooks`` accepts the same list-or-bare-string shape (ADR-044 Phase 3).
    raw_prehooks = jd.get("prehooks", []) or []
    if isinstance(raw_prehooks, str):
        raw_prehooks = [raw_prehooks]

    # ADR-044 Phase 4 — ``routes:`` is the concise sugar for the common
    # stdout-``AGENT_RESULT`` routing case; it desugars into the same
    # ``OutputRule`` list as ``rules:``. A step declares one or the other.
    parsed_rules = _combine_routes_and_rules(
        _parse_routes(jd.get("routes"), where=f"Job {jd.get('name')!r}"),
        _parse_rules(jd.get("rules")),
        where=f"Job {jd.get('name')!r}",
    )

    return Job(
        name=jd["name"],
        type=job_type,
        prompt=jd.get("prompt"),
        resume=jd.get("resume", False),
        evaluate=jd.get("evaluate", False),
        rules=parsed_rules or [],
        queue_state=jd.get("queue_state"),
        active_state=jd.get("active_state"),
        gate_state=jd.get("gate_state"),
        conversational=jd.get("conversational", False),
        output=jd.get("output"),
        inputs=list(raw_inputs),
        tool=jd.get("tool"),
        engine=jd.get("engine"),
        config=dict(jd.get("config", {})),
        output_file=output_file,
        commit=commit,
        posthooks=list(raw_posthooks),
        prehooks=list(raw_prehooks),
        commit_prefix=jd.get("commit_prefix"),
        model=jd.get("model"),
        runner=jd.get("runner"),
        timeout_warn_seconds=jd.get("timeout_warn_seconds"),
        timeout_kill_seconds=jd.get("timeout_kill_seconds"),
    )


def _parse_flow_step(raw: Any) -> FlowBinding:
    if isinstance(raw, str):
        return FlowBinding(name=raw, rules=None, config={})
    if isinstance(raw, dict):
        # ``posthooks`` defaults to None (= "use the job's posthooks") so a
        # binding-level empty list is distinguishable from "unset" and can
        # override the job to run no posthooks. A bare string is sugar for a
        # one-posthook list, matching ``_parse_job``.
        raw_posthooks = raw.get("posthooks")
        if isinstance(raw_posthooks, str):
            raw_posthooks = [raw_posthooks]
        # ``prehooks`` mirrors ``posthooks``: default None (= "use the job's
        # derived prehooks") so a binding-level empty list is distinguishable
        # from "unset" and can override the step to run no prehooks. A bare
        # string is sugar for a one-prehook list.
        raw_prehooks = raw.get("prehooks")
        if isinstance(raw_prehooks, str):
            raw_prehooks = [raw_prehooks]
        # ADR-044 Phase 4 — a per-flow binding may override routing with the
        # ``routes:`` sugar too (same one-or-the-other rule as the job level).
        # ``None`` still means "use the job's rules" (lookup-then-fallback).
        binding_rules = _combine_routes_and_rules(
            _parse_routes(raw.get("routes"), where=f"Flow step {raw['name']!r}"),
            _parse_rules(raw.get("rules")),
            where=f"Flow step {raw['name']!r}",
        )
        return FlowBinding(
            name=raw["name"],
            rules=binding_rules,
            config=dict(raw.get("config", {})),
            posthooks=list(raw_posthooks) if raw_posthooks is not None else None,
            prehooks=list(raw_prehooks) if raw_prehooks is not None else None,
        )
    raise ValueError(f"Bad flow step: {raw!r}")


def _load_yaml_process(path: Path) -> tuple[str, list[Job], dict[str, list[FlowBinding]], dict]:
    """Load a process from YAML.

    Returns (name, jobs, flows, raw_data).
    """
    data = _yaml.safe_load(path.read_text())
    if not isinstance(data, dict):
        raise ValueError(f"Invalid process file: {path}")

    if "pr" in data:
        raise ValueError(
            "The 'pr:' block is no longer supported. Replace it with a monitor "
            "job, e.g.:\n"
            "  - name: wait_for_pr_signal\n"
            "    type: monitor\n"
            "    engine: pr_monitor\n"
            "    config:\n"
            "      poll_interval_seconds: 30\n"
            "      debounce_seconds: 120\n"
            "and reference it in your flow's steps. If you relied on an explicit "
            "'base_branch' under the old 'pr:' block, set it under the 'push_pr' "
            "action job's 'config:' block — otherwise the push falls through to "
            "GitHub's default-branch resolution. See docs/adr/ADR-014.md."
        )

    if "jobs" not in data:
        raise ValueError(f"Invalid process file: {path} (must have 'jobs' key)")

    # Accept ``process:`` (new) or ``name:`` (legacy bridge during transition)
    name = data.get("process", data.get("name", path.stem))

    jobs = [_parse_job(jd) for jd in data["jobs"]]

    flows_raw = data.get("flows")
    flows: dict[str, list[FlowBinding]] = {}
    if flows_raw is None:
        # Backward compat: if no flows: block, synthesise a "main" flow from
        # the job list in declaration order. Keeps the minimal simple/standard
        # YAML shape valid.
        flows["main"] = [FlowBinding(name=j.name) for j in jobs]
    else:
        if not isinstance(flows_raw, dict):
            raise ValueError(f"Invalid flows block in {path}: must be a mapping")
        for flow_name, flow_body in flows_raw.items():
            if not isinstance(flow_body, dict) or "steps" not in flow_body:
                raise ValueError(f"Flow {flow_name!r} in {path} must have a 'steps:' list")
            flows[flow_name] = [_parse_flow_step(s) for s in flow_body["steps"]]

    return name, jobs, flows, data


# ---------------------------------------------------------------------------
# State derivation
# ---------------------------------------------------------------------------


def _resolve_jobs(
    jobs: list[Job],
    bindings: list[FlowBinding],
    resolve_agent: Callable[[str], Agent | None] | None = None,
) -> tuple[list[ResolvedJob], set[str]]:
    """Resolve job states for a flow's binding order.

    success_state derivation only makes sense in the context of a flow's
    ordering (because "next" means "next in this flow"). Each call resolves
    states for one flow's view of the jobs.

    ``resolve_agent`` (ADR-044 Phase 2) maps a job's prompt/agent name to its
    catalog :class:`~lotsa.agents.Agent` (or ``None``). When supplied, an agent
    job whose agent declares ``produces_changes: true`` has the built-in
    ``commit`` posthook folded into its base posthooks — the property is the
    single source of truth, replacing a hand-declared ``posthooks: [commit]``.
    A per-binding ``posthooks:`` override still fully replaces the step
    (derivation only touches the no-override base).

    ``resolve_agent`` also drives the ADR-044 Phase 3 ``worktree`` prehook
    derivation, but with the *inverse* polarity to commit: worktree is the
    pre-existing universal default for every dispatched step, so it is derived
    onto every agent/action step EXCEPT the one opt-out — an agent whose
    ``needs_worktree`` is false (today: only ``chat``). Monitor jobs never
    created a worktree at dispatch, so they derive none. Deriving worktree
    opt-in (like commit) would strip it from ``push_pr`` (action),
    ``resolve_conflicts``, and inline agent steps with no ``agent.yaml`` — a
    regression. A per-binding ``prehooks:`` override fully replaces the step.
    """
    by_name = {j.name: j for j in jobs}
    binding_names = [b.name for b in bindings]
    resolved: list[ResolvedJob] = []
    gate_states: set[str] = set()

    for idx, binding in enumerate(bindings):
        if binding.name not in by_name:
            raise ValueError(f"Flow step {binding.name!r} references unknown job")
        job = by_name[binding.name]
        is_last = idx == len(bindings) - 1

        if job.queue_state is not None:
            queue = job.queue_state
        elif idx == 0 and job.type == "agent":
            queue = "backlog"
        else:
            queue = job.name
        active = job.active_state if job.active_state is not None else job.name
        prompt_name = job.prompt if job.prompt is not None else job.name

        # Monitor jobs collapse queue/active into a single state.
        if job.type == "monitor":
            queue = job.queue_state if job.queue_state is not None else job.name
            active = queue

        if is_last:
            success = "complete"
        elif job.evaluate:
            gate = job.gate_state if job.gate_state is not None else f"{job.name}d"
            gate_states.add(gate)
            success = gate
        else:
            next_job = by_name[binding_names[idx + 1]]
            next_queue = next_job.queue_state if next_job.queue_state is not None else next_job.name
            success = next_queue

        effective_rules = binding.rules if binding.rules is not None else list(job.rules)
        # ADR-044 Phase 4 — derived ``FAILED → blocked`` default, scoped to
        # AUTO-ROUTING GATE steps. A gate that renders a ``FAILED`` verdict with
        # no rule for it would otherwise auto-advance (the drainer's implicit "no
        # match → next"), silently passing a failed gate. The ADR default-route
        # table says ``FAILED → blocked``; fold it in when the resolved agent is
        # a gate declaring ``FAILED`` and nothing already routes that outcome.
        # Purely additive: every bundled gate routes ``FAILED`` explicitly, so
        # this changes no current behaviour — it is a safety net for a future
        # bare gate. Keyed on the canonical desugared pattern so a raw custom
        # ``FAILED`` pattern (the ``rules:`` escape hatch) still counts as
        # routed. Same lookup-then-fallback shape as the commit/worktree
        # derivations below; a per-binding override fully replaces the step, so
        # the derivation only touches the effective (already-resolved) set.
        #
        # Two exclusions keep the default *additive* — it only completes a
        # partial marker-routing table, never imposes routing on a step that
        # opted out of markers:
        #   * ``evaluate`` gates park for human approval and never auto-route, so
        #     a derived routing rule is both moot and harmful.
        #   * a step with NO effective rules opted out of marker routing entirely
        #     (it auto-advances on any output); leave it alone. Deriving here
        #     would make the step rule-bearing, so the drainer's "no recognized
        #     marker → block" guard would flip a previously auto-advancing gate
        #     to ``blocked`` on non-marker output — a behaviour change, not a
        #     safety net. The default therefore only fires for a gate that
        #     already routes at least one outcome (e.g. ``PASSED``) but is
        #     missing ``FAILED`` — the "forgot a rule" case the ADR guards.
        if job.type == "agent" and not job.evaluate and effective_rules and resolve_agent is not None:
            agent = resolve_agent(job.prompt if job.prompt is not None else job.name)
            if agent is not None and agent.is_gate and "FAILED" in agent.outcomes:
                routed_failed = any(
                    r.source == "stdout" and r.pattern == _agent_result_pattern("FAILED") for r in effective_rules
                )
                if not routed_failed:
                    effective_rules = [
                        *effective_rules,
                        OutputRule(source="stdout", pattern=_agent_result_pattern("FAILED"), target="blocked"),
                    ]
        # Per-binding posthooks override the job's default; ``None`` (unset)
        # falls back to the job's base. Same lookup-then-fallback shape as rules.
        if binding.posthooks is not None:
            effective_posthooks = list(binding.posthooks)
        else:
            # ADR-044 Phase 2: the agent's ``produces_changes`` property is the
            # single source of truth for "this step's output needs committing".
            # Fold the built-in ``commit`` into the job's base posthooks when
            # the resolved agent produces changes. Binding overrides (handled
            # above) fully replace the step, so derivation never touches them.
            effective_posthooks = list(job.posthooks)
            if job.type == "agent" and resolve_agent is not None:
                agent = resolve_agent(job.prompt if job.prompt is not None else job.name)
                if agent is not None and agent.produces_changes and "commit" not in effective_posthooks:
                    effective_posthooks.append("commit")

        # Per-binding prehooks override the job's default; ``None`` (unset)
        # falls back to the derived base. Same lookup-then-fallback shape.
        if binding.prehooks is not None:
            effective_prehooks = list(binding.prehooks)
        else:
            # ADR-044 Phase 3: worktree creation is the pre-existing universal
            # default for every dispatched step (agent + action). The SOLE
            # opt-out is an agent step whose agent declares ``needs_worktree:
            # false`` (today: only ``chat``). Monitor jobs never dispatch a
            # worktree — they are excluded (and never reach the orchestrator's
            # prehook site). This is deliberately the INVERSE of the opt-in
            # commit derivation above (see the method docstring).
            effective_prehooks = list(job.prehooks)
            if job.type != "monitor" and "worktree" not in effective_prehooks:
                opts_out = False
                if job.type == "agent" and resolve_agent is not None:
                    agent = resolve_agent(job.prompt if job.prompt is not None else job.name)
                    if agent is not None and not agent.needs_worktree:
                        opts_out = True
                if not opts_out:
                    effective_prehooks.append("worktree")

        resolved.append(
            ResolvedJob(
                name=job.name,
                prompt_name=prompt_name,
                resume_session=job.resume,
                evaluate=job.evaluate,
                queue_state=queue,
                active_state=active,
                success_state=success,
                type=job.type,
                rules=list(effective_rules),
                conversational=job.conversational,
                output=job.output,
                inputs=list(job.inputs),
                tool=job.tool,
                engine=job.engine,
                config=dict(job.config) | dict(binding.config),
                output_file=job.output_file,
                commit=job.commit,
                posthooks=list(effective_posthooks),
                prehooks=list(effective_prehooks),
                commit_prefix=job.commit_prefix,
                model=job.model,
                runner=job.runner,
                timeout_warn_seconds=job.timeout_warn_seconds,
                timeout_kill_seconds=job.timeout_kill_seconds,
            )
        )

    return resolved, gate_states


def _build_state_machine(
    resolved: list[ResolvedJob],
    gate_states: set[str],
) -> StateMachine:
    """Derive a StateMachine from resolved jobs — no synthetic states.

    Cross-flow rule targets (jobs not in ``resolved``) are intentionally
    skipped here and stitched in by ``_register_cross_flow_edges`` after
    every flow has been resolved. That pass has access to every flow's
    ResolvedJob queue_state, which is the only source of truth for
    cross-flow target queue states (a job's derived queue_state depends
    on its position in its owning flow's bindings, not on the raw Job).
    """
    states: set[str] = {"complete", "blocked", "abandoned"}
    transitions: dict[tuple[str, str], TransitionRule] = {}

    for rj in resolved:
        states.update({rj.queue_state, rj.active_state, rj.success_state})

        if rj.type == "monitor":
            # Monitor states have no dispatch transitions out — the engine
            # drives them. Only the active→blocked edge is needed so
            # block() and the engine can route there on errors.
            transitions[(rj.queue_state, "blocked")] = TransitionRule()
            # Allow engines to transition to terminal states.
            transitions[(rj.queue_state, "complete")] = TransitionRule()
            transitions[(rj.queue_state, "abandoned")] = TransitionRule()
            continue

        transitions[(rj.queue_state, rj.active_state)] = TransitionRule()
        transitions[(rj.active_state, rj.success_state)] = TransitionRule()
        transitions[(rj.active_state, "blocked")] = TransitionRule()
        transitions[(rj.queue_state, "blocked")] = TransitionRule()
        transitions[(rj.active_state, rj.active_state)] = TransitionRule()

    for i, rj in enumerate(resolved):
        if rj.success_state in gate_states and i + 1 < len(resolved):
            next_rj = resolved[i + 1]
            transitions[(rj.success_state, next_rj.queue_state)] = TransitionRule()

    all_queue_states = {rj.name: rj.queue_state for rj in resolved}
    # Intra-flow rule targets — same flow, ResolvedJob queue_state is
    # canonical. Cross-flow targets (e.g. pr_fix's SKIPPED rule pointing at
    # ``wait_for_pr_signal`` which only appears in main) are handled later
    # in ``_register_cross_flow_edges`` because their correct queue_state
    # depends on the target flow's binding order — a first-agent binding
    # derives ``"backlog"``, while the same job elsewhere derives
    # ``job.name``. ``_build_state_machine`` runs per-flow without
    # visibility into other flows' bindings, so it cannot mirror that
    # derivation correctly; the stitching pass has both flows resolved
    # and uses each ResolvedJob's actual queue_state.
    for rj in resolved:
        for rule in rj.rules:
            if rule.target in ("next", "blocked"):
                continue
            target_queue = all_queue_states.get(rule.target)
            if target_queue is None:
                continue  # cross-flow — stitched by _register_cross_flow_edges
            transitions[(rj.active_state, target_queue)] = TransitionRule()

    for rj in resolved:
        if rj.success_state in gate_states:
            transitions[(rj.success_state, rj.active_state)] = TransitionRule()

    for gate in gate_states:
        transitions[(gate, "blocked")] = TransitionRule()

    states.update(gate_states)

    initial = resolved[0].queue_state if resolved else "backlog"
    return StateMachine(
        states=sorted(states),
        transitions=transitions,
        initial_state=initial,
    )


# ---------------------------------------------------------------------------
# Output rule evaluation
# ---------------------------------------------------------------------------


# Leading markdown noise an agent may put before a line-anchored routing
# marker: a heading prefix (``#``..``######`` + space) and/or tight inline
# wrappers (inline code, bold, italic, strikethrough). Stripped in
# ``_match_marker``'s fallback so ``## AGENT_RESULT: PASSED`` (an internal task),
# ``\`AGENT_RESULT: PASSED\``` (an internal task), and ``**AGENT_RESULT: PASSED**`` all route.
# Deliberately narrow on the wrapper side: a wrapper run must abut the text
# with NO space, so a bullet quoting a marker mid-document ("* `AGENT_RESULT:` is
# emitted when …" — ``*`` then a space) cannot false-match. The heading
# alternative DOES consume its trailing space (``## `` → ``AGENT_RESULT:``) because
# a ``#``-prefixed line is unambiguously a heading, not a list item.
_MARKER_WRAPPER_RE = re.compile(r"^(?:#{1,6}[ \t]+|[`*_~]{1,3})+")


def _match_marker(pattern: str, content: str) -> int | None:
    """Find *pattern* in *content*; return the match's char offset or None.

    Tries the raw multiline regex first (status quo). On a miss, retries
    line-by-line with tight leading markdown wrappers stripped, so
    line-anchored markers route regardless of agent typography. Returns
    the offset of the (original) line start in the fallback case.
    """
    try:
        match = re.search(pattern, content, re.MULTILINE)
    except re.error:
        return None
    if match:
        return match.start()
    offset = 0
    for line in content.splitlines(keepends=True):
        stripped = _MARKER_WRAPPER_RE.sub("", line, count=1)
        if stripped != line and re.search(pattern, stripped):
            return offset
        offset += len(line)
    return None


def check_conversational_rules(step: ResolvedJob, stdout: str) -> str | None:
    for rule in step.rules:
        if rule.source != "stdout":
            continue
        start = _match_marker(rule.pattern, stdout)
        if start is not None:
            return stdout[start:].strip()
    return None


def evaluate_output_rules(
    rules: list[OutputRule],
    result: AgentResult,
    work_dir: Path,
) -> str | None:
    for rule in rules:
        if rule.source == "stdout":
            content = result.stdout or ""
        else:
            path = (work_dir / rule.source).resolve()
            if not path.is_relative_to(work_dir.resolve()):
                continue
            if not path.exists():
                continue
            content = path.read_text()
        if _match_marker(rule.pattern, content) is not None:
            return rule.target
    return None


def resolve_output_target(
    target: str,
    job: ResolvedJob,
    flow: FlowConfig,
) -> str:
    """Convert a rule target string to a concrete state.

    ``target`` is one of: ``"next"``, ``"blocked"``, or a job name in the
    currently-active *flow*. The old ``target: previous`` shorthand was
    removed in ADR-014 Layer A — any unrecognized target string resolves
    to ``"blocked"`` with a warning.
    """
    if target == "next":
        # The same Job appears in different flows with different success_states
        # (because ordering changes "next"). Look up the job within THIS flow
        # so cross-flow callers get the flow-specific next state.
        for rj in flow.jobs:
            if rj.name == job.name:
                return rj.success_state
        return job.success_state
    if target == "blocked":
        return "blocked"
    if target == "complete":
        return "complete"
    for rj in flow.jobs:
        if rj.name == target:
            return rj.queue_state
    # Cross-flow rule targets other than the orchestrator's SKIPPED→monitor
    # short-circuit resolve to ``"blocked"`` here. The drainer's
    # ``AGENT_RESULT: SKIPPED`` branch handles the one cross-flow handoff the
    # bundled processes use (sub-flow → host monitor) by short-circuiting
    # the rule resolver and routing back to the parent flow's monitor by
    # name. Any *other* custom rule that names a job belonging to a sibling
    # flow (e.g. a sub-flow rule targeting a main-flow job directly) lands
    # in this fallback: log warning, route to ``blocked``, no SM rejection.
    # Supporting generic cross-flow targets would require lookup against
    # ``process.jobs`` here. No bundled process needs it; if a custom
    # process does, the fix lives at this site.
    logger.warning(
        "Output rule target %r not found in flow %r (job %r) — routing to blocked",
        target,
        flow.name,
        job.name,
    )
    return "blocked"


# ---------------------------------------------------------------------------
# Public API
# ---------------------------------------------------------------------------


def _register_cross_flow_edges(flows: dict[str, FlowConfig]) -> None:
    """Stitch sub-flow entry/exit edges into each flow's state machine.

    The orchestrator references a single ``self.flow.state_machine`` for
    every CAS check, but tasks routinely cross flow boundaries (e.g. the
    pr_monitor engine dispatches from ``main``'s ``wait_for_pr_signal`` into
    the ``pr_fix`` sub-flow, and pr-fix's SKIPPED rule routes back). Each
    flow's per-flow SM only knows its own bindings, so without this pass:

    * sub-flow entry (monitor_state, sub_first.queue_state) is missing →
      ``_dispatch_step``'s pre-CAS transition check rejects the dispatch
      and the task stalls at ``status=working`` until the next restart.
    * sub-flow exit (sub_step.active_state, host_job.queue_state) is missing
      → the drainer's post-rule CAS check rejects the routing (e.g.
      pr-fix SKIPPED → wait_for_pr_signal, pr-fix COMPLETED → reviewing).

    The fix is to mutate the underlying ``StateMachine`` after construction:
    register the missing edges and add any newly-referenced states. This is
    safe because ``StateMachine`` exposes both ``states`` and ``transitions``
    as live (uncopied) mutable references via its public properties — no
    immutable invariants beyond the construction-time validation, which we
    satisfy by adding every referenced state.
    """
    if len(flows) < 2:
        return  # No cross-flow boundaries to stitch.

    for flow_name, flow in flows.items():
        sm_states = flow.state_machine.states
        sm_trans = flow.state_machine.transitions

        # (a) Sub-flow entry: every monitor in this flow can dispatch into
        # any other flow's first step. Register (monitor_state, first.queue)
        # in BOTH this flow's SM (the source of the dispatch CAS) AND the
        # destination sub-flow's SM. The second registration is required
        # because ``_dispatch_pr_fix_locked`` sets ``current_flow=sub_flow``
        # in metadata BEFORE calling ``_dispatch_step``; the latter then
        # resolves ``active_flow`` via ``_resolve_flow(item)`` and validates
        # the pre-CAS transition against the sub-flow's SM, not the host
        # flow's. Without the edge registered there, the guard rejects the
        # dispatch silently and the task stalls at status=working until the
        # next server restart flips it to blocked.
        monitors = [rj for rj in flow.jobs if rj.type == "monitor"]
        for other_name, other in flows.items():
            if other_name == flow_name or not other.bindings:
                continue
            # Register entry edges for ALL sub-flow bindings, not only bindings[0].
            # Phase 2 (ADR-015) dispatches resolve_conflicts (bindings[1]) from
            # the monitor state when a merge conflict is detected — without
            # registering its entry edge here, _dispatch_step's pre-CAS guard
            # rejects the (wait_for_pr_signal, resolving_conflicts) transition
            # and the task stalls at status=working.
            for binding in other.bindings:
                bound_job = next((rj for rj in other.jobs if rj.name == binding.name), None)
                if bound_job is None:
                    continue
                sm_states.add(bound_job.queue_state)
                sm_states.add(bound_job.active_state)
                for m in monitors:
                    sm_trans[(m.queue_state, bound_job.queue_state)] = TransitionRule()
                    # Mirror into the sub-flow's SM (see docstring above).
                    other.state_machine.states.add(m.queue_state)
                    other.state_machine.transitions[(m.queue_state, bound_job.queue_state)] = TransitionRule()

        # (b) Sub-flow exit: any other flow's job whose rule targets a job
        # in THIS flow needs (other.active_state, this.queue_state) registered
        # in THIS flow's SM so the drainer's main-flow CAS can land it.
        # Also mirror the same edge into the SOURCE (other) flow's SM —
        # ``_build_state_machine`` skips cross-flow rule targets entirely
        # because their correct queue_state depends on the target flow's
        # binding order (first-agent → ``"backlog"``, otherwise
        # ``job.name``), which is not visible in the per-flow build. The
        # source flow's SM still needs the edge so the drainer's pre-CAS
        # transition check against the source's ``state_machine`` can
        # land the routing decision.
        my_names = {rj.name: rj for rj in flow.jobs}
        for other_name, other in flows.items():
            if other_name == flow_name:
                continue
            for rj in other.jobs:
                for rule in rj.rules:
                    if rule.target in ("next", "blocked", "complete", "abandoned"):
                        continue
                    target = my_names.get(rule.target)
                    if target is None:
                        continue
                    sm_states.add(rj.active_state)
                    sm_states.add(target.queue_state)
                    sm_trans[(rj.active_state, target.queue_state)] = TransitionRule()
                    # The other-flow step may also need to terminate at
                    # blocked from this flow's perspective (pr-fix FAILED → blocked).
                    sm_trans[(rj.active_state, "blocked")] = TransitionRule()
                    # And needs the self-loop for retry on its own active state.
                    sm_trans[(rj.active_state, rj.active_state)] = TransitionRule()
                    # Mirror into the source flow's SM so the drainer's
                    # pre-CAS check against ``other.state_machine`` (when
                    # the task is resolved to the source flow) also sees
                    # the edge.
                    other.state_machine.states.add(target.queue_state)
                    other.state_machine.transitions[(rj.active_state, target.queue_state)] = TransitionRule()

    # (c) Sub-flow terminal exit: a sub-flow's last binding naturally resolves
    # to success_state="complete" via _resolve_jobs, but operationally the
    # sub-flow's terminal step should return to the host flow's monitor (the
    # one that dispatched it) — not mark the task complete. Register
    # ``(last_binding.active_state, host_monitor.queue_state)`` in the
    # sub-flow's SM so the drainer / action-step pre-CAS check sees a valid
    # transition. ``_execute_action_step`` overrides ``success_state`` to
    # the host monitor's queue_state when running in a sub-flow with a
    # terminal "complete", matching this edge.
    #
    # Today there is exactly one host flow (``main``) and at most one host
    # monitor, so the host lookup is unambiguous. A future multi-host
    # topology (e.g. two distinct monitors that both dispatch into the same
    # sub-flow, or sub-flows hosted by another sub-flow) would need to
    # record the dispatching monitor at sub-flow entry so this resolver
    # could pick the correct return target. No bundled process needs it.
    main_flow = flows.get("main") or next(iter(flows.values()))
    host_monitor = next((rj for rj in main_flow.jobs if rj.type == "monitor"), None)
    if host_monitor is not None:
        for flow in flows.values():
            if flow is main_flow or not flow.bindings:
                continue
            last_binding = flow.bindings[-1]
            last_job = next((rj for rj in flow.jobs if rj.name == last_binding.name), None)
            if last_job is None:
                continue
            flow.state_machine.states.add(host_monitor.queue_state)
            flow.state_machine.transitions[(last_job.active_state, host_monitor.queue_state)] = TransitionRule()


def _validate_rule_targets(jobs: list[Job], flow_bindings: dict[str, list[FlowBinding]]) -> None:
    """Raise ``ValueError`` if any output-rule target names a job outside this
    process (ADR-021 R6).

    A rule ``target:`` may be a recognized routing keyword or the name of a job
    in THIS process — every flow in a process shares the same job catalog, so a
    target that resolves to none of those can only be a cross-process reference
    (or a typo). Cross-process dispatch is unsupported: a sub-flow rule may only
    route within its own process. Failing here at parse time turns what used to
    be a silent ``resolve_output_target`` → ``blocked`` fallback (with only a
    runtime warning) into a loud build-time error, naming the offending rule.

    Both rule surfaces are checked: the job-level default ``rules:`` AND the
    per-flow binding override ``rules:`` (``{name: review, rules: [...]}``).
    The override rules ARE the "sub-flow rules" R6 names — sub-flow routing
    (e.g. ``pr_fix.review`` FAILED → pr-fix) lives in binding overrides,
    not the job defaults, so validating only ``Job.rules`` would let a
    cross-process sub-flow target slip straight through to the runtime
    ``blocked`` fallback.

    Recognized non-job keywords: ``next`` (success edge), the terminal states
    ``blocked`` / ``complete`` / ``abandoned``, and ``needs_input`` — the last
    is special-cased in the completion drainer's ``AGENT_RESULT: INPUT`` path
    (e.g. the bundled ``build`` process's ``pr-fix`` rule routes to it), so it
    is a legitimate target even though it is not a job.
    """
    sentinels = {"next", "blocked", "complete", "abandoned", "needs_input"}
    job_names = {j.name for j in jobs}

    def _check(target: str, where: str) -> None:
        if target in sentinels or target in job_names:
            return
        raise ValueError(
            f"{where} has an output rule whose target {target!r} "
            f"does not resolve to a job in this process (jobs: {sorted(job_names)}). "
            "Cross-process dispatch is unsupported — inline the step into this "
            "process, or restructure so the target is a job/flow within it."
        )

    for j in jobs:
        for rule in j.rules:
            _check(rule.target, f"Job {j.name!r}")
    for flow_name, bindings in flow_bindings.items():
        for binding in bindings:
            for rule in binding.rules or ():
                _check(rule.target, f"Flow {flow_name!r} step {binding.name!r}")


def _validate_registry_references(jobs: list[Job]) -> None:
    """Raise ``ValueError`` if any job references an unregistered tool/engine.

    Runs at the end of ``build_process`` so the YAML-declared registry
    references are checked once. Built-in tools/engines self-register at
    import time (``lotsa.tools.__init__`` registers ``push_pr``;
    ``lotsa.engines.__init__`` registers ``pr_monitor``); we import them
    here so that direct ``build_process`` callers (tests, custom entry
    points) see the same registry the runtime path sees without having to
    import the packages themselves. User-supplied entries from
    ``lotsa.yaml``'s ``tools:`` / ``engines:`` blocks are registered by
    ``OrchestratorService.start()`` BEFORE it calls this function, so any
    name still missing here is unambiguously a typo or a missing
    ``lotsa.yaml`` entry.

    The error message lists the registered names so an operator can
    immediately spot a typo (e.g. ``tool: pust_pr``) without spelunking
    the orchestrator log to discover what was actually registered.
    """
    # Trigger built-in registration as a side-effect of import. ``noqa``
    # because the import itself is the side-effect we want.
    import lotsa.engines  # noqa: F401
    import lotsa.tools  # noqa: F401
    from lotsa.registry import (
        is_engine_registered,
        is_tool_registered,
        list_engines,
        list_tools,
    )

    for j in jobs:
        if j.type == "action" and j.tool and not is_tool_registered(j.tool):
            raise ValueError(
                f"Job {j.name!r} references unknown tool {j.tool!r}. "
                f"Registered tools: {list_tools()}. "
                "Register the tool via the ``tools:`` block in lotsa.yaml, "
                "or check for a typo."
            )
        if j.type == "monitor" and j.engine and not is_engine_registered(j.engine):
            raise ValueError(
                f"Job {j.name!r} references unknown engine {j.engine!r}. "
                f"Registered engines: {list_engines()}. "
                "Register the engine via the ``engines:`` block in lotsa.yaml, "
                "or check for a typo."
            )


def _validate_posthook_references(jobs: list[Job], flow_bindings: dict[str, list[FlowBinding]]) -> None:
    """Raise ``ValueError`` if any job/binding references an unregistered posthook.

    Mirrors ``_validate_registry_references``: imports ``lotsa.posthooks`` for
    its self-registration side-effect so direct ``build_process`` callers
    (tests, custom entry points) see the same built-in registry the runtime
    path sees, then checks every referenced posthook name — across both
    per-job ``posthooks:`` and per-binding ``posthooks:`` overrides — against
    the registry. An unknown name is unambiguously a typo, so failing here at
    build time beats failing at dispatch time.
    """
    import lotsa.posthooks  # noqa: F401 — import side-effect registers built-ins
    from lotsa.registry import is_posthook_registered, list_posthooks

    referenced: set[str] = set()
    for j in jobs:
        referenced.update(j.posthooks)
    for bindings in flow_bindings.values():
        for b in bindings:
            if b.posthooks:
                referenced.update(b.posthooks)

    for name in sorted(referenced):
        if not is_posthook_registered(name):
            raise ValueError(
                f"Process references unknown posthook {name!r}. "
                f"Registered posthooks: {list_posthooks()}. "
                "Register the posthook via ``lotsa.registry.register_posthook``, "
                "or check for a typo."
            )


def _validate_posthook_property_consistency(
    jobs: list[Job],
    flow_bindings: dict[str, list[FlowBinding]],
    resolve_agent: Callable[[str], Agent | None],
) -> None:
    """Fail loud if a step explicitly declares ``commit`` on an agent whose
    catalog property says it produces no changes (ADR-044 Phase 2).

    ``commit`` is now *derived* from the agent's ``produces_changes`` property,
    so an explicit ``commit`` on a non-producing agent (e.g. a gate) is drift —
    the exact duplication Phase 2 removes. This guard turns a future re-drift
    into a build-time error rather than a silently-contradictory config, the
    same way ``_validate_posthook_references`` fails an unknown posthook name.

    Only the contradiction direction is checked. A producing agent explicitly
    listing ``commit`` is redundant-but-consistent (the derivation union dedups
    it), and a binding ``posthooks: []`` suppressing a producer's commit is the
    documented override seam — neither is flagged. Non-agent jobs (which never
    run posthooks) and prompts with no ``agent.yaml`` are skipped.
    """
    by_name = {j.name: j for j in jobs}

    def _check(job: Job, posthooks: list[str], where: str) -> None:
        if job.type != "agent" or "commit" not in posthooks:
            return
        agent = resolve_agent(job.prompt if job.prompt is not None else job.name)
        if agent is not None and not agent.produces_changes:
            raise ValueError(
                f"{where} explicitly declares the ``commit`` posthook, but its agent "
                f"({job.prompt or job.name!r}) has ``produces_changes: false``. "
                "commit is derived from ``produces_changes`` (ADR-044 Phase 2) — drop the "
                "explicit posthook, or set ``produces_changes: true`` in the agent's "
                "agent.yaml if it genuinely writes changes."
            )

    for j in jobs:
        _check(j, j.posthooks, f"Job {j.name!r}")
    for flow_name, bindings in flow_bindings.items():
        for b in bindings:
            if b.posthooks:
                job = by_name.get(b.name)
                if job is not None:
                    _check(job, b.posthooks, f"Flow {flow_name!r} step {b.name!r}")


def _validate_prehook_references(jobs: list[Job], flow_bindings: dict[str, list[FlowBinding]]) -> None:
    """Raise ``ValueError`` if any job/binding references an unregistered prehook.

    Mirrors ``_validate_posthook_references``: imports ``lotsa.prehooks`` for
    its self-registration side-effect so direct ``build_process`` callers
    (tests, custom entry points) see the same built-in registry (``worktree``)
    the runtime path sees, then checks every referenced prehook name — across
    both per-job ``prehooks:`` and per-binding ``prehooks:`` overrides — against
    the registry. An unknown name is unambiguously a typo, so failing here at
    build time beats failing at dispatch time.
    """
    import lotsa.prehooks  # noqa: F401 — import side-effect registers built-ins
    from lotsa.registry import is_prehook_registered, list_prehooks

    referenced: set[str] = set()
    for j in jobs:
        referenced.update(j.prehooks)
    for bindings in flow_bindings.values():
        for b in bindings:
            if b.prehooks:
                referenced.update(b.prehooks)

    for name in sorted(referenced):
        if not is_prehook_registered(name):
            raise ValueError(
                f"Process references unknown prehook {name!r}. "
                f"Registered prehooks: {list_prehooks()}. "
                "Register the prehook via ``lotsa.registry.register_prehook``, "
                "or check for a typo."
            )


def _validate_prehook_property_consistency(
    jobs: list[Job],
    flow_bindings: dict[str, list[FlowBinding]],
    resolve_agent: Callable[[str], Agent | None],
) -> None:
    """Fail loud if a step explicitly declares ``worktree`` on an agent whose
    catalog property says it needs no worktree (ADR-044 Phase 3).

    ``worktree`` is *derived* from the agent's ``needs_worktree`` property, so an
    explicit ``worktree`` on a ``needs_worktree: false`` agent (e.g. ``chat``) is
    drift — the exact contradiction the derivation removes. This guard turns a
    future re-drift into a build-time error, mirroring
    ``_validate_posthook_property_consistency``.

    Only the contradiction direction is checked. A ``needs_worktree: true`` agent
    explicitly listing ``worktree`` is redundant-but-consistent (the derivation
    union dedups it), and a binding ``prehooks: []`` suppressing the derived
    worktree is the documented override seam — neither is flagged. Non-agent jobs
    and prompts with no ``agent.yaml`` are skipped.
    """
    by_name = {j.name: j for j in jobs}

    def _check(job: Job, prehooks: list[str], where: str) -> None:
        if job.type != "agent" or "worktree" not in prehooks:
            return
        agent = resolve_agent(job.prompt if job.prompt is not None else job.name)
        if agent is not None and not agent.needs_worktree:
            raise ValueError(
                f"{where} explicitly declares the ``worktree`` prehook, but its agent "
                f"({job.prompt or job.name!r}) has ``needs_worktree: false``. "
                "worktree is derived from ``needs_worktree`` (ADR-044 Phase 3) — drop the "
                "explicit prehook, or set ``needs_worktree: true`` in the agent's "
                "agent.yaml if it genuinely needs a worktree."
            )

    for j in jobs:
        _check(j, j.prehooks, f"Job {j.name!r}")
    for flow_name, bindings in flow_bindings.items():
        for b in bindings:
            if b.prehooks:
                job = by_name.get(b.name)
                if job is not None:
                    _check(job, b.prehooks, f"Flow {flow_name!r} step {b.name!r}")


def _validate_runner_references(jobs: list[Job]) -> None:
    """Raise ``ValueError`` if any job names an unregistered ``runner:``.

    The runner registry (ADR-023) is populated by ``OrchestratorService.start()``
    (built-in ``default`` at import time, ``claude-agent-sdk`` + operator
    ``runners:`` at start) BEFORE processes load, so a name still missing here
    is a typo or a missing ``runners:`` entry. Failing at build time beats
    failing at dispatch time. Only jobs with ``runner`` set are checked; jobs
    with ``runner=None`` route via the existing model-prefix resolution path,
    which is unchanged (ADR-023).

    Mirrors ``_validate_posthook_references`` and ``_validate_engine_references``:
    registers the built-in ``claude-agent-sdk`` runner if it is absent so that
    direct ``build_process`` callers (tests, custom entry points that haven't
    called ``start()``) see the same registry the runtime path sees. Unlike the
    posthook/engine validators, there is no standalone side-effect module to
    import — the registration is done inline here with the constructor's
    all-defaults form (construction is safe without the package installed; the
    actual SDK import is lazy inside ``run()``).
    """
    from rigg.agent_runner import RunnerNotFound, register_runner, resolve_runner_by_name

    try:
        resolve_runner_by_name("claude-agent-sdk")
    except RunnerNotFound:
        from rigg.claude_agent_sdk_runner import ClaudeAgentSDKRunner

        register_runner("claude-agent-sdk", ClaudeAgentSDKRunner())

    for j in jobs:
        if j.runner is None:
            continue
        try:
            resolve_runner_by_name(j.runner)
        except RunnerNotFound as exc:
            raise ValueError(
                f"Job {j.name!r} references unknown runner {j.runner!r}. {exc} "
                "Register it via the ``runners:`` block in lotsa.yaml, select the "
                "built-in ``claude-agent-sdk``, or check for a typo."
            ) from exc


def build_process(
    name: str,
    prompts_dir: Path | None = None,
    process_file: Path | None = None,
) -> Process:
    """Build a Process by preset name or from a YAML file."""
    if process_file is not None:
        loaded_name, jobs, flow_bindings, _raw = _load_yaml_process(process_file)
    elif name in PRESET_NAMES:
        bundled = BUNDLED_PROMPTS / name / "process.yaml"
        if not bundled.is_file():
            raise FileNotFoundError(
                f"Bundled process file missing: {bundled}. "
                "ADR-014 Layer A renamed flow.yaml → process.yaml; if you see "
                "this error after upgrading, re-install the package or rebuild."
            )
        loaded_name, jobs, flow_bindings, _raw = _load_yaml_process(bundled)
    else:
        raise ValueError(f"Unknown process: {name!r}. Choose from: {PRESET_NAMES}")

    registry = AgentPromptRegistry(prompts_dir, AGENTS_DIR)

    flows: dict[str, FlowConfig] = {}
    for flow_name, bindings in flow_bindings.items():
        resolved, gate_states = _resolve_jobs(jobs, bindings, resolve_agent=registry.load_agent_optional)
        sm = _build_state_machine(resolved, gate_states)
        flows[flow_name] = FlowConfig(
            name=flow_name,
            state_machine=sm,
            jobs=resolved,
            bindings=bindings,
            registry=registry,
            gate_states=gate_states,
        )

    # Register cross-flow edges so the orchestrator can CAS through sub-flow
    # boundaries while still using a single ``self.flow.state_machine``
    # (the main flow's SM). Without this, the orchestrator's ``_dispatch_step``
    # and drainer reject every (monitor_state → sub_flow_first.queue) and
    # (sub_flow_step.active → host_flow.queue) transition because each flow's
    # SM only knows its own bindings.
    _register_cross_flow_edges(flows)

    # Validate ``tool:`` / ``engine:`` job references against the registry.
    # Callers (``OrchestratorService.start()``, custom CLI entry points)
    # register built-ins and user-supplied tools/engines BEFORE invoking
    # ``build_process``, so a missing name here is unambiguously a typo or a
    # missing ``tools:`` / ``engines:`` entry in ``lotsa.yaml``. Failing at
    # startup beats failing at dispatch time, which would happen only after
    # the operator has waited through spec / plan / test / code / review.
    _validate_registry_references(jobs)

    # ADR-021 R6 — reject output-rule targets that don't resolve to a job in
    # THIS process (cross-process dispatch is unsupported). Runs after
    # ``_register_cross_flow_edges`` (which only ever stitches edges between
    # flows of THIS process, using ``my_names``/sibling-flow job names — it
    # never reaches across processes) so within-process sub-flow targets are
    # already accounted for and only genuine cross-process / typo'd targets
    # trip the validator.
    _validate_rule_targets(jobs, flow_bindings)

    # Validate ``posthooks:`` references (per-job and per-binding) against the
    # posthook registry so an unknown name fails fast at build time, same as
    # tool/engine references above.
    _validate_posthook_references(jobs, flow_bindings)

    # ADR-044 Phase 2 — reject an explicit ``commit`` posthook on an agent whose
    # ``produces_changes: false`` property contradicts it (commit is now derived
    # from the property; an explicit one on a non-producer is drift).
    _validate_posthook_property_consistency(jobs, flow_bindings, registry.load_agent_optional)

    # ADR-044 Phase 3 — validate ``prehooks:`` references (per-job and
    # per-binding) against the prehook registry, and reject an explicit
    # ``worktree`` on a ``needs_worktree: false`` agent (worktree is now derived
    # from the property; an explicit one on an opt-out agent is drift).
    _validate_prehook_references(jobs, flow_bindings)
    _validate_prehook_property_consistency(jobs, flow_bindings, registry.load_agent_optional)

    # Validate ``runner:`` job references against the runner registry (ADR-028
    # Phase 3) so a mistyped or missing runner name fails at startup, not at
    # dispatch time. The registry is populated by ``start()`` before processes
    # load, so any name still absent here is unambiguously wrong.
    _validate_runner_references(jobs)

    # The "main" flow's resolved-jobs view is the process-level catalog used
    # by callers that need a ResolvedJob with derived states (e.g. for
    # display). It may not include every job (sub-flow-only jobs), so we
    # union jobs across flows.
    seen: dict[str, ResolvedJob] = {}
    for flow in flows.values():
        for rj in flow.jobs:
            seen.setdefault(rj.name, rj)
    # Make sure jobs declared but not in any flow still appear (corner case).
    by_name = {j.name: j for j in jobs}
    for jn, j in by_name.items():
        if jn not in seen:
            # Build a minimal ResolvedJob with degenerate states for catalog purposes.
            seen[jn] = ResolvedJob(
                name=j.name,
                prompt_name=j.prompt or j.name,
                resume_session=j.resume,
                evaluate=j.evaluate,
                queue_state=j.queue_state or j.name,
                active_state=j.active_state or j.name,
                success_state="complete",
                type=j.type,
                rules=list(j.rules),
                conversational=j.conversational,
                output=j.output,
                inputs=list(j.inputs),
                tool=j.tool,
                engine=j.engine,
                config=dict(j.config),
                output_file=j.output_file,
                commit=j.commit,
                posthooks=list(j.posthooks),
                prehooks=list(j.prehooks),
                commit_prefix=j.commit_prefix,
                model=j.model,
                runner=j.runner,
                timeout_warn_seconds=j.timeout_warn_seconds,
                timeout_kill_seconds=j.timeout_kill_seconds,
            )
    catalog = list(seen.values())

    return Process(
        name=loaded_name,
        jobs=catalog,
        flows=flows,
        registry=registry,
        description=_raw.get("description"),
        promotion_inputs=_parse_promotion_inputs(_raw.get("promotion_inputs")),
        invocable=_parse_invocable(_raw.get("invocable")),
    )


def build_process_from_inline(
    name: str,
    raw: dict[str, Any],
    base_dir: Path,
) -> Process:
    """Build a Process from a ``processes:`` block entry in ``lotsa.yaml``.

    The inline form supports agent-only sequences — the common case for
    non-engineering processes (marketing research, content review, etc.).
    Complex processes that need monitors, actions, or sub-flows should live
    as a standalone ``process.yaml`` and be loaded via ``--flow-file``.

    Schema (each value in ``LotsaConfig.processes``)::

        default: bool         # optional; picks the fallback when no --process
        prompts_dir: str      # optional; relative paths resolve against base_dir
                              # default: ``./prompts``
        steps: list[step]     # required; each step is an agent job

    Each step::

        name: str             # required; the job name (and queue/active state)
        prompt: str           # optional; basename of the prompt file
                              # default: the step's name
                              # the orchestrator loads <prompt>-system.md and
                              # <prompt>-user.md from the process's prompts_dir
        rules: list[rule]     # optional; same shape as a process.yaml's output rules
        evaluate: bool        # optional; requires human approval to advance
        conversational: bool  # optional; chat-style iterative step
        output: str           # optional; named artifact produced by this step
        inputs: list[str]     # optional; artifact names this step requires

    ``base_dir`` is the directory of the ``lotsa.yaml`` that produced this
    config — relative paths inside the entry (``prompts_dir``) resolve
    against it. Pass an absolute path to short-circuit the join.
    """
    if not isinstance(raw, dict):
        raise ValueError(f"Process {name!r}: entry must be a dict, got {type(raw).__name__}")

    raw_steps = raw.get("steps")
    if not isinstance(raw_steps, list) or not raw_steps:
        raise ValueError(f"Process {name!r}: requires a non-empty ``steps:`` list")

    # Resolve the prompts directory. Default is ``<base_dir>/prompts``; an
    # explicit ``prompts_dir:`` overrides (relative resolves against base_dir).
    raw_prompts_dir = raw.get("prompts_dir")
    if raw_prompts_dir is None:
        prompts_dir = base_dir / "prompts"
    else:
        candidate = Path(raw_prompts_dir)
        prompts_dir = candidate if candidate.is_absolute() else (base_dir / candidate)

    jobs: list[Job] = []
    for i, raw_step in enumerate(raw_steps):
        if not isinstance(raw_step, dict):
            raise ValueError(f"Process {name!r}: step {i} must be a dict (got {type(raw_step).__name__})")
        step_name = raw_step.get("name")
        if not step_name:
            raise ValueError(f"Process {name!r}: step {i} missing required ``name:`` field")
        if raw_step.get("type") not in (None, "agent"):
            raise ValueError(
                f"Process {name!r}: step {step_name!r} declares type={raw_step['type']!r}. "
                f"Inline processes support only agent steps; use a standalone process.yaml "
                f"loaded via --flow-file for action/monitor jobs."
            )
        # ``inputs:`` accepts either a list of artifact names or a single
        # bare string (sugar for a one-artifact dependency). Mirrors the
        # convenience shim in ``_parse_job`` so the two parsers handle
        # the YAML schema identically. Without this, ``inputs: spec``
        # would be ``list("spec")`` → ``["s", "p", "e", "c"]``.
        raw_inputs = raw_step.get("inputs", []) or []
        if isinstance(raw_inputs, str):
            raw_inputs = [raw_inputs]
        jobs.append(
            Job(
                name=step_name,
                type="agent",
                prompt=raw_step.get("prompt"),  # _resolve_jobs defaults to job name when None
                evaluate=bool(raw_step.get("evaluate", False)),
                rules=_parse_rules(raw_step.get("rules")) or [],
                conversational=bool(raw_step.get("conversational", False)),
                output=raw_step.get("output"),
                inputs=list(raw_inputs),
                timeout_warn_seconds=raw_step.get("timeout_warn_seconds"),
                timeout_kill_seconds=raw_step.get("timeout_kill_seconds"),
            )
        )

    registry = AgentPromptRegistry(prompts_dir, AGENTS_DIR)

    # Bindings mirror the steps in order (no per-flow rule overrides for the
    # inline form — every rule lives on the job itself).
    bindings = [FlowBinding(name=j.name) for j in jobs]
    resolved, gate_states = _resolve_jobs(jobs, bindings, resolve_agent=registry.load_agent_optional)
    sm = _build_state_machine(resolved, gate_states)

    # ADR-044 Phase 2 — an inline step referencing a catalog agent by name
    # derives/guards ``commit`` just like a bundled process; a step whose
    # prompt has no ``agent.yaml`` resolves to ``None`` and is a no-op.
    _validate_posthook_property_consistency(jobs, {"main": bindings}, registry.load_agent_optional)

    # ADR-044 Phase 3 — same guard for the derived ``worktree`` prehook. Inline
    # steps carry no explicit ``prehooks:`` (the inline schema doesn't parse
    # them), so reference validation is unnecessary here; only the property
    # consistency guard is relevant for a step referencing an opt-out agent.
    _validate_prehook_property_consistency(jobs, {"main": bindings}, registry.load_agent_optional)

    flow = FlowConfig(
        name="main",
        state_machine=sm,
        jobs=resolved,
        bindings=bindings,
        registry=registry,
        gate_states=gate_states,
    )
    flows = {"main": flow}

    # No cross-flow edges (single flow); no registry-name validation
    # (no action/monitor jobs reference the registry).
    return Process(
        name=name,
        jobs=resolved,
        flows=flows,
        registry=registry,
        description=raw.get("description"),
        promotion_inputs=_parse_promotion_inputs(raw.get("promotion_inputs")),
        invocable=_parse_invocable(raw.get("invocable")),
    )


def build_flow(
    flow_name: str,
    prompts_dir: Path | None = None,
    flow_file: Path | None = None,
) -> FlowConfig:
    """Build the root flow of a process — backward-compat entry point.

    For ADR-014 Layer A, ``build_flow`` returns ``process.flows["main"]``
    (or the only flow defined when there is no "main"). Callers that need
    the full process catalog should use :func:`build_process`.
    """
    process = build_process(flow_name, prompts_dir=prompts_dir, process_file=flow_file)
    if "main" in process.flows:
        return process.flows["main"]
    if len(process.flows) == 1:
        return next(iter(process.flows.values()))
    raise ValueError(f"Process {flow_name!r} has multiple flows; use build_process()")


def find_step_for_gate(flow: FlowConfig, gate_state: str) -> ResolvedJob | None:
    for step in flow.jobs:
        if step.success_state == gate_state:
            return step
    return None


def find_step(flow: FlowConfig, active_state: str) -> ResolvedJob | None:
    for step in flow.jobs:
        if step.active_state == active_state:
            return step
    return None


def next_dispatchable_state(flow: FlowConfig, current_state: str) -> str | None:
    """Find the next state that has a dispatch rule."""
    for step in flow.jobs:
        if step.queue_state == current_state:
            return current_state

    if current_state in flow.gate_states:
        for i, step in enumerate(flow.jobs):
            if step.success_state == current_state and i + 1 < len(flow.jobs):
                return flow.jobs[i + 1].queue_state

    visited: set[str] = set()
    state = current_state
    while state not in visited:
        visited.add(state)
        for src, dst in flow.state_machine.transitions:
            if src == state and dst != "blocked":
                for step in flow.jobs:
                    if step.queue_state == dst:
                        return dst
                state = dst
                break
        else:
            return None
    return None


def build_dispatch_rules(flow: FlowConfig, work_dir: Path) -> list[DispatchRule]:
    """Convert ResolvedJobs into DispatchRules for the OrchestrationEngine."""

    def _make_prompt_builder(step: ResolvedJob):
        def build_prompts(item: Item) -> tuple[str, str]:
            system = flow.registry.load(f"{step.prompt_name}-system")
            user_template = flow.registry.load(f"{step.prompt_name}-user")
            user = user_template.replace("{title}", item.title or "").replace("{body}", item.body or "")
            return system, user

        return build_prompts

    rules = []
    for step in flow.jobs:
        if step.type != "agent":
            continue
        rules.append(
            DispatchRule(
                queue_state=step.queue_state,
                active_state=step.active_state,
                job_type=step.job_type,
                build_prompts=_make_prompt_builder(step),
                work_dir=lambda _item, wd=work_dir: wd,
            )
        )
    return rules
