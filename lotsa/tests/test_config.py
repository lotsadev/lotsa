"""Tests for LotsaConfig loading and merge logic."""

from pathlib import Path

import yaml

from lotsa.config import LotsaConfig


def test_defaults():
    """Config with no file and no overrides uses built-in defaults."""
    config = LotsaConfig()
    assert config.data_dir == Path.home() / ".lotsa"
    assert config.model == "sonnet"
    assert config.budget == 5.0
    assert config.prompts_dir is None


def test_load_from_yaml(tmp_path):
    """Config file values override defaults."""
    config_file = tmp_path / "lotsa.yaml"
    config_file.write_text(yaml.dump({"model": "opus", "budget": 10.0}, sort_keys=False))
    config = LotsaConfig.load(config_path=config_file)
    assert config.model == "opus"
    assert config.budget == 10.0


def test_cli_overrides_yaml(tmp_path):
    """CLI args take precedence over config file."""
    config_file = tmp_path / "lotsa.yaml"
    config_file.write_text(yaml.dump({"model": "opus", "budget": 10.0}))
    config = LotsaConfig.load(config_path=config_file, model="haiku", budget=None)
    assert config.model == "haiku"  # CLI wins
    assert config.budget == 10.0  # None CLI = use yaml value


def test_none_cli_args_dont_override(tmp_path):
    """None CLI values don't overwrite config file or defaults."""
    config_file = tmp_path / "lotsa.yaml"
    config_file.write_text(yaml.dump({"model": "opus"}))
    config = LotsaConfig.load(config_path=config_file, model=None)
    assert config.model == "opus"


def test_data_dir_override_picks_up_lotsa_yaml(tmp_path):
    """``--data-dir`` override discovers lotsa.yaml in the new location."""
    (tmp_path / "lotsa.yaml").write_text(yaml.dump({"model": "haiku"}))
    config = LotsaConfig.load(data_dir=tmp_path)
    assert config.model == "haiku"
    assert config.config_path == (tmp_path / "lotsa.yaml").resolve()


def test_no_config_file_leaves_config_path_none(tmp_path):
    """Missing config file leaves ``config_path=None`` for callers to detect.

    The CLI uses this signal to surface the "run lotsa init" error.
    """
    config = LotsaConfig.load(data_dir=tmp_path)
    assert config.config_path is None
    assert config.model == "sonnet"  # defaults preserved


def test_prompts_dir_as_path(tmp_path):
    """prompts_dir from YAML is converted to Path."""
    config_file = tmp_path / "lotsa.yaml"
    config_file.write_text(yaml.dump({"prompts_dir": "my/prompts"}))
    config = LotsaConfig.load(config_path=config_file)
    assert config.prompts_dir == Path("my/prompts")


def test_invalid_yaml_ignored(tmp_path):
    """Non-dict YAML content is silently ignored."""
    config_file = tmp_path / "lotsa.yaml"
    config_file.write_text("just a string")
    config = LotsaConfig.load(config_path=config_file)
    assert config.model == "sonnet"  # defaults preserved


def test_docker_defaults():
    """Docker fields have sensible defaults."""
    config = LotsaConfig()
    assert config.docker is False
    assert config.docker_image == "lotsa-agent:latest"


def test_docker_from_yaml(tmp_path):
    """Docker settings loaded from config file."""
    config_file = tmp_path / "lotsa.yaml"
    config_file.write_text(yaml.dump({"docker": True, "docker_image": "custom:v1"}))
    config = LotsaConfig.load(config_path=config_file)
    assert config.docker is True
    assert config.docker_image == "custom:v1"


def test_docker_cli_override(tmp_path):
    """CLI --docker flag overrides config file."""
    config_file = tmp_path / "lotsa.yaml"
    config_file.write_text(yaml.dump({"docker": False}))
    config = LotsaConfig.load(config_path=config_file, docker=True)
    assert config.docker is True


def test_max_output_tokens_default_is_none(tmp_path):
    """Unset ``max_output_tokens`` stays None so the runner doesn't clobber
    the shell-exported ``CLAUDE_CODE_MAX_OUTPUT_TOKENS`` workaround.
    """
    config = LotsaConfig()
    assert config.max_output_tokens is None


def test_max_output_tokens_loads_from_yaml(tmp_path):
    """``max_output_tokens: N`` in lotsa.yaml is loaded as an int."""
    config_file = tmp_path / "lotsa.yaml"
    config_file.write_text(yaml.dump({"max_output_tokens": 128000}))
    config = LotsaConfig.load(config_path=config_file)
    assert config.max_output_tokens == 128000


def test_max_output_tokens_cli_override(tmp_path):
    """``--max-output-tokens`` on the CLI overrides the YAML value."""
    config_file = tmp_path / "lotsa.yaml"
    config_file.write_text(yaml.dump({"max_output_tokens": 64000}))
    config = LotsaConfig.load(config_path=config_file, max_output_tokens=200000)
    assert config.max_output_tokens == 200000


def test_flow_default():
    """Default flow is 'chat' (ADR-034 §2 — chat-first task creation)."""
    config = LotsaConfig()
    assert config.flow == "chat"


def test_flow_from_yaml(tmp_path):
    """Flow loaded from config file."""
    config_file = tmp_path / "lotsa.yaml"
    config_file.write_text(yaml.dump({"flow": "full"}))
    config = LotsaConfig.load(config_path=config_file)
    assert config.flow == "full"


def test_flow_cli_override(tmp_path):
    """CLI --flow overrides config file."""
    config_file = tmp_path / "lotsa.yaml"
    config_file.write_text(yaml.dump({"flow": "simple"}))
    config = LotsaConfig.load(config_path=config_file, flow="full")
    assert config.flow == "full"


