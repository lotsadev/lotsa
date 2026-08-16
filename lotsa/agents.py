"""The shared, process-independent agent catalog (ADR-044, Phase 1).

An **agent** is a reusable unit: a prompt (``system.md`` / ``user.md``) plus
declared properties (``agent.yaml``) — a worker-vs-gate ``class``, the closed
set of ``outcomes`` it may emit, and the reserved property slots
``needs_worktree`` / ``produces_changes`` (declared here from day one; wired to
hooks in Phases 2–3).

Catalog layout — one directory per agent::

    prompts/agents/<name>/
        agent.yaml     # name, class, outcomes, needs_worktree, produces_changes
        system.md
        user.md

Routing semantics live on the flow *edge*, not the agent: an agent only reports
an outcome from the universal :data:`AGENT_OUTCOMES` vocabulary, and the
workflow decides what that outcome means next.
"""

from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path

import yaml as _yaml

# The closed, universal outcome vocabulary. ``INPUT`` is orthogonal — any agent
# may emit it (a blocking question) — so it is admissible for both classes.
AGENT_OUTCOMES: tuple[str, ...] = ("COMPLETED", "PASSED", "FAILED", "SKIPPED", "INPUT")

# Class-admissible emittable sets. Workers do work and report COMPLETED (with an
# optional SKIPPED / FAILED); gates render a verdict (PASSED / FAILED). Both may
# additionally raise INPUT.
_WORKER_OUTCOMES: frozenset[str] = frozenset({"COMPLETED", "SKIPPED", "FAILED", "INPUT"})
_GATE_OUTCOMES: frozenset[str] = frozenset({"PASSED", "FAILED", "INPUT"})

_CLASSES = ("worker", "gate")

# The bundled catalog directory.
AGENTS_DIR: Path = Path(__file__).parent / "prompts" / "agents"


@dataclass(frozen=True)
class Agent:
    """A first-class, process-independent agent definition."""

    name: str
    agent_class: str  # "worker" | "gate" (YAML key: ``class``)
    outcomes: tuple[str, ...]
    needs_worktree: bool = False
    produces_changes: bool = False

    @property
    def is_gate(self) -> bool:
        return self.agent_class == "gate"

    @property
    def is_worker(self) -> bool:
        return self.agent_class == "worker"


def _parse_agent(name: str, data: dict) -> Agent:
    """Validate + build an :class:`Agent` from a parsed ``agent.yaml`` mapping.

    Fails loud (``ValueError``) on an unknown class, an out-of-vocab outcome, or
    an outcome the declared class may not emit — mirroring the build-time
    validators in :mod:`lotsa.flows`.
    """
    if not isinstance(data, dict):
        raise ValueError(f"Agent {name!r}: agent.yaml must be a mapping, got {type(data).__name__}")

    agent_class = data.get("class")
    if agent_class not in _CLASSES:
        raise ValueError(f"Agent {name!r}: class must be one of {_CLASSES}, got {agent_class!r}")

    raw_outcomes = data.get("outcomes", [])
    if isinstance(raw_outcomes, str):
        raw_outcomes = [raw_outcomes]
    if not isinstance(raw_outcomes, list) or not raw_outcomes:
        raise ValueError(f"Agent {name!r}: outcomes must be a non-empty list")

    outcomes = tuple(str(o).strip() for o in raw_outcomes)
    vocab = set(AGENT_OUTCOMES)
    unknown = set(outcomes) - vocab
    if unknown:
        raise ValueError(f"Agent {name!r}: out-of-vocabulary outcomes {sorted(unknown)}; allowed: {sorted(vocab)}")

    admissible = _GATE_OUTCOMES if agent_class == "gate" else _WORKER_OUTCOMES
    inadmissible = set(outcomes) - admissible
    if inadmissible:
        raise ValueError(
            f"Agent {name!r} ({agent_class}) declares outcomes {sorted(inadmissible)} "
            f"it may not emit; {agent_class} outcomes ⊆ {sorted(admissible)}"
        )

    return Agent(
        name=data.get("name", name),
        agent_class=agent_class,
        outcomes=outcomes,
        needs_worktree=bool(data.get("needs_worktree", False)),
        produces_changes=bool(data.get("produces_changes", False)),
    )


def _parse_repo_agent(name: str, data: dict) -> Agent:
    """Validate + build an :class:`Agent` from a **repo-shipped** ``agent.yaml``
    (ADR-044 Phase 5).

    Repo agents declare the same schema as bundled ones, so this reuses
    :func:`_parse_agent` — it is the single seam where a future tightening of
    the operator-owned property axis (e.g. rejecting a repo that sets
    ``produces_changes`` without an operator grant) would live. Today repo
    ``produces_changes`` / ``needs_worktree`` are honoured: they only opt work
    *into* orchestrator-owned deterministic hooks (commit / worktree), never out
    of the push/review structure.
    """
    return _parse_agent(name, data)


def load_agent(name: str, agents_dir: Path | None = None) -> Agent:
    """Load a single agent definition by name from *agents_dir* (default bundled)."""
    base = agents_dir if agents_dir is not None else AGENTS_DIR
    agent_yaml = Path(base) / name / "agent.yaml"
    if not agent_yaml.is_file():
        raise ValueError(f"Agent {name!r}: no agent.yaml at {agent_yaml}")
    data = _yaml.safe_load(agent_yaml.read_text())
    return _parse_agent(name, data or {})


def load_agent_catalog(agents_dir: Path | None = None) -> dict[str, Agent]:
    """Load every agent (a subdirectory carrying ``agent.yaml``) from *agents_dir*."""
    base = Path(agents_dir) if agents_dir is not None else AGENTS_DIR
    catalog: dict[str, Agent] = {}
    if not base.is_dir():
        return catalog
    for child in sorted(base.iterdir()):
        if not child.is_dir():
            continue
        if not (child / "agent.yaml").is_file():
            continue
        catalog[child.name] = load_agent(child.name, agents_dir=base)
    return catalog


__all__ = [
    "AGENT_OUTCOMES",
    "AGENTS_DIR",
    "Agent",
    "_parse_repo_agent",
    "load_agent",
    "load_agent_catalog",
]
