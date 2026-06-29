"""Tests for `lotsa deploy` orchestration (ADR-042).

Covers the pure pieces (config → env mapping, deploy.env rendering, project
fan-out) and the ssh/scp command sequence via an injected runner — no real
network, no real subprocess.
"""

from __future__ import annotations

from pathlib import Path

import pytest

from lotsa import deploy as dep


# --------------------------------------------------------------------------- #
# config_to_env
# --------------------------------------------------------------------------- #
def _base_cfg() -> dict:
    return {
        "host": "root@box",
        "domain": "lotsa.example.com",
        "basic_auth": {"user": "admin", "password": "s3cret"},
        "anthropic_api_key": "sk-test",
    }


def test_config_to_env_minimal() -> None:
    env = dep.config_to_env(_base_cfg())
    assert env["LOTSA_DOMAIN"] == "lotsa.example.com"
    assert env["LOTSA_BASIC_USER"] == "admin"
    assert env["LOTSA_BASIC_PASS"] == "s3cret"
    assert env["ANTHROPIC_API_KEY"] == "sk-test"
    assert env["LOTSA_MODEL"] == "sonnet"  # default
    # Optional keys absent when unset.
    assert "GITHUB_TOKEN" not in env
    assert "LOTSA_ADMIN_EMAIL" not in env
    assert "CLAUDE_CODE_OAUTH_TOKEN" not in env


def test_config_to_env_full() -> None:
    cfg = _base_cfg() | {
        "admin_email": "you@example.com",
        "github_token": "ghp_x",
        "model": "opus",
        "version": "0.1.0",
        "git": "https://github.com/lotsadev/lotsa.git",
    }
    env = dep.config_to_env(cfg)
    assert env["LOTSA_ADMIN_EMAIL"] == "you@example.com"
    assert env["GITHUB_TOKEN"] == "ghp_x"
    assert env["LOTSA_MODEL"] == "opus"
    assert env["LOTSA_VERSION"] == "0.1.0"
    assert env["LOTSA_GIT"] == "https://github.com/lotsadev/lotsa.git"


def test_config_to_env_oauth_instead_of_api_key() -> None:
    cfg = _base_cfg()
    del cfg["anthropic_api_key"]
    cfg["claude_code_oauth_token"] = "oauth-token"
    env = dep.config_to_env(cfg)
    assert env["CLAUDE_CODE_OAUTH_TOKEN"] == "oauth-token"
    assert "ANTHROPIC_API_KEY" not in env


@pytest.mark.parametrize(
    "mutate, message",
    [
        (lambda c: c.pop("domain"), "domain"),
        (lambda c: c.pop("basic_auth"), "basic_auth"),
        (lambda c: c.pop("anthropic_api_key"), "anthropic_api_key"),
    ],
)
def test_config_to_env_missing_required(mutate, message) -> None:
    cfg = _base_cfg()
    mutate(cfg)
    with pytest.raises(dep.DeployError, match=message):
        dep.config_to_env(cfg)


# --------------------------------------------------------------------------- #
# projects fan-out
# --------------------------------------------------------------------------- #
def test_projects_single_uses_shorthand() -> None:
    env = dep.config_to_env(_base_cfg() | {"projects": [{"id": "demo", "repo": ""}]})
    assert env["PROJECT_ID"] == "demo"
    assert env["PROJECT_REPO"] == ""
    assert "PROJECT_REPOS" not in env


def test_projects_multi_uses_repos_list() -> None:
    projects = [
        {"id": "api", "repo": "https://github.com/you/api.git"},
        {"id": "web", "repo": "https://github.com/you/web.git"},
    ]
    env = dep.config_to_env(_base_cfg() | {"projects": projects})
    assert env["PROJECT_REPOS"] == ("api=https://github.com/you/api.git web=https://github.com/you/web.git")
    assert "PROJECT_ID" not in env