# ---------------------------------------------------------------------------
# processes: block (multi-process hosting)
# ---------------------------------------------------------------------------


def test_processes_block_loads_from_yaml(tmp_path):
    """A ``processes:`` block in lotsa.yaml parses into a dict of process configs."""
    config_file = tmp_path / "lotsa.yaml"
    config_file.write_text(
        yaml.dump(
            {
                "processes": {
                    "marketing_research": {
                        "default": True,
                        "steps": [
                            {"name": "research", "prompt": "research"},
                            {"name": "synthesize", "prompt": "synthesize"},
                        ],
                    },
                }
            }
        )
    )
    config = LotsaConfig.load(config_path=config_file)
    assert "marketing_research" in config.processes
    entry = config.processes["marketing_research"]
    assert entry["default"] is True
    assert len(entry["steps"]) == 2
    assert entry["steps"][0]["name"] == "research"


def test_processes_block_defaults_to_empty_dict():
    """Without a ``processes:`` block the field is empty (today's UX preserved)."""
    config = LotsaConfig()
    assert config.processes == {}


def test_config_path_records_yaml_location(tmp_path):
    """``config_path`` is set to the absolute path of the loaded lotsa.yaml."""
    config_file = tmp_path / "lotsa.yaml"
    config_file.write_text(yaml.dump({"flow": "full"}))
    config = LotsaConfig.load(config_path=config_file)
    assert config.config_path is not None
    assert config.config_path == config_file.resolve()


def test_config_path_is_none_without_yaml(tmp_path):
    """A programmatic config (no yaml found) has ``config_path=None``."""
    # tmp_path has no lotsa.yaml — load() against this data_dir finds nothing.
    config = LotsaConfig.load(data_dir=tmp_path)
    assert config.config_path is None


def test_config_path_cannot_be_set_from_yaml(tmp_path):
    """A ``config_path:`` key in lotsa.yaml is ignored — the field is derived."""
    config_file = tmp_path / "lotsa.yaml"
    # If the YAML walk honoured ``config_path``, it would clobber the load()-
    # derived value with the bogus string.
    config_file.write_text(yaml.dump({"config_path": "/tmp/bogus.yaml"}))
    config = LotsaConfig.load(config_path=config_file)
    assert config.config_path == config_file.resolve()


def test_data_dir_cannot_be_set_from_yaml(tmp_path):
    """A ``data_dir:`` key in lotsa.yaml is ignored — ``--data-dir`` is CLI-only.

    The YAML lives inside data_dir; letting the YAML rewrite where data_dir
    points would create a circular discovery problem (the next ``lotsa
    serve`` would look in a different place than where the YAML lives).
    """
    config_file = tmp_path / "lotsa.yaml"
    config_file.write_text(yaml.dump({"data_dir": "/tmp/elsewhere"}))
    config = LotsaConfig.load(config_path=config_file)
    # data_dir stays at the dataclass default (home / .lotsa) — NOT the
    # YAML's bogus value.
    assert config.data_dir == Path.home() / ".lotsa"


# ---------------------------------------------------------------------------
# YAML-null normalization — bare ``key:`` (null value) keeps the default
# ---------------------------------------------------------------------------


def test_yaml_null_processes_keeps_empty_default(tmp_path):
    """``processes:`` (bare null) in lotsa.yaml leaves config.processes={}.

    Regression: pre-fix the setattr loop would write ``None`` over the
    ``default_factory=dict`` default, and every downstream consumer that
    iterated ``self.config.processes.items()`` (e.g. ``_select_active_process_name``
    during ``start()``) would crash with ``AttributeError: 'NoneType'
    object has no attribute 'items'``.
    """
    config_file = tmp_path / "lotsa.yaml"
    config_file.write_text("processes:\n")  # bare null
    config = LotsaConfig.load(config_path=config_file)
    assert config.processes == {}, (
        f"YAML-null ``processes:`` must preserve the empty-dict default; got {config.processes!r}"
    )


def test_yaml_null_tools_keeps_empty_default(tmp_path):
    """``tools:`` (bare null) — same crash class as processes:."""
    config_file = tmp_path / "lotsa.yaml"
    config_file.write_text("tools:\n")
    config = LotsaConfig.load(config_path=config_file)
    assert config.tools == {}


def test_yaml_null_engines_keeps_empty_default(tmp_path):
    """``engines:`` (bare null) — same crash class as processes:."""
    config_file = tmp_path / "lotsa.yaml"
    config_file.write_text("engines:\n")
    config = LotsaConfig.load(config_path=config_file)
    assert config.engines == {}


def test_yaml_null_optional_path_preserves_default(tmp_path):
    """A YAML-null on a ``Path | None`` field stays None (the default).

    Confirms the universal ``if value is None: continue`` rule is safe
    for fields where None is meaningful (no false positive — the value
    was already None and stays that way).
    """
    config_file = tmp_path / "lotsa.yaml"
    config_file.write_text("prompts_dir:\n")
    config = LotsaConfig.load(config_path=config_file)
    assert config.prompts_dir is None


def test_yaml_null_scalar_keeps_dataclass_default(tmp_path):
    """A YAML-null on a non-None scalar keeps the dataclass default.

    ``model:`` (null) doesn't write ``None`` (which would crash anything
    expecting a string later) — it leaves the ``"sonnet"`` default in
    place. The skip-on-None rule applies uniformly.
    """
    config_file = tmp_path / "lotsa.yaml"
    config_file.write_text("model:\n")
    config = LotsaConfig.load(config_path=config_file)
    assert config.model == "sonnet"
