"""Configuration loading with layered resolution.

Resolution order: built-in defaults ← lotsa.yaml ← CLI args.

All Lotsa state — the SQLite DB, per-task worktrees, and ``lotsa.yaml`` —
lives in a single directory (``data_dir``), default ``~/.lotsa``. The
config file is discovered at ``data_dir/lotsa.yaml``; ``--config <path>``
overrides the discovery for one-off invocations.
"""

from __future__ import annotations

import logging
import re
from dataclasses import dataclass, field, fields
from pathlib import Path
from typing import Any

import yaml

logger = logging.getLogger(__name__)

# Project ids are used verbatim as a filesystem path segment
# (``worktrees/<project_id>/…``) and as the ``projects`` table primary key, so
# they are constrained to a conservative slug (ADR-029 §1). An unconstrained
# YAML key could contain ``/``, ``..`` or spaces — a silent path-traversal
# hazard.
_PROJECT_ID_RE = re.compile(r"[a-z0-9_-]{1,64}")


@dataclass
class LotsaConfig:
    """All settings for a Lotsa run."""

    data_dir: Path = field(default_factory=lambda: Path.home() / ".lotsa")
    work_dir: Path = field(default_factory=lambda: Path("."))
    model: str = "sonnet"
    budget: float = 5.0
    # Cap on tokens Claude Code may emit in a single response. ``None`` means
    # "don't set it — let Claude Code use its built-in default (32000 as of
    # mid-2026) or any value the operator has exported via the
    # ``CLAUDE_CODE_MAX_OUTPUT_TOKENS`` environment variable". Set this when
    # tasks are hitting the 32000 ceiling and the work genuinely needs more
    # output (large diffs, long plans). Plumbed through to the runner's
    # subprocess env in ``rigg.agent_runner.ClaudeCodeRunner``.
    max_output_tokens: int | None = None
    prompts_dir: Path | None = None
    # New-task default process (ADR-034 §2). ``chat`` opens a task as a
    # conversation the operator grows from (promote into ``full``/``quickfix``/…
    # when ready). ``--flow``/``--process`` (or this ``flow:`` field) only
    # selects which loaded process is the picker's pre-selected default — the
    # full bundled catalog always loads (ADR-034 §1/§4).
    flow: str = "chat"
    docker: bool = False
    docker_image: str = "lotsa-agent:latest"
    # Global agent-runner selection (ADR-028 Phase 1). ``None`` keeps today's
    # behaviour (the CLI ``ClaudeCodeRunner``, or ``DockerAgentRunner`` when
    # ``docker`` is set). ``"claude-agent-sdk"`` selects the SDK-shaped
    # ``ClaudeAgentSDKRunner`` and requires ``ANTHROPIC_API_KEY``. This is a
    # minimal global selector; ADR-028 Phase 3 later replaces it with
    # per-step registry resolution (ADR-023).
    runner: str | None = None
    # ADR-038 — explicit, per-launch opt-out of the host sandbox: run the agent
    # with ``--dangerously-skip-permissions`` (it can then modify the host). CLI
    # only (``lotsa serve --dangerously-skip-permissions``); deliberately NOT
    # read from lotsa.yaml — running unsandboxed must be a conscious choice each
    # launch, never a persisted setting. See ``_apply_yaml``.
    skip_permissions: bool = False
    flow_file: Path | None = None
    # User-supplied tool/engine registries.
    # Each entry is ``name -> "dotted.module:callable"`` imported at startup.
    tools: dict[str, str] = field(default_factory=dict)
    engines: dict[str, str] = field(default_factory=dict)
    # User-supplied agent-runner registry (ADR-023). Each entry is
    # ``name -> {"handler": "dotted.module:Class", "prefixes": [...]}`` —
    # the handler is imported and constructed at startup and registered for
    # its model-name prefixes. Built-in ``ClaudeCodeRunner`` is always the
    # default and needs no entry, so existing configs leave this empty.
    runners: dict[str, dict[str, Any]] = field(default_factory=dict)
    # Inline-defined processes from lotsa.yaml. Each entry is a name keying
    # a dict with at minimum a ``steps:`` list of agent jobs. Optional:
    # ``default: true`` selects the fallback when no ``--process`` flag is
    # given; ``prompts_dir:`` overrides the prompts directory (defaults to
    # ``./prompts`` next to lotsa.yaml). See ``flows.build_process_from_inline``
    # for the exact shape.
    processes: dict[str, dict[str, Any]] = field(default_factory=dict)
    # Registered projects (ADR-029). Raw YAML passthrough: each entry is
    # ``id -> {"path": <local repo>, "name": <optional>}``. The id is a slug
    # (``[a-z0-9_-]{1,64}``) used as a filesystem path segment and DB PK.
    # Validated + normalized into ``ProjectSpec``s by ``resolve_project_specs``
    # at startup; ``work_dir:`` seeds a ``default`` project for single-project
    # configs that omit this block.
    projects: dict[str, dict[str, Any]] = field(default_factory=dict)
    # Absolute path to the lotsa.yaml file that produced this config (if any).
    # Used by ``flows.build_process_from_inline`` to resolve relative paths
    # (e.g. ``prompts_dir: ./prompts/sw-dev``) against the YAML's directory
    # rather than the orchestrator's CWD. Set by ``load()``; None for configs
    # built programmatically without a YAML.
    config_path: Path | None = None

    @classmethod
    def load(
        cls,
        config_path: Path | None = None,
        **cli_overrides: object,
    ) -> LotsaConfig:
        """Load config: defaults ← lotsa.yaml ← CLI args.

        If *config_path* is given, read that file directly. Otherwise look
        for ``lotsa.yaml`` at ``data_dir/lotsa.yaml`` (default
        ``~/.lotsa/lotsa.yaml``). When ``--data-dir`` is supplied as a CLI
        override, the discovery happens against the override location.

        Returns a config with ``config_path`` set when a YAML was loaded;
        callers check that field to surface "no config found — run lotsa
        init" errors.
        """
        config = cls()

        # Apply ``--data-dir`` early so the discovery below sees the right
        # directory. Other CLI overrides are applied after the YAML load.
        if (override := cli_overrides.get("data_dir")) is not None:
            config.data_dir = override if isinstance(override, Path) else Path(str(override))

        # Discover the YAML — explicit --config wins; otherwise look in data_dir.
        yaml_path = config_path
        if yaml_path is None:
            candidate = config.data_dir / "lotsa.yaml"
            if candidate.is_file():
                yaml_path = candidate

        if yaml_path is not None:
            config = _apply_yaml(config, yaml_path)
            config.config_path = yaml_path.resolve()

        # CLI overrides (only apply non-None values)
        for key, value in cli_overrides.items():
            if value is None:
                continue
            if hasattr(config, key):
                # Convert string paths to Path objects for Path fields
                fld = {f.name: f for f in fields(cls)}.get(key)
                if fld and fld.type in ("Path", "Path | None") and isinstance(value, str):
                    value = Path(value)
                setattr(config, key, value)

        return config


