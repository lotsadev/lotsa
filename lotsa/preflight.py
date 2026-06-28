"""Startup preflight checks — back both `lotsa doctor` and the `lotsa serve` gate.

A misconfigured install should say what is wrong upfront, not limp along and
fail cryptically at first dispatch (ADR-036 §2). Each check returns a
:class:`CheckResult` carrying a severity:

- **FATAL**   — refuse to start.
- **CONFIRM** — interactive acknowledgement required (the operator must opt in).
- **WARN**    — note and continue.

The checks are pure (env / filesystem / subprocess reads, no IO side effects),
so the interactive gating and reporting live in the CLI and stay easy to test.
"""

from __future__ import annotations

import os
import shutil
import subprocess
import sys
from dataclasses import dataclass
from enum import StrEnum

from lotsa.config import LotsaConfig, resolve_project_specs


class Severity(StrEnum):
    FATAL = "fatal"
    CONFIRM = "confirm"
    WARN = "warn"


@dataclass(frozen=True)
class CheckResult:
    name: str
    severity: Severity
    ok: bool
    detail: str
    remedy: str | None = None


def check_claude_cli(config: LotsaConfig) -> CheckResult:
    """The `claude` CLI must be on PATH — every runner drives it (ADR-028)."""
    found = shutil.which("claude") is not None
    return CheckResult(
        "claude-cli",
        Severity.FATAL,
        found,
        "claude CLI found on PATH" if found else "claude CLI not found on PATH",
        None
        if found
        else "Install the Claude Code CLI and ensure `claude` is on PATH (https://docs.claude.com/claude-code).",
    )


def check_agent_auth(config: LotsaConfig) -> CheckResult:
    """Agent authentication.

    The SDK runner (ADR-028) truly needs ``ANTHROPIC_API_KEY`` — missing it is
    FATAL. **Docker mode** needs an env credential too: ``claude login`` keychain
    lives on the host and can't cross into the container, and the runner only
    forwards ``ANTHROPIC_API_KEY``/``CLAUDE_CODE_OAUTH_TOKEN`` via ``-e`` — so a
    missing env var there is FATAL. The default *native* CLI runner also accepts
    keychain credentials (invisible from the environment), so an absent env var
    there is only a WARN rather than a hard failure that would lock out the most
    common setup.
    """
    has_key = bool(os.environ.get("ANTHROPIC_API_KEY"))
    has_oauth = bool(os.environ.get("CLAUDE_CODE_OAUTH_TOKEN"))

    if config.runner == "claude-agent-sdk":
        detail = (
            "ANTHROPIC_API_KEY set"
            if has_key
            else "ANTHROPIC_API_KEY missing (the claude-agent-sdk runner requires it)"
        )
        remedy = (
            None
            if has_key
            else "export ANTHROPIC_API_KEY=… — the SDK runner needs a key (the CLI runner can use `claude login`)."
        )
        return CheckResult("agent-auth", Severity.FATAL, has_key, detail, remedy)

    if config.docker:
        ok = has_key or has_oauth
        detail = (
            "agent auth env var set (forwarded into the container)"
            if ok
            else "no ANTHROPIC_API_KEY / CLAUDE_CODE_OAUTH_TOKEN — docker can't use `claude login` keychain"
        )
        remedy = (
            None
            if ok
            else "export ANTHROPIC_API_KEY=… (or CLAUDE_CODE_OAUTH_TOKEN) — keychain auth can't reach the container."
        )
        return CheckResult("agent-auth", Severity.FATAL, ok, detail, remedy)

    if has_key or has_oauth:
        return CheckResult("agent-auth", Severity.WARN, True, "agent auth env var set", None)
    return CheckResult(
        "agent-auth",
        Severity.WARN,
        False,
        "no ANTHROPIC_API_KEY / CLAUDE_CODE_OAUTH_TOKEN env var",
        "Relying on `claude` keychain login — run `claude login`. Agent tasks fail if no credential is configured.",
    )


