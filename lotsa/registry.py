"""Tool and engine registry (ADR-014 Layer A).

Tools are the action-job execution surface; engines are the monitor-job
execution surface. Both register under a string name that flow YAML can
reference via ``tool:`` / ``engine:``.

Built-in tools and engines self-register at import time (via
``lotsa.tools.__init__`` and ``lotsa.engines.__init__``). User-supplied
entries are loaded from ``lotsa.yaml``'s ``tools:`` / ``engines:`` blocks
at ``OrchestratorService.start()``.

SECURITY (audit finding #5): the ``tools:`` / ``engines:`` / ``runners:``
blocks name ``'pkg.mod:callable'`` strings that are ``importlib``-imported at
startup — importing a module runs its top-level code, and runner classes are
also instantiated. ``lotsa.yaml`` is therefore an **executable trust
boundary**: only run a config file you wrote or fully trust. Treat a cloned or
shared ``lotsa.yaml`` like a script, not data. This is documented in
``SECURITY.md`` and the ``lotsa init`` scaffold.
"""

from __future__ import annotations

import asyncio
import importlib
from collections.abc import Awaitable, Callable
from typing import TYPE_CHECKING, Any

if TYPE_CHECKING:
    from lotsa.tools import TaskContext, ToolResult

ToolCallable = Callable[["TaskContext", dict[str, Any]], Awaitable["ToolResult"]]
# Posthooks share the tool call signature — ``async (TaskContext, config) ->
# ToolResult`` — because both run an orchestrator-owned operation against a
# task's worktree and report success/metadata the same way. The registry is
# kept separate from ``_TOOLS`` so a name can't collide across the two
# surfaces and so ``list_posthooks`` reports only posthooks.
PosthookCallable = Callable[["TaskContext", dict[str, Any]], Awaitable["ToolResult"]]

_TOOLS: dict[str, ToolCallable] = {}
# Engine classes are intentionally typed as ``type[Any]`` rather than bare
# ``type``: callers want to instantiate the returned class (e.g. ``cls(orch,
# state, config)``) and bare ``type`` gives mypy no shape to check against,
# which silently downgrades call-site type information to ``Any``. Using
# ``type[Any]`` is the same runtime contract — any class is accepted — but
# makes the "you'll get a class back" promise explicit at the API edge.
_ENGINES: dict[str, type[Any]] = {}


# ---------------------------------------------------------------------------
# Tools
# ---------------------------------------------------------------------------


def register_tool(name: str, fn: ToolCallable) -> None:
    """Register a tool under *name*.

    Raises ``ValueError`` on collision so silent overrides cannot happen — a
    second registration is almost always a bug (duplicate import side-effect,
    typo, etc.).
    """
    if name in _TOOLS:
        raise ValueError(f"Tool {name!r} already registered")
    _TOOLS[name] = fn


def get_tool(name: str) -> ToolCallable:
    """Look up a tool by name.

    Raises ``KeyError`` with the registered tool list in the message so the
    operator can immediately spot a typo or missing import.
    """
    if name not in _TOOLS:
        raise KeyError(f"Tool {name!r} is not registered. Registered tools: {sorted(_TOOLS)}")
    return _TOOLS[name]


def is_tool_registered(name: str) -> bool:
    """Return ``True`` if a tool with *name* is already registered.

    Public membership probe so callers (notably the built-in re-registration
    guards in ``lotsa.tools.__init__``) don't need to import the private
    ``_TOOLS`` dict — a future rename of the underlying storage would otherwise
    surface as an ``ImportError`` at the call site instead of a clean lookup.
    """
    return name in _TOOLS


def list_tools() -> list[str]:
    """Return the sorted list of registered tool names.

    Public listing for callers that need to surface the registered set in an
    error message (notably ``_validate_registry_references`` in ``flows.py``).
    Same rationale as ``is_tool_registered``: keeps the call site decoupled
    from the private ``_TOOLS`` storage name.
    """
    return sorted(_TOOLS)


def load_user_tools(spec: dict[str, str]) -> None:
    """Import + register user tools from a ``{name: 'pkg.mod:func'}`` mapping.

    Called once at orchestrator start with the ``tools:`` block from
    ``lotsa.yaml``. Errors are surfaced with the tool name so operators can
    diagnose mis-typed import paths without spelunking the orchestrator log.
    """
    for name, dotted in spec.items():
        if ":" not in dotted:
            raise ValueError(f"Bad tool path {dotted!r} for {name!r}; expected 'pkg.mod:func'")
        module_name, _, attr = dotted.rpartition(":")
        try:
            module = importlib.import_module(module_name)
        except ImportError as exc:
            raise ImportError(f"Cannot import {module_name!r} for tool {name!r}: {exc}") from exc
        fn = getattr(module, attr, None)
        if fn is None:
            raise AttributeError(f"{module_name!r} has no attribute {attr!r} (tool {name!r})")
        # Reject non-async callables at startup rather than letting the
        # dispatcher crash mid-run when it tries to ``await`` a sync function.
        if not asyncio.iscoroutinefunction(fn):
            raise TypeError(f"Tool {name!r} ({dotted!r}) must be defined with ``async def`` — got a plain callable")
        register_tool(name, fn)


