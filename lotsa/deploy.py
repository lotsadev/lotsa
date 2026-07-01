"""Single-host deploy orchestration for ``lotsa deploy`` (ADR-042).

A pip-only operator has no repo checkout, so the deploy assets (``install.sh``,
the systemd unit, the Caddyfile template, ``check-sandbox.sh``) are bundled into
the wheel under ``lotsa/deploy/``. This module reads a declarative ``deploy.yaml``,
renders the ``deploy.env`` that ``install.sh`` already sources, ships everything
to the target host over plain ``ssh``/``scp`` (no new dependency), and runs the
installer.

The installer targets Debian/Ubuntu + systemd and refuses other platforms up
front (its own preflight). This module never puts secrets on a command line —
they travel only inside the 0600 ``deploy.env`` file.
"""

from __future__ import annotations

import shutil
import subprocess
import tempfile
from collections.abc import Callable
from importlib import resources
from pathlib import Path
from typing import Any

import yaml

# Asset files bundled into the wheel (and present in the repo's deploy/ dir).
# install.sh + check-sandbox.sh are executable; the installer chmods them anyway.
ASSET_FILES = (
    "install.sh",
    "lotsa.service",
    "Caddyfile.example",
    "check-sandbox.sh",
    "deploy.env.example",
    "README.md",
)

REMOTE_DIR = "/root/deploy"


class DeployError(Exception):
    """A deploy precondition failed (bad config, missing asset, ssh failure)."""


# --------------------------------------------------------------------------- #
# Asset location
# --------------------------------------------------------------------------- #
def assets_dir() -> Path:
    """Directory holding the bundled deploy assets.

    In an installed wheel they live at ``lotsa/deploy/`` (force-included). In an
    editable/source checkout that directory does not exist, so fall back to the
    repo-root ``deploy/`` two levels up from this file.
    """
    packaged = resources.files("lotsa") / "deploy"
    install_sh = packaged / "install.sh"
    if install_sh.is_file():
        return Path(str(packaged))
    repo_root = Path(__file__).resolve().parent.parent / "deploy"
    if (repo_root / "install.sh").is_file():
        return repo_root
    raise DeployError(
        "deploy assets not found — expected them bundled in the wheel "
        "(lotsa/deploy/) or in the repo's deploy/ directory."
    )


# --------------------------------------------------------------------------- #
# Config
# --------------------------------------------------------------------------- #
DEFAULT_CONFIG_NAME = "deploy.yaml"

INIT_TEMPLATE = """\
# deploy.yaml — config for `lotsa deploy` (ADR-042).
# Secrets live here: keep it out of git and `chmod 600 deploy.yaml`.

# ── Target host (where Lotsa runs) ──────────────────────────────────────────
host: root@your-server          # ssh target; DNS A-record below must point here
# port: 22                      # optional, if ssh isn't on 22

# ── Public access (TLS + basic auth via Caddy) ──────────────────────────────
domain: lotsa.example.com
admin_email: you@example.com    # Let's Encrypt account (recommended)
basic_auth:
  user: admin
  password: change-me-to-something-strong

# ── Agent credentials (headless — no keychain on a server). Set ONE. ────────
# For the OAuth token: run `claude setup-token` locally.
anthropic_api_key:
# claude_code_oauth_token:
github_token:                   # enables push + pull-request features

# ── Projects (the git repos Lotsa runs tasks against) ───────────────────────
projects:
  - id: demo
    repo:                       # empty → seeds an empty demo repo so doctor passes
model: sonnet
# budget: 25                    # optional; USD cap per agent dispatch (default 5).
                                # A task spends a multiple of this across its steps.

# ── Install source (optional). Default: PyPI, pinned to this CLI's version. ──
# version: 0.1.0                # pin a specific PyPI release
# git: https://github.com/lotsadev/lotsa.git   # install from git instead
"""


def init_template() -> str:
    """The commented scaffold written by ``lotsa deploy --init``."""
    return INIT_TEMPLATE