def check_project_repo(config: LotsaConfig) -> CheckResult:
    """At least one project must resolve to a real git repository (ADR-029)."""
    try:
        specs = resolve_project_specs(config)
    except ValueError as exc:
        return CheckResult("project-repo", Severity.FATAL, False, "invalid projects configuration", str(exc))

    # The seeded ``default`` project is intentionally lenient (not git-validated),
    # so confirm at least one resolved project is actually a git repo.
    git_specs = [s for s in specs if (s.path / ".git").exists()]
    if git_specs:
        names = ", ".join(s.id for s in git_specs)
        return CheckResult("project-repo", Severity.FATAL, True, f"{len(git_specs)} git project(s): {names}", None)
    return CheckResult(
        "project-repo",
        Severity.FATAL,
        False,
        "no project resolves to a git repository",
        "Launch `lotsa serve` from inside a git repo, or add a `projects:` block to lotsa.yaml (ADR-029).",
    )


def check_dashboard_bundle(config: LotsaConfig) -> CheckResult:
    """The dashboard bundle must be present or buildable (ADR-036 §1).

    Mirrors ``_ensure_spa_built``'s verdict without building: a present (even
    stale) bundle is fine; a missing one is fatal only when it cannot be built.
    """
    from lotsa.server import app as appmod

    spa_index = appmod._STATIC_DIR / "dist" / "index.html"
    if not appmod._bundle_needs_rebuild(spa_index, appmod._FRONTEND_DIR):
        return CheckResult("dashboard-bundle", Severity.FATAL, True, "dashboard bundle present", None)

    if spa_index.exists():
        detail = "dashboard bundle present (stale; rebuilds at startup if npm is available)"
        return CheckResult("dashboard-bundle", Severity.FATAL, True, detail, None)

    has_source = (appmod._FRONTEND_DIR / "package.json").exists()
    if has_source and shutil.which("npm"):
        return CheckResult("dashboard-bundle", Severity.FATAL, True, "dashboard bundle will be built at startup", None)
    return CheckResult(
        "dashboard-bundle",
        Severity.FATAL,
        False,
        "dashboard bundle missing and cannot be built",
        "Run `make frontend` (needs Node.js/npm), or reinstall the packaged wheel which ships the bundle.",
    )


_DEFAULT_AGENT_IMAGE = "lotsa-agent:latest"


def check_docker(config: LotsaConfig) -> CheckResult:
    """Docker readiness — only meaningful when docker mode is active (ADR-036 §2).

    Checks, in order: the ``docker`` CLI is on PATH, the daemon is reachable, and
    the agent image exists. The default ``lotsa-agent:latest`` is a local-build
    tag (nothing to pull), so a missing one is FATAL with a ``lotsa build``
    remedy; a custom/registry image is only a WARN, since ``docker run`` will try
    to pull it. Without this, a missing image surfaces as a cryptic mid-run
    ``exit 125`` instead of an upfront message.
    """
    image = config.docker_image
    if shutil.which("docker") is None:
        return CheckResult(
            "docker",
            Severity.FATAL,
            False,
            "docker mode is on but the docker CLI is not on PATH",
            "Install Docker, or run without --docker / `docker: true`.",
        )
    try:
        info = subprocess.run(["docker", "info"], capture_output=True, text=True, timeout=15)
    except (FileNotFoundError, subprocess.TimeoutExpired):
        return CheckResult("docker", Severity.FATAL, False, "Docker daemon not reachable", "Start Docker and retry.")
    if info.returncode != 0:
        return CheckResult(
            "docker",
            Severity.FATAL,
            False,
            "Docker daemon not reachable",
            "Start Docker — the daemon isn't responding.",
        )

    inspect = subprocess.run(["docker", "image", "inspect", image], capture_output=True, text=True, timeout=15)
    if inspect.returncode == 0:
        return CheckResult("docker", Severity.FATAL, True, f"docker mode ready (image {image} present)", None)
    if image == _DEFAULT_AGENT_IMAGE:
        return CheckResult(
            "docker",
            Severity.FATAL,
            False,
            f"docker image {image} not built",
            "Run `lotsa build` to build the agent image before serving with --docker.",
        )
    return CheckResult(
        "docker",
        Severity.WARN,
        False,
        f"docker image {image} not present locally",
        f"docker will try to pull it on first run; `docker pull {image}` to pre-stage.",
    )