def test_projects_multi_requires_repo_each() -> None:
    projects = [{"id": "api", "repo": "u"}, {"id": "web", "repo": ""}]
    with pytest.raises(dep.DeployError, match="needs a `repo`"):
        dep.config_to_env(_base_cfg() | {"projects": projects})


def test_projects_entry_needs_id() -> None:
    with pytest.raises(dep.DeployError, match="`id`"):
        dep.config_to_env(_base_cfg() | {"projects": [{"repo": "u"}]})


# --------------------------------------------------------------------------- #
# render_deploy_env
# --------------------------------------------------------------------------- #
def test_render_deploy_env_quotes_and_sorts() -> None:
    out = dep.render_deploy_env({"B_KEY": "two words", "A_KEY": "v"})
    lines = [ln for ln in out.splitlines() if not ln.startswith("#")]
    assert lines == ["A_KEY='v'", "B_KEY='two words'"]


def test_render_deploy_env_escapes_single_quote() -> None:
    out = dep.render_deploy_env({"PASS": "a'b"})
    assert "PASS='a'\\''b'" in out


# --------------------------------------------------------------------------- #
# load_config
# --------------------------------------------------------------------------- #
def test_load_config_missing(tmp_path: Path) -> None:
    with pytest.raises(dep.DeployError, match="--init"):
        dep.load_config(tmp_path / "nope.yaml")


def test_load_config_not_a_mapping(tmp_path: Path) -> None:
    p = tmp_path / "deploy.yaml"
    p.write_text("- a\n- b\n")
    with pytest.raises(dep.DeployError, match="mapping"):
        dep.load_config(p)


def test_init_template_is_valid_yaml() -> None:
    import yaml

    data = yaml.safe_load(dep.init_template())
    assert data["domain"] == "lotsa.example.com"
    assert data["basic_auth"]["user"] == "admin"


# --------------------------------------------------------------------------- #
# assets_dir — the repo checkout always has deploy/install.sh
# --------------------------------------------------------------------------- #
def test_assets_dir_finds_installer() -> None:
    d = dep.assets_dir()
    assert (d / "install.sh").is_file()
    for name in dep.ASSET_FILES:
        assert (d / name).is_file(), name


# --------------------------------------------------------------------------- #
# run_deploy command sequence (injected runner)
# --------------------------------------------------------------------------- #
def _record_runner(calls: list[list[str]]):
    def _run(cmd: list[str]) -> int:
        calls.append(cmd)
        return 0

    return _run


def test_run_deploy_sequence_pypi() -> None:
    calls: list[list[str]] = []
    dep.run_deploy(_base_cfg(), runner=_record_runner(calls), echo=lambda *_: None)

    # mkdir, scp assets, scp deploy.env, ssh install.sh
    assert calls[0][:2] == ["ssh", "root@box"]
    assert "mkdir -p /root/deploy" in calls[0][-1]

    scp_assets = calls[1]
    assert scp_assets[0] == "scp"
    assert scp_assets[-1] == "root@box:/root/deploy/"
    assert any(a.endswith("install.sh") for a in scp_assets)

    scp_env = calls[2]
    assert scp_env[-1] == "root@box:/root/deploy/deploy.env"

    last = calls[-1]
    assert last[0] == "ssh"
    assert "./install.sh" in last[-1]


def test_run_deploy_host_override() -> None:
    calls: list[list[str]] = []
    cfg = _base_cfg()
    del cfg["host"]
    dep.run_deploy(cfg, host="root@other", runner=_record_runner(calls), echo=lambda *_: None)
    assert all("root@other" in c[-1] or "root@other" in c for c in calls if c[0] in ("ssh", "scp"))


def test_run_deploy_no_host() -> None:
    cfg = _base_cfg()
    del cfg["host"]
    with pytest.raises(dep.DeployError, match="no target host"):
        dep.run_deploy(cfg, runner=_record_runner([]), echo=lambda *_: None)