def load_config(path: Path) -> dict[str, Any]:
    if not path.is_file():
        raise DeployError(f"{path} not found. Run `lotsa deploy --init` to scaffold one.")
    try:
        data = yaml.safe_load(path.read_text()) or {}
    except yaml.YAMLError as exc:
        raise DeployError(f"could not parse {path}: {exc}") from exc
    if not isinstance(data, dict):
        raise DeployError(f"{path} must be a YAML mapping, got {type(data).__name__}.")
    return data


def _projects_env(projects: Any) -> dict[str, str]:
    """Map the ``projects:`` list onto install.sh's PROJECT_* contract.

    One project → PROJECT_ID/PROJECT_REPO (the single-repo shorthand). Several →
    PROJECT_REPOS as space-separated ``id=url`` pairs.
    """
    if not projects:
        return {}
    if not isinstance(projects, list):
        raise DeployError("`projects` must be a list of {id, repo} entries.")
    pairs: list[tuple[str, str]] = []
    for i, proj in enumerate(projects):
        if not isinstance(proj, dict) or "id" not in proj:
            raise DeployError(f"projects[{i}] must be a mapping with an `id`.")
        pairs.append((str(proj["id"]), str(proj.get("repo") or "")))
    if len(pairs) == 1:
        pid, repo = pairs[0]
        return {"PROJECT_ID": pid, "PROJECT_REPO": repo}
    if any(not repo for _, repo in pairs):
        raise DeployError("every project needs a `repo` when more than one is configured.")
    return {"PROJECT_REPOS": " ".join(f"{pid}={repo}" for pid, repo in pairs)}


def config_to_env(cfg: dict[str, Any]) -> dict[str, str]:
    """Translate deploy.yaml into the env vars install.sh's deploy.env expects.

    Raises DeployError on a missing required field so the operator fails before
    anything is shipped to the box.
    """
    domain = cfg.get("domain")
    if not domain:
        raise DeployError("`domain` is required in deploy.yaml.")

    basic = cfg.get("basic_auth") or {}
    user, password = basic.get("user"), basic.get("password")
    if not user or not password:
        raise DeployError("`basic_auth.user` and `basic_auth.password` are required.")

    api_key = cfg.get("anthropic_api_key") or ""
    oauth = cfg.get("claude_code_oauth_token") or ""
    if not api_key and not oauth:
        raise DeployError(
            "set `anthropic_api_key` or `claude_code_oauth_token` in deploy.yaml "
            "(run `claude setup-token` locally for the OAuth token)."
        )

    env: dict[str, str] = {
        "LOTSA_DOMAIN": str(domain),
        "LOTSA_BASIC_USER": str(user),
        "LOTSA_BASIC_PASS": str(password),
        "LOTSA_MODEL": str(cfg.get("model") or "sonnet"),
    }
    if cfg.get("admin_email"):
        env["LOTSA_ADMIN_EMAIL"] = str(cfg["admin_email"])
    if api_key:
        env["ANTHROPIC_API_KEY"] = str(api_key)
    if oauth:
        env["CLAUDE_CODE_OAUTH_TOKEN"] = str(oauth)
    if cfg.get("github_token"):
        env["GITHUB_TOKEN"] = str(cfg["github_token"])
    if cfg.get("budget") is not None:
        try:
            budget = float(cfg["budget"])
        except (TypeError, ValueError) as exc:
            raise DeployError(f"`budget` must be a number (USD per agent dispatch), got {cfg['budget']!r}.") from exc
        if budget <= 0:
            raise DeployError(f"`budget` must be greater than 0, got {budget}.")
        env["LOTSA_BUDGET"] = repr(budget)
    if cfg.get("version"):
        env["LOTSA_VERSION"] = str(cfg["version"])
    if cfg.get("git"):
        env["LOTSA_GIT"] = str(cfg["git"])
    env.update(_projects_env(cfg.get("projects")))
    return env