# ---------------------------------------------------------------------------
# Engines (symmetric API)
# ---------------------------------------------------------------------------


def register_engine(name: str, cls: type[Any]) -> None:
    """Register a monitor-engine class under *name*."""
    if name in _ENGINES:
        raise ValueError(f"Engine {name!r} already registered")
    _ENGINES[name] = cls


def get_engine(name: str) -> type[Any]:
    """Look up a monitor-engine class by name."""
    if name not in _ENGINES:
        raise KeyError(f"Engine {name!r} is not registered. Registered engines: {sorted(_ENGINES)}")
    return _ENGINES[name]


def is_engine_registered(name: str) -> bool:
    """Return ``True`` if an engine with *name* is already registered.

    Symmetric to ``is_tool_registered`` — see that docstring for the rationale.
    """
    return name in _ENGINES


def list_engines() -> list[str]:
    """Return the sorted list of registered engine names.

    Symmetric to ``list_tools`` — see that docstring for the rationale.
    """
    return sorted(_ENGINES)


def load_user_engines(spec: dict[str, str]) -> None:
    """Import + register user engines from a ``{name: 'pkg.mod:Class'}`` mapping."""
    for name, dotted in spec.items():
        if ":" not in dotted:
            raise ValueError(f"Bad engine path {dotted!r} for {name!r}; expected 'pkg.mod:Class'")
        module_name, _, attr = dotted.rpartition(":")
        try:
            module = importlib.import_module(module_name)
        except ImportError as exc:
            raise ImportError(f"Cannot import {module_name!r} for engine {name!r}: {exc}") from exc
        cls = getattr(module, attr, None)
        if cls is None:
            raise AttributeError(f"{module_name!r} has no attribute {attr!r} (engine {name!r})")
        if not isinstance(cls, type):
            raise TypeError(f"Engine {name!r} ({dotted!r}) must be a class — got {type(cls).__name__}")
        register_engine(name, cls)


# ---------------------------------------------------------------------------
# Agent runners (ADR-023) — symmetric loader over the rigg registry
# ---------------------------------------------------------------------------


def load_user_runners(
    spec: dict[str, dict[str, Any]],
    *,
    model: str,
    budget_usd: float,
    max_output_tokens: int | None = None,
) -> None:
    """Import + construct + register user runners from a ``lotsa.yaml`` block.

    Each entry is ``name -> {"handler": "pkg.mod:Class", "prefixes": [...]}``.
    The handler class is imported, constructed, and registered for its model-name
    prefixes in the *rigg* runner registry (ADR-023). Symmetric to
    ``load_user_tools`` / ``load_user_engines``, but the registry it feeds lives
    in ``rigg/`` because runners are a both-editions primitive.

    Construction-arg contract: each handler is constructed with
    ``cls(model=..., budget_usd=..., max_output_tokens=...)`` — the same kwargs
    ``ClaudeCodeRunner`` accepts. Third-party runner ctors must accept these (a
    known limitation; follow-up runner PRs adapt). Errors surface with the
    runner name so a mis-typed import path is diagnosable without spelunking the
    orchestrator log.
    """
    # Imported here (not at module top) so ``lotsa.registry`` keeps no import-time
    # dependency on rigg beyond what callers already pull in.
    from rigg import register_runner

    for name, entry in spec.items():
        if not isinstance(entry, dict) or "handler" not in entry:
            raise ValueError(f"Runner {name!r} must be a mapping with a 'handler' key; got {entry!r}")
        dotted = entry["handler"]
        if ":" not in dotted:
            raise ValueError(f"Bad runner handler {dotted!r} for {name!r}; expected 'pkg.mod:Class'")
        module_name, _, attr = dotted.rpartition(":")
        try:
            module = importlib.import_module(module_name)
        except ImportError as exc:
            raise ImportError(f"Cannot import {module_name!r} for runner {name!r}: {exc}") from exc
        cls = getattr(module, attr, None)
        if cls is None:
            raise AttributeError(f"{module_name!r} has no attribute {attr!r} (runner {name!r})")
        if not isinstance(cls, type):
            raise TypeError(f"Runner {name!r} ({dotted!r}) must be a class — got {type(cls).__name__}")
        runner = cls(model=model, budget_usd=budget_usd, max_output_tokens=max_output_tokens)
        register_runner(name, runner, prefixes=entry.get("prefixes", []))


# ---------------------------------------------------------------------------
# Posthooks (ADR-024) — symmetric API
# ---------------------------------------------------------------------------

_POSTHOOKS: dict[str, PosthookCallable] = {}


def register_posthook(name: str, fn: PosthookCallable) -> None:
    """Register a posthook under *name*.

    Posthooks are orchestrator-run operations that fire after an agent step
    succeeds (e.g. the built-in ``commit``). Raises ``ValueError`` on
    collision so a duplicate import side-effect or typo surfaces loudly,
    matching ``register_tool``.
    """
    if name in _POSTHOOKS:
        raise ValueError(f"Posthook {name!r} already registered")
    _POSTHOOKS[name] = fn