def test_run_deploy_with_wheel(tmp_path: Path) -> None:
    wheel = tmp_path / "lotsa-0.1.0-py3-none-any.whl"
    wheel.write_bytes(b"fake")
    calls: list[list[str]] = []

    captured_env: dict[str, str] = {}
    orig = dep.render_deploy_env

    def _capture(env: dict[str, str]) -> str:
        captured_env.update(env)
        return orig(env)

    dep.render_deploy_env = _capture  # type: ignore[assignment]
    try:
        dep.run_deploy(_base_cfg(), wheel=wheel, runner=_record_runner(calls), echo=lambda *_: None)
    finally:
        dep.render_deploy_env = orig  # type: ignore[assignment]

    assert captured_env["LOTSA_WHEEL"] == "/root/deploy/lotsa-0.1.0-py3-none-any.whl"
    # The wheel is scp'd to the box.
    assert any(c[0] == "scp" and any(str(wheel) == a for a in c) for c in calls)


def test_run_deploy_missing_wheel() -> None:
    with pytest.raises(dep.DeployError, match="--wheel not found"):
        dep.run_deploy(
            _base_cfg(),
            wheel=Path("/no/such.whl"),
            runner=_record_runner([]),
            echo=lambda *_: None,
        )


def test_run_deploy_with_port() -> None:
    calls: list[list[str]] = []
    dep.run_deploy(_base_cfg() | {"port": 2222}, runner=_record_runner(calls), echo=lambda *_: None)
    assert calls[0][1:3] == ["-p", "2222"]  # ssh -p 2222 ...
    scp = next(c for c in calls if c[0] == "scp")
    assert scp[1:3] == ["-P", "2222"]  # scp -P 2222 ...


def test_run_deploy_propagates_failure() -> None:
    def _fail(cmd: list[str]) -> int:
        return 1

    with pytest.raises(dep.DeployError, match="command failed"):
        dep.run_deploy(_base_cfg(), runner=_fail, echo=lambda *_: None)


def test_run_deploy_dry_run_runs_nothing() -> None:
    calls: list[list[str]] = []
    dep.run_deploy(_base_cfg(), dry_run=True, runner=_record_runner(calls), echo=lambda *_: None)
    assert calls == []


# --------------------------------------------------------------------------- #
# CLI surface
# --------------------------------------------------------------------------- #
def test_cli_deploy_init_scaffolds(tmp_path: Path) -> None:
    import yaml
    from click.testing import CliRunner

    from lotsa.cli import cli

    cfg = tmp_path / "deploy.yaml"
    result = CliRunner().invoke(cli, ["deploy", "--init", "--config", str(cfg)])
    assert result.exit_code == 0, result.output
    assert cfg.is_file()
    assert (cfg.stat().st_mode & 0o777) == 0o600
    assert yaml.safe_load(cfg.read_text())["domain"] == "lotsa.example.com"


def test_cli_deploy_init_refuses_overwrite(tmp_path: Path) -> None:
    from click.testing import CliRunner

    from lotsa.cli import cli

    cfg = tmp_path / "deploy.yaml"
    cfg.write_text("host: x\n")
    result = CliRunner().invoke(cli, ["deploy", "--init", "--config", str(cfg)])
    assert result.exit_code == 1
    assert "already exists" in result.output


def test_cli_deploy_missing_config_errors(tmp_path: Path) -> None:
    from click.testing import CliRunner

    from lotsa.cli import cli

    result = CliRunner().invoke(cli, ["deploy", "--config", str(tmp_path / "nope.yaml")])
    assert result.exit_code == 1
    assert "init" in result.output


def test_cli_deploy_dry_run(tmp_path: Path) -> None:
    from click.testing import CliRunner

    from lotsa.cli import cli

    cfg = tmp_path / "deploy.yaml"
    cfg.write_text(
        "host: root@box\n"
        "domain: lotsa.example.com\n"
        "basic_auth:\n  user: admin\n  password: s3cret\n"
        "anthropic_api_key: sk-test\n"
    )
    result = CliRunner().invoke(cli, ["deploy", "--config", str(cfg), "--dry-run"])
    assert result.exit_code == 0, result.output
    assert "install.sh" in result.output