# --------------------------------------------------------------------------- #
# deploy.env rendering (sourced by install.sh: `set -a; . deploy.env`)
# --------------------------------------------------------------------------- #
def _sh_single_quote(value: str) -> str:
    """POSIX-safe single-quoting for a value sourced by the shell."""
    return "'" + value.replace("'", "'\\''") + "'"


def render_deploy_env(env: dict[str, str]) -> str:
    lines = [
        "# Generated by `lotsa deploy` — do not edit by hand. Contains secrets.",
    ]
    for key in sorted(env):
        lines.append(f"{key}={_sh_single_quote(env[key])}")
    return "\n".join(lines) + "\n"


# --------------------------------------------------------------------------- #
# Orchestration
# --------------------------------------------------------------------------- #
def _ssh_base(host_args: dict[str, Any], binary: str) -> list[str]:
    port = host_args.get("port")
    flag = "-p" if binary == "ssh" else "-P"  # scp uses -P
    base = [binary]
    if port:
        base += [flag, str(port)]
    return base


def run_deploy(
    cfg: dict[str, Any],
    *,
    host: str | None = None,
    wheel: Path | None = None,
    remote_dir: str = REMOTE_DIR,
    dry_run: bool = False,
    runner: Callable[[list[str]], int] | None = None,
    echo: Callable[[str], None] = print,
) -> None:
    """Render config, ship the assets + deploy.env, and run the installer.

    `runner` runs a command and returns its exit code (defaults to a real
    subprocess); injecting it keeps the orchestration unit-testable. Secrets only
    ever travel inside the 0600 deploy.env file, never on a command line.
    """
    target = host or cfg.get("host")
    if not target:
        raise DeployError("no target host — set `host` in deploy.yaml or pass --host.")

    env = config_to_env(cfg)
    src = assets_dir()
    for name in ASSET_FILES:
        if not (src / name).is_file():
            raise DeployError(f"bundled asset missing: {name} (looked in {src})")

    if wheel is not None:
        if not wheel.is_file():
            raise DeployError(f"--wheel not found: {wheel}")
        # Override the install source: ship the wheel, point LOTSA_WHEEL at it.
        env["LOTSA_WHEEL"] = f"{remote_dir}/{wheel.name}"

    def _run(cmd: list[str]) -> None:
        echo("  $ " + " ".join(cmd))
        if dry_run:
            return
        code = (runner or _subprocess_run)(cmd)
        if code != 0:
            raise DeployError(f"command failed ({code}): {' '.join(cmd)}")

    ssh = _ssh_base(cfg, "ssh")
    scp = _ssh_base(cfg, "scp")

    echo(f"Deploying to {target} (assets from {src})")
    _run([*ssh, target, f"mkdir -p {remote_dir}"])

    asset_paths = [str(src / name) for name in ASSET_FILES]
    _run([*scp, *asset_paths, f"{target}:{remote_dir}/"])
    if wheel is not None:
        _run([*scp, str(wheel), f"{target}:{remote_dir}/"])

    # Render deploy.env to a 0600 temp file, ship it, then always remove it
    # locally — even on a dry run or a failure, it holds secrets.
    tmp = Path(tempfile.mkdtemp(prefix="lotsa-deploy-")) / "deploy.env"
    try:
        tmp.write_text(render_deploy_env(env))
        tmp.chmod(0o600)
        _run([*scp, str(tmp), f"{target}:{remote_dir}/deploy.env"])
    finally:
        shutil.rmtree(tmp.parent, ignore_errors=True)

    # scp doesn't preserve mode: lock the shipped deploy.env down and make the
    # scripts executable before running the installer.
    _run(
        [
            *ssh,
            target,
            f"cd {remote_dir} && chmod 600 deploy.env && chmod +x install.sh check-sandbox.sh && ./install.sh",
        ]
    )
    echo(f"Done. Dashboard: https://{env['LOTSA_DOMAIN']}")


def _subprocess_run(cmd: list[str]) -> int:
    return subprocess.run(cmd).returncode