def _apply_yaml(config: LotsaConfig, path: Path) -> LotsaConfig:
    """Merge YAML file values into config."""
    text = path.read_text()
    data = yaml.safe_load(text)
    if not isinstance(data, dict):
        return config

    field_names = {f.name for f in fields(LotsaConfig)}
    path_fields = {f.name for f in fields(LotsaConfig) if f.type in ("Path", "Path | None")}

    # ``work_dir:`` still seeds a ``default`` project going forward (ADR-029 §2)
    # but is deprecated in favour of an explicit ``projects:`` block.
    if "work_dir" in data and data["work_dir"] is not None:
        logger.warning(
            "lotsa.yaml: ``work_dir:`` is deprecated — declare a ``projects:`` block instead. "
            "Unless you declare ``projects: default:``, ``work_dir:`` still seeds a ``default`` "
            "project from that path — even alongside an explicit ``projects:`` block, which then "
            "registers a surprise extra ``default``. Remove ``work_dir:`` once you migrate. "
            "It will be dropped in a later release."
        )

    for key, value in data.items():
        if key not in field_names:
            # Warn on unknown top-level keys rather than silently ignoring them
            # (ADR-029 §Context): the reference install carried a stale
            # ``project_dir:``/``process:`` that quietly did nothing for months.
            # ``data_dir``/``config_path`` are real fields handled below, so they
            # never reach this branch.
            logger.warning("lotsa.yaml: unknown top-level key %r — ignored.", key)
            continue
        if key in ("data_dir", "config_path"):
            continue  # derived; never read from YAML
        if key == "skip_permissions":
            # ADR-038 — the sandbox opt-out is per-launch CLI only; ignore it in
            # lotsa.yaml so it can't be silently persisted.
            logger.warning(
                "lotsa.yaml: ``skip_permissions:`` is ignored — pass "
                "--dangerously-skip-permissions per launch instead (ADR-038)."
            )
            continue
        # YAML-null on a key means "use the dataclass default" — skip the
        # setattr rather than override the default_factory dict/list/str
        # with None. Without this, a bare ``processes:`` (or ``tools:`` /
        # ``engines:``) line in lotsa.yaml clobbers the empty-dict default
        # with None, and every downstream consumer that iterates it
        # crashes with ``AttributeError: 'NoneType' object has no
        # attribute 'items'``. Fields where None is meaningful
        # (``flow_file``, ``prompts_dir``) are already None by default, so
        # the skip is a no-op for them.
        if value is None:
            continue
        if key in path_fields:
            value = Path(value)
        setattr(config, key, value)

    return config