def get_posthook(name: str) -> PosthookCallable:
    """Look up a posthook by name.

    Raises ``KeyError`` with the registered posthook list in the message so
    an operator can immediately spot a typo in a process YAML's ``posthooks:``
    declaration.
    """
    if name not in _POSTHOOKS:
        raise KeyError(f"Posthook {name!r} is not registered. Registered posthooks: {sorted(_POSTHOOKS)}")
    return _POSTHOOKS[name]


def is_posthook_registered(name: str) -> bool:
    """Return ``True`` if a posthook with *name* is already registered.

    Symmetric to ``is_tool_registered`` — see that docstring for the rationale
    (keeps the built-in re-registration guard decoupled from ``_POSTHOOKS``).
    """
    return name in _POSTHOOKS


def list_posthooks() -> list[str]:
    """Return the sorted list of registered posthook names.

    Symmetric to ``list_tools`` — used by ``flows._validate_posthook_references``
    to surface the registered set in a build-time error message.
    """
    return sorted(_POSTHOOKS)


# ---------------------------------------------------------------------------
# Prehooks (ADR-044 Phase 3) — symmetric API
# ---------------------------------------------------------------------------

# Prehooks share the tool/posthook call signature — ``async (TaskContext,
# config) -> ToolResult`` — because they too run an orchestrator-owned
# operation against a task's dispatch environment and report success/metadata
# the same way. They fire *before* an agent/action step dispatches; the
# built-in ``worktree`` prehook ensures the task's git worktree exists. The
# registry is kept separate from ``_TOOLS`` / ``_POSTHOOKS`` so a name can't
# collide across surfaces and so ``list_prehooks`` reports only prehooks.
PrehookCallable = Callable[["TaskContext", dict[str, Any]], Awaitable["ToolResult"]]

_PREHOOKS: dict[str, PrehookCallable] = {}


def register_prehook(name: str, fn: PrehookCallable) -> None:
    """Register a prehook under *name*.

    Prehooks are orchestrator-run operations that fire before an agent/action
    step dispatches (e.g. the built-in ``worktree``). Raises ``ValueError`` on
    collision so a duplicate import side-effect or typo surfaces loudly,
    matching ``register_posthook``.
    """
    if name in _PREHOOKS:
        raise ValueError(f"Prehook {name!r} already registered")
    _PREHOOKS[name] = fn


def get_prehook(name: str) -> PrehookCallable:
    """Look up a prehook by name.

    Raises ``KeyError`` with the registered prehook list in the message so an
    operator can immediately spot a typo in a process YAML's ``prehooks:``
    declaration.
    """
    if name not in _PREHOOKS:
        raise KeyError(f"Prehook {name!r} is not registered. Registered prehooks: {sorted(_PREHOOKS)}")
    return _PREHOOKS[name]


def is_prehook_registered(name: str) -> bool:
    """Return ``True`` if a prehook with *name* is already registered.

    Symmetric to ``is_posthook_registered`` — keeps the built-in
    re-registration guard decoupled from ``_PREHOOKS``.
    """
    return name in _PREHOOKS


def list_prehooks() -> list[str]:
    """Return the sorted list of registered prehook names.

    Symmetric to ``list_posthooks`` — used by ``flows._validate_prehook_references``
    to surface the registered set in a build-time error message.
    """
    return sorted(_PREHOOKS)


# ---------------------------------------------------------------------------
# Snapshot / restore (test-isolation surface)
# ---------------------------------------------------------------------------


def snapshot() -> dict[str, dict[str, Any]]:
    """Capture a copy of the registry state for later ``restore()``.

    The returned dict is opaque to callers — pass it back to ``restore()``
    unchanged. Pairs with ``restore()`` to bracket per-test fixtures that
    mutate the process-global registry; the public API keeps test code from
    reaching into the private ``_TOOLS`` / ``_ENGINES`` dicts directly, so a
    future rename of the storage doesn't silently break every fixture.
    """
    return {
        "tools": dict(_TOOLS),
        "engines": dict(_ENGINES),
        "posthooks": dict(_POSTHOOKS),
        "prehooks": dict(_PREHOOKS),
    }


def restore(state: dict[str, dict[str, Any]]) -> None:
    """Replace the registry contents with a previously captured ``snapshot()``.

    Caller-provided ``state`` is treated as authoritative — any tool or engine
    registered since the snapshot is discarded, and any name present in the
    snapshot is re-registered. Use this only in test teardown; production code
    must go through ``register_tool`` / ``register_engine``.
    """
    _TOOLS.clear()
    _TOOLS.update(state["tools"])
    _ENGINES.clear()
    _ENGINES.update(state["engines"])
    # ``posthooks`` is absent from snapshots captured before ADR-024 — default
    # to empty so an old snapshot still restores cleanly.
    _POSTHOOKS.clear()
    _POSTHOOKS.update(state.get("posthooks", {}))
    # ``prehooks`` is absent from snapshots captured before ADR-044 Phase 3 —
    # same back-compat default so an old snapshot still restores cleanly.
    _PREHOOKS.clear()
    _PREHOOKS.update(state.get("prehooks", {}))
