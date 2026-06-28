"""Tests for the tool/engine registry (ADR-014 Layer A).

These tests exercise the new ``lotsa.registry`` module and the
``lotsa.tools`` / ``lotsa.engines`` packages introduced by the
typed-jobs refactor. They are expected to fail until those modules
exist.
"""

from __future__ import annotations

from pathlib import Path

import pytest
import yaml

# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


# The ``_isolated_registry`` autouse fixture lives in ``lotsa/tests/conftest.py``
# (uses the public ``registry.snapshot()`` / ``registry.restore()`` API). Tests in
# this module register / pop tools freely; the fixture restores the built-in
# baseline after each test.


# ---------------------------------------------------------------------------
# Module surface
# ---------------------------------------------------------------------------


def test_registry_module_exports_tool_api():
    """``lotsa.registry`` exposes ``register_tool`` / ``get_tool`` / ``load_user_tools``."""
    from lotsa import registry

    assert hasattr(registry, "register_tool")
    assert hasattr(registry, "get_tool")
    assert hasattr(registry, "load_user_tools")


def test_registry_module_exports_engine_api():
    """``lotsa.registry`` exposes ``register_engine`` / ``get_engine`` / ``load_user_engines``."""
    from lotsa import registry

    assert hasattr(registry, "register_engine")
    assert hasattr(registry, "get_engine")
    assert hasattr(registry, "load_user_engines")


def test_tools_package_exports_task_context_and_tool_result():
    """``lotsa.tools`` exposes the ``TaskContext`` and ``ToolResult`` dataclasses."""
    from lotsa.tools import TaskContext, ToolResult

    assert TaskContext is not None
    assert ToolResult is not None


# ---------------------------------------------------------------------------
# Tool registration
# ---------------------------------------------------------------------------


def test_register_tool_makes_it_retrievable():
    from lotsa.registry import get_tool, register_tool

    async def my_tool(ctx, config):
        from lotsa.tools import ToolResult

        return ToolResult(success=True, output="ok")

    register_tool("my_tool", my_tool)
    assert get_tool("my_tool") is my_tool


def test_register_tool_rejects_name_collision():
    """Registering the same name twice raises so silent overrides cannot happen."""
    from lotsa.registry import register_tool

    async def t1(ctx, config): ...

    async def t2(ctx, config): ...

    register_tool("dup_tool", t1)
    with pytest.raises(ValueError, match="dup_tool"):
        register_tool("dup_tool", t2)


def test_get_tool_unknown_name_raises_with_registered_list():
    """``get_tool`` on an unknown name names the missing tool AND lists known ones."""
    from lotsa.registry import get_tool, register_tool

    async def known(ctx, config): ...

    register_tool("known_tool", known)

    with pytest.raises(KeyError) as exc_info:
        get_tool("nope")
    msg = str(exc_info.value)
    assert "nope" in msg
    assert "known_tool" in msg


def test_built_in_push_pr_tool_is_registered_on_import():
    """Importing ``lotsa.tools`` registers the built-in ``push_pr`` tool."""
    import lotsa.tools  # noqa: F401 — import side effect registers built-ins
    from lotsa.registry import get_tool

    fn = get_tool("push_pr")
    assert callable(fn)


# ---------------------------------------------------------------------------
# load_user_tools
# ---------------------------------------------------------------------------


def test_load_user_tools_imports_dotted_callable(tmp_path: Path, monkeypatch):
    """``load_user_tools({'name': 'pkg.mod:func'})`` imports + registers the callable."""
    pkg = tmp_path / "user_tools_pkg"
    pkg.mkdir()
    (pkg / "__init__.py").write_text("")
    (pkg / "tools.py").write_text(
        "from lotsa.tools import ToolResult\n"
        "async def my_user_tool(ctx, config):\n"
        "    return ToolResult(success=True, output='hello')\n"
    )
    monkeypatch.syspath_prepend(str(tmp_path))

    from lotsa.registry import get_tool, load_user_tools

    load_user_tools({"user_tool": "user_tools_pkg.tools:my_user_tool"})
    fn = get_tool("user_tool")
    assert callable(fn)


def test_load_user_tools_bad_dotted_path_raises():
    """A path missing the ':' separator is rejected with a clear message."""
    from lotsa.registry import load_user_tools

    with pytest.raises(ValueError, match="bad_path"):
        load_user_tools({"bad_path": "no_colon_here"})