@dataclass(frozen=True)
class ProjectSpec:
    """A validated, normalized project registration derived from ``lotsa.yaml``.

    ``id`` is the slug; ``name`` defaults to the id; ``path`` is the
    ``expanduser().resolve()``-normalized local repo root. Produced by
    :func:`resolve_project_specs` and consumed by the orchestrator's startup
    project sync (ADR-029).
    """

    id: str
    name: str
    path: Path


def resolve_project_specs(config: LotsaConfig) -> list[ProjectSpec]:
    """Validate + normalize the ``projects:`` block into :class:`ProjectSpec`s.

    Validation (ADR-029 §1) is a hard startup error — invalid config must fail
    fast, not at first dispatch:

    * Each project id matches ``[a-z0-9_-]{1,64}`` (it is a filesystem path
      segment and DB primary key). The error names the offending key.
    * Each ``path`` is ``expanduser().resolve()``-normalized (``pathlib`` does
      NOT expand ``~``), then validated to exist and be a git repository.

    ``work_dir:`` seeds a ``default`` project so a single-project config (no
    ``projects:`` block) keeps working — including the zero-config / scaffolded
    case where ``work_dir`` is the bare ``.`` default, which resolves to the
    launch CWD exactly as the former singleton ``WorktreeManager`` did. An
    explicit ``projects: default:`` entry always wins over ``work_dir:`` seeding
    regardless of order (declared entries are authoritative). The seeded
    ``default`` is intentionally lenient — it is not git-validated — so existing
    single-project deployments whose ``work_dir`` predates the git-repo
    requirement still start.
    """
    specs: list[ProjectSpec] = []
    declared: set[str] = set()
    for raw_id, entry in (config.projects or {}).items():
        pid = str(raw_id)
        if not _PROJECT_ID_RE.fullmatch(pid):
            raise ValueError(
                f"Invalid project id {raw_id!r} in lotsa.yaml ``projects:`` — must match "
                f"[a-z0-9_-]{{1,64}} (used as a filesystem path segment and DB key)."
            )
        if isinstance(entry, dict):
            raw_path = entry.get("path")
            name = entry.get("name") or pid
        else:
            raw_path = entry
            name = pid
        if not raw_path:
            raise ValueError(f"Project {pid!r} in lotsa.yaml is missing a ``path:``.")
        path = Path(str(raw_path)).expanduser().resolve()
        if not path.exists():
            raise ValueError(f"Project {pid!r} path does not exist: {path}")
        if not (path / ".git").exists():
            raise ValueError(f"Project {pid!r} path is not a git repository: {path}")
        specs.append(ProjectSpec(id=pid, name=str(name), path=path))
        declared.add(pid)

    # ``work_dir:`` seeding for single-project configs. An explicit
    # ``projects: default:`` always wins, so never seed over it. The ONE case we
    # must not seed is a multi-project ``projects:`` block that omits
    # ``work_dir`` (so ``work_dir`` is the unconfigured ``.`` sentinel) —
    # appending a spurious CWD-rooted ``default`` there would be wrong. In every
    # other case seed ``default`` from ``work_dir``, resolving the bare ``.`` to
    # the launch CWD exactly as the former singleton did. (``lotsa init`` now
    # scaffolds no ``work_dir`` and no ``projects:`` block; ``work_dir`` falls
    # back to the ``.`` default, so this zero-config path still seeds ``default``
    # from the launch CWD — ADR-029 §2 / acceptance criterion 6.)
    skip_seed = bool(config.projects) and Path(config.work_dir) == Path(".")
    if "default" not in declared and not skip_seed:
        seeded = Path(config.work_dir).expanduser().resolve()
        specs.append(ProjectSpec(id="default", name="default", path=seeded))

    return specs