def check_host_sandbox(config: LotsaConfig) -> CheckResult:
    """The native runner confines the agent with Claude's OS sandbox (ADR-038).

    In practice this is reliable only on **macOS** (Seatbelt). On **Linux servers**
    Claude's sandbox HTTP bridge does not reliably start (validated on Ubuntu
    24.04 / claude 2.1.191), so native Linux is FATAL with a steer to ``--docker``
    (the container is the sandbox) — the deploy uses Docker on Linux for exactly
    this reason. ``--dangerously-skip-permissions`` opts out per launch (WARN).
    """
    if config.skip_permissions:
        return CheckResult(
            "host-sandbox",
            Severity.WARN,
            False,
            "agent runs UNSANDBOXED — --dangerously-skip-permissions is set (host is not protected)",
            "Drop the flag to require isolation; or run with --docker for container isolation.",
        )
    if sys.platform == "darwin":
        return CheckResult("host-sandbox", Severity.FATAL, True, "OS sandbox available (macOS Seatbelt)", None)
    if sys.platform.startswith("linux"):
        return CheckResult(
            "host-sandbox",
            Severity.FATAL,
            False,
            "Claude's native sandbox is unreliable on Linux (its sandbox HTTP bridge often fails to start)",
            "Use --docker for container isolation, or --dangerously-skip-permissions on a disposable host.",
        )
    return CheckResult(
        "host-sandbox",
        Severity.FATAL,
        False,
        f"OS sandbox unsupported on this platform ({sys.platform})",
        "Run with --docker, or pass --dangerously-skip-permissions (not recommended).",
    )


def check_github_token(config: LotsaConfig) -> CheckResult:
    """Missing GITHUB_TOKEN disables push/PR features — an explicit CONFIRM."""
    ok = bool(os.environ.get("GITHUB_TOKEN"))
    return CheckResult(
        "github-token",
        Severity.CONFIRM,
        ok,
        "GITHUB_TOKEN set" if ok else "GITHUB_TOKEN not set — push & pull-request features will be disabled",
        None if ok else "export GITHUB_TOKEN=… to enable pushing branches and opening pull requests.",
    )


def check_git_identity(config: LotsaConfig) -> CheckResult:
    """Agent commits need a git author identity; chat-only use does not (WARN)."""
    name = _git_config("user.name")
    email = _git_config("user.email")
    ok = bool(name and email)
    return CheckResult(
        "git-identity",
        Severity.WARN,
        ok,
        f"git identity set ({name} <{email}>)" if ok else "git user.name / user.email not set",
        None if ok else "Set `git config --global user.name` and `user.email` — agent commits need them.",
    )


def _git_config(key: str) -> str | None:
    try:
        out = subprocess.run(["git", "config", "--get", key], capture_output=True, text=True, check=False)
    except FileNotFoundError:
        return None
    return out.stdout.strip() or None


def run_all_checks(config: LotsaConfig) -> list[CheckResult]:
    """Run every preflight check, in report order."""
    checks = [
        check_claude_cli(config),
        check_agent_auth(config),
        check_project_repo(config),
        check_dashboard_bundle(config),
        check_github_token(config),
        check_git_identity(config),
    ]
    # Docker readiness only applies when docker mode is active (config or --docker).
    if config.docker:
        checks.append(check_docker(config))
    # Host sandbox applies to the native CLI runner only — Docker IS the sandbox,
    # and the SDK runner's sandbox is ADR-038 Phase 3 (not yet enforced here).
    elif config.runner != "claude-agent-sdk":
        checks.append(check_host_sandbox(config))
    return checks


_FAIL_SYMBOL = {Severity.FATAL: "✖", Severity.CONFIRM: "?", Severity.WARN: "⚠"}


def format_line(result: CheckResult) -> str:
    """One human-readable line per check, with a remedy line when it failed."""
    symbol = "✔" if result.ok else _FAIL_SYMBOL[result.severity]
    line = f"{symbol} {result.name}: {result.detail}"
    if not result.ok and result.remedy:
        line += f"\n    → {result.remedy}"
    return line