def test_load_user_tools_missing_module_raises_with_tool_name():
    """A missing module surfaces an ImportError naming the tool."""
    from lotsa.registry import load_user_tools

    with pytest.raises(ImportError) as exc_info:
        load_user_tools({"ghost": "does.not.exist:fn"})
    assert "ghost" in str(exc_info.value)


def test_load_user_tools_missing_attr_raises_with_tool_name(tmp_path: Path, monkeypatch):
    """A module that lacks the named callable surfaces AttributeError naming the tool."""
    pkg = tmp_path / "partial_pkg"
    pkg.mkdir()
    (pkg / "__init__.py").write_text("# empty module — no my_func defined\n")
    monkeypatch.syspath_prepend(str(tmp_path))

    from lotsa.registry import load_user_tools

    with pytest.raises(AttributeError) as exc_info:
        load_user_tools({"missing_attr_tool": "partial_pkg:my_func"})
    assert "missing_attr_tool" in str(exc_info.value)


# ---------------------------------------------------------------------------
# Engine registration (symmetric API)
# ---------------------------------------------------------------------------


def test_register_engine_makes_it_retrievable():
    from lotsa.registry import get_engine, register_engine

    class FakeEngine:
        def __init__(self, orchestrator, monitor_state, config): ...

        async def run(self): ...

        def untrack(self, task_id): ...

    register_engine("fake_engine", FakeEngine)
    assert get_engine("fake_engine") is FakeEngine


def test_register_engine_rejects_name_collision():
    from lotsa.registry import register_engine

    class E1:
        def __init__(self, orchestrator, monitor_state, config): ...
        async def run(self): ...
        def untrack(self, task_id): ...

    class E2:
        def __init__(self, orchestrator, monitor_state, config): ...
        async def run(self): ...
        def untrack(self, task_id): ...

    register_engine("dup_engine", E1)
    with pytest.raises(ValueError, match="dup_engine"):
        register_engine("dup_engine", E2)


def test_get_engine_unknown_name_raises_with_registered_list():
    from lotsa.registry import get_engine, register_engine

    class Known:
        def __init__(self, orchestrator, monitor_state, config): ...
        async def run(self): ...
        def untrack(self, task_id): ...

    register_engine("known_engine", Known)

    with pytest.raises(KeyError) as exc_info:
        get_engine("nope_engine")
    msg = str(exc_info.value)
    assert "nope_engine" in msg
    assert "known_engine" in msg


def test_built_in_pr_monitor_engine_registered_on_import():
    """Importing ``lotsa.engines`` registers the built-in ``pr_monitor`` engine."""
    import lotsa.engines  # noqa: F401
    from lotsa.registry import get_engine

    cls = get_engine("pr_monitor")
    assert isinstance(cls, type)


# ---------------------------------------------------------------------------
# lotsa.yaml integration — tools: and engines: fields on LotsaConfig
# ---------------------------------------------------------------------------


def test_lotsa_config_has_tools_field():
    """``LotsaConfig`` carries a ``tools: dict[str, str]`` field."""
    from lotsa.config import LotsaConfig

    config = LotsaConfig()
    assert isinstance(config.tools, dict)


def test_lotsa_config_has_engines_field():
    """``LotsaConfig`` carries an ``engines: dict[str, str]`` field."""
    from lotsa.config import LotsaConfig

    config = LotsaConfig()
    assert isinstance(config.engines, dict)


def test_lotsa_yaml_tools_block_loaded(tmp_path: Path):
    """A ``tools:`` block in lotsa.yaml lands on ``LotsaConfig.tools``."""
    from lotsa.config import LotsaConfig

    yaml_path = tmp_path / "lotsa.yaml"
    yaml_path.write_text(yaml.dump({"tools": {"my_tool": "my_pkg.mod:fn"}}))

    config = LotsaConfig.load(config_path=yaml_path)
    assert config.tools == {"my_tool": "my_pkg.mod:fn"}


def test_lotsa_yaml_engines_block_loaded(tmp_path: Path):
    """An ``engines:`` block in lotsa.yaml lands on ``LotsaConfig.engines``."""
    from lotsa.config import LotsaConfig

    yaml_path = tmp_path / "lotsa.yaml"
    yaml_path.write_text(yaml.dump({"engines": {"my_engine": "my_pkg.mod:Engine"}}))

    config = LotsaConfig.load(config_path=yaml_path)
    assert config.engines == {"my_engine": "my_pkg.mod:Engine"}
