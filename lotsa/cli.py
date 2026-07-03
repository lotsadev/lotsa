"""Click CLI for Lotsa Community Edition."""

from __future__ import annotations

import logging
import os
import subprocess
import sys
from collections import deque
from pathlib import Path
from typing import TYPE_CHECKING

import click
import yaml

from lotsa.config import LotsaConfig
from lotsa.preflight import Severity, format_line, run_all_checks

if TYPE_CHECKING:
    from rigg.models import ActivityEvent


@click.group()
@click.version_option(package_name="lotsa")
def cli() -> None:
    """Lotsa — local runner for the Rigg SDK."""


@cli.command()
@click.argument("data_dir", required=False, type=click.Path(path_type=Path))
def init(data_dir: Path | None) -> None:
    """Initialize a Lotsa directory with a config file.

    ``data_dir`` defaults to ``~/.lotsa`` — the single directory that holds
    ``lotsa.yaml``, ``lotsa.db``, and the per-task worktrees. Pass an
    explicit path to put everything somewhere else (useful for portable
    project setups).
    """
    if data_dir is None:
        data_dir = Path.home() / ".lotsa"
    data_dir.mkdir(parents=True, exist_ok=True)

    # Config file
    config_file = data_dir / "lotsa.yaml"
    if not config_file.exists():
        # Emit the active config keys as YAML, then append a commented
        # template for the optional ``tools:`` / ``engines:`` / ``processes:``
        # blocks so users discover the extension surface without reading the
        # source. See ADR-014 for the typed-job model that these blocks feed.
        config_file.write_text(
            yaml.dump(
                {
                    "model": "sonnet",
                    "budget": 5.0,
                    # ADR-034 §2 / ADR-043 — new tasks default to ``chat`` (start
                    # as a conversation, hand off into an Execute process
                    # (``build``/``fix``) when ready). The full catalog still
                    # loads; ``flow:`` only picks which process the new-task
                    # picker pre-selects.
                    "flow": "chat",
                },
                default_flow_style=False,
                sort_keys=False,
            )
            + (
                "\n"
                "# Projects: the git repos Lotsa runs tasks against (ADR-029). Each id\n"
                "# matches [a-z0-9_-] and ``path`` must be an existing git repo (~ is\n"
                "# expanded). The new-task picker and sidebar filter list these; a single\n"
                "# project is shown without a picker. With NO projects block, Lotsa seeds\n"
                "# one ``default`` project from the directory you launch in.\n"
                "#\n"
                "# projects:\n"
                "#   myapp:\n"
                "#     name: My App\n"
                "#     path: ~/code/myapp\n"
                "\n"
                "# Optional: cap on tokens Claude Code may emit in a single response.\n"
                "# Leave commented to use Claude Code's built-in 32000 default (or\n"
                "# whatever the operator has exported via the\n"
                "# CLAUDE_CODE_MAX_OUTPUT_TOKENS environment variable). Raise this\n"
                '# when tasks fail with "response exceeded the N output token\n'
                '# maximum" and the work genuinely needs more output.\n'
                "#\n"
                "# max_output_tokens: 128000\n"
                "\n"
                "# Optional: register custom action tools and monitor engines\n"
                "# referenced from a custom process YAML (--flow-file). Each entry\n"
                "# is ``name: 'dotted.module:callable'`` and is imported at startup.\n"
                "# SECURITY: importing runs that module's code, so these blocks make\n"
                "# this file executable — only add paths you wrote or fully trust.\n"
                "# Built-ins (``push_pr`` tool, ``pr_monitor`` engine) are always\n"
                "# available; only list third-party additions here.\n"
                "#\n"
                "# tools:\n"
                "#   my_tool: my_package.tools:my_tool\n"
                "# engines:\n"
                "#   my_engine: my_package.engines:MyEngineClass\n"
                "\n"
                "# Optional: define agent-only processes inline. Each entry is a\n"
                "# named process with its own ``steps:`` list. Step prompts are\n"
                "# loaded as ``<basename>-system.md`` and ``<basename>-user.md``\n"
                "# from the per-process ``prompts_dir`` (defaults to ./prompts).\n"
                "# Add ``default: true`` to one entry to pre-select it in the\n"
                "# new-task picker; otherwise ``flow:`` above selects the default.\n"
                "# The full bundled catalog plus every inline process is always\n"
                "# loaded (ADR-034) — ``--flow``/``--process`` only sets which one\n"
                "# the picker pre-selects; any loaded process is pickable per task.\n"
                "#\n"
                "# processes:\n"
                "#   marketing_research:\n"
                "#     default: true\n"
                "#     prompts_dir: ./prompts/mkt\n"
                "#     steps:\n"
                "#       - { name: research, prompt: research }\n"
                "#       - { name: synthesize, prompt: synthesize }\n"
                "#   support_triage:\n"
                "#     prompts_dir: ./prompts/support\n"
                "#     steps:\n"
                "#       - { name: triage, prompt: triage }\n"
            )
        )
        click.echo(f"Created {config_file}")

    click.echo(f"\nLotsa directory ready: {data_dir}/")
    click.echo("\nQuick start:")
    if data_dir == Path.home() / ".lotsa":
        click.echo("  Run:  lotsa serve")
    else:
        click.echo(f"  Run:  lotsa serve --data-dir {data_dir}")
    click.echo("  Then open the dashboard in your browser to create your first task.")


@cli.command()
@click.option("--tag", default="lotsa-agent:latest", help="Docker image tag")
def build(tag: str) -> None:
    """Build the Docker image for sandboxed agent execution."""
    dockerfile = Path(__file__).parent / "Dockerfile.agent"
    if not dockerfile.exists():
        click.echo(f"Error: {dockerfile} not found", err=True)
        raise SystemExit(1)

    click.echo(f"Building {tag} from {dockerfile}...")
    result = subprocess.run(
        ["docker", "build", "-t", tag, "-f", str(dockerfile), "."],
        check=False,
    )
    if result.returncode != 0:
        click.echo("Build failed.", err=True)
        raise SystemExit(result.returncode)
    click.echo(f"Built {tag}")


@cli.command()
@click.option(
    "--config",
    "config_path",
    default="deploy.yaml",
    type=click.Path(path_type=Path),
    help="Path to deploy.yaml (default: ./deploy.yaml)",
)
@click.option("--init", "do_init", is_flag=True, default=False, help="Scaffold a commented deploy.yaml and exit")
@click.option("--host", default=None, help="Override the ssh target from deploy.yaml (user@host)")
@click.option(
    "--wheel",
    default=None,
    type=click.Path(exists=True, path_type=Path),
    help="Deploy a local wheel instead of installing from PyPI (dev/contributor path)",
)
@click.option("--dry-run", is_flag=True, default=False, help="Print the ssh/scp commands without running them")
def deploy(config_path: Path, do_init: bool, host: str | None, wheel: Path | None, dry_run: bool) -> None:
    """Deploy Lotsa to a single Debian/Ubuntu + systemd host (ADR-042).

    Reads ``deploy.yaml``, ships the bundled installer + a rendered ``deploy.env``
    to the target over ssh/scp, and runs the installer (PyPI install by default).
    """
    from lotsa import deploy as deploy_mod

    if do_init:
        if config_path.exists():
            click.echo(f"{config_path} already exists — not overwriting.", err=True)
            raise SystemExit(1)
        config_path.write_text(deploy_mod.init_template())
        config_path.chmod(0o600)
        click.echo(f"Wrote {config_path} (chmod 600). Fill it in, then run `lotsa deploy`.")
        return

    try:
        cfg = deploy_mod.load_config(config_path)
        deploy_mod.run_deploy(cfg, host=host, wheel=wheel, dry_run=dry_run, echo=click.echo)
    except deploy_mod.DeployError as exc:
        click.echo(f"Error: {exc}", err=True)
        raise SystemExit(1) from exc


@cli.command()
@click.argument("task_id")
@click.argument("process")
@click.option("--context", default=None, help="Free-text context seeded as the generic 'promotion_context' artifact")
@click.option("--host", default="127.0.0.1", help="Dashboard bind address (default: 127.0.0.1)")
@click.option("--port", default=8420, type=int, help="Dashboard port (default: 8420)")
def promote(task_id: str, process: str, context: str | None, host: str, port: int) -> None:
    """Promote TASK_ID to a different loaded PROCESS (ADR-027).

    Issues ``POST /api/tasks/<task_id>/promote`` against a running
    ``lotsa serve`` — the orchestrator owns the task's in-memory state, so this
    must talk to the live server rather than spinning up its own. For
    destination-specific handover (e.g. ``full``'s ``draft_spec``), use the
    dashboard's Promote modal; ``--context`` seeds the generic
    ``promotion_context`` artifact.
    """
    import contextlib
    import json
    import urllib.error
    import urllib.request

    body: dict[str, object] = {"to_process": process}
    if context:
        body["initial_artifacts"] = {"promotion_context": context}

    url = f"http://{host}:{port}/api/tasks/{task_id}/promote"
    req = urllib.request.Request(
        url,
        data=json.dumps(body).encode("utf-8"),
        headers={"Content-Type": "application/json"},
        method="POST",
    )
    try:
        with urllib.request.urlopen(req) as resp:
            data = json.loads(resp.read().decode("utf-8"))
        task = data.get("task", {})
        click.echo(
            f"Promoted {task_id} to {process} — now at state={task.get('state')!r}, step={task.get('current_step')!r}"
        )
    except urllib.error.HTTPError as exc:
        detail = ""
        with contextlib.suppress(Exception):
            detail = json.loads(exc.read().decode("utf-8")).get("detail", {}).get("error", "")
        click.echo(f"Promote failed ({exc.code}): {detail or exc.reason}", err=True)
        raise SystemExit(1) from None
    except urllib.error.URLError as exc:
        click.echo(
            f"Could not reach Lotsa server at {host}:{port} ({exc.reason}). Is `lotsa serve` running?",
            err=True,
        )
        raise SystemExit(1) from None


@cli.command()
@click.option("--model", default=None, help="Claude model (default: sonnet)")
@click.option("--budget", default=None, type=float, help="Max budget in USD (default: 5.0)")
@click.option(
    "--max-output-tokens",
    default=None,
    type=int,
    help=(
        "Cap on tokens Claude Code may emit per response. Unset uses Claude "
        "Code's built-in default (or whatever the operator has exported via "
        "the CLAUDE_CODE_MAX_OUTPUT_TOKENS env var)."
    ),
)
@click.option("--work-dir", default=None, type=click.Path(path_type=Path), help="Working directory for agent")
@click.option("--prompts-dir", default=None, type=click.Path(path_type=Path), help="Custom prompts directory")
@click.option(
    "--flow",
    default=None,
    help=(
        "Process the new-task picker pre-selects. Bundled (ADR-043): chat (Think) / "
        "build (Execute, full depth) / fix (Execute, shallow). Custom: any name "
        "defined in lotsa.yaml's `processes:` block. The full catalog always loads; "
        "this only sets the default selection. Default: the inline entry with "
        "default:true, or 'chat' if none."
    ),
)
@click.option(
    "--process",
    "process",
    default=None,
    help="Alias for --flow. Use whichever reads better; either picks the active process.",
)
@click.option("--flow-file", default=None, type=click.Path(exists=True, path_type=Path), help="Custom flow YAML file")
@click.option("--docker", is_flag=True, default=False, help="Run agent inside a Docker container")
@click.option("--docker-image", default=None, help="Docker image (default: lotsa-agent:latest)")
@click.option(
    "--runner",
    default=None,
    help=(
        "Agent runner shape (ADR-028). Default: the CLI runner (claude). Set "
        "'claude-agent-sdk' for the SDK-shaped runner (requires "
        "ANTHROPIC_API_KEY). Overrides --docker when set."
    ),
)
@click.option("--config", "config_path", default=None, type=click.Path(path_type=Path), help="Config file path")
@click.option("--data-dir", default=None, type=click.Path(path_type=Path), help="Data directory (default: ~/.lotsa)")
@click.option("--port", default=8420, type=int, help="Server port (default: 8420)")
@click.option("--host", default="127.0.0.1", help="Bind address (default: 127.0.0.1)")
@click.option(
    "--yes",
    "-y",
    "assume_yes",
    is_flag=True,
    default=False,
    help=(
        "Pre-acknowledge all startup CONFIRM prompts (e.g. missing GITHUB_TOKEN). "
        "Also via LOTSA_ASSUME_YES. Needed for headless/CI starts."
    ),
)
@click.option(
    "--dangerously-skip-permissions",
    "skip_permissions",
    is_flag=True,
    default=False,
    help=(
        "Disable the OS sandbox and run the agent with all permissions bypassed "
        "(ADR-038). The agent can then modify the host — only for hosts without a "
        "sandbox. NOT recommended; prefer --docker or installing the sandbox."
    ),
)
def serve(
    model: str | None,
    budget: float | None,
    max_output_tokens: int | None,
    work_dir: Path | None,
    prompts_dir: Path | None,
    flow: str | None,
    process: str | None,
    flow_file: Path | None,
    docker: bool,
    docker_image: str | None,
    runner: str | None,
    config_path: Path | None,
    data_dir: Path | None,
    port: int,
    host: str,
    assume_yes: bool,
    skip_permissions: bool,
) -> None:
    """Start the web dashboard.

    Reads ``lotsa.yaml`` from ``--data-dir`` (default ``~/.lotsa``). Run
    ``lotsa init`` first if no config exists yet, or supply ``--config
    <path>`` to point at a YAML elsewhere.
    """
    # Strip Claude Code nesting vars so child claude processes
    # use their own auth (keychain) instead of inheriting parent session
    for var in ("CLAUDECODE", "CLAUDE_CODE_ENTRYPOINT"):
        os.environ.pop(var, None)

    if host != "127.0.0.1" and host != "localhost":
        click.echo(
            f"Warning: binding to {host} exposes the dashboard without authentication. "
            "Anyone on the network can create tasks and approve agent actions.",
            err=True,
        )

    # ``--process`` is an alias for ``--flow``. Conflict handling: if both
    # are passed, ``--process`` wins (it's the newer / clearer name); we
    # warn so the operator notices the deprecated flag was ignored.
    if process is not None and flow is not None and process != flow:
        click.echo(
            f"Both --flow={flow!r} and --process={process!r} given; using --process={process!r}.",
            err=True,
        )
    selected_flow = process if process is not None else flow

    config = LotsaConfig.load(
        config_path=config_path,
        model=model,
        budget=budget,
        max_output_tokens=max_output_tokens,
        work_dir=work_dir,
        data_dir=data_dir,
        prompts_dir=prompts_dir,
        flow=selected_flow,
        flow_file=flow_file,
        docker=docker or None,
        docker_image=docker_image,
        runner=runner,
        skip_permissions=skip_permissions or None,
    )

    # Hard cutover: no config found means the user hasn't run lotsa init
    # against the resolved data_dir. Tell them what to do rather than
    # silently starting with defaults that point at a non-existent
    # ``lotsa.db``.
    if config.config_path is None:
        click.echo(
            f"Error: no lotsa.yaml found at {config.data_dir}/lotsa.yaml.\n"
            f"Run `lotsa init{'' if config.data_dir == Path.home() / '.lotsa' else f' {config.data_dir}'}` "
            f"to scaffold one, or supply `--config <path>` to point at a YAML elsewhere.",
            err=True,
        )
        raise SystemExit(1)

    # ADR-036 §2 — gate startup on the same preflight checks `lotsa doctor`
    # runs. ``LOTSA_ASSUME_YES`` is the env equivalent of ``--yes``.
    _run_preflight_gate(config, assume_yes or _env_truthy("LOTSA_ASSUME_YES"))

    logging.basicConfig(
        level=logging.INFO,
        format="%(asctime)s [%(levelname)s] %(name)s: %(message)s",
    )

    from lotsa.server.app import create_app

    app = create_app(config)

    import signal
    import threading

    import uvicorn

    # Run uvicorn in a daemon thread. The main thread handles Ctrl-C.
    server = uvicorn.Server(uvicorn.Config(app, host=host, port=port, log_level="info"))
    server_thread = threading.Thread(target=server.run, daemon=True)
    server_thread.start()

    # Signal handler: try graceful shutdown first, force-exit as fallback.
    # Default KeyboardInterrupt delivery is unreliable on macOS when
    # uvicorn's event loop is running in another thread.
    def _handle_shutdown(*_args):
        server.should_exit = True
        server_thread.join(timeout=3)
        os._exit(0)

    signal.signal(signal.SIGINT, _handle_shutdown)
    signal.signal(signal.SIGTERM, _handle_shutdown)

    while server_thread.is_alive():
        server_thread.join(timeout=1.0)


@cli.command()
@click.option("--config", "config_path", default=None, type=click.Path(path_type=Path), help="Config file path")
@click.option("--data-dir", default=None, type=click.Path(path_type=Path), help="Data directory (default: ~/.lotsa)")
def doctor(config_path: Path | None, data_dir: Path | None) -> None:
    """Run first-run preflight checks and report problems (ADR-036).

    The same checks `lotsa serve` gates on at startup — run this to diagnose a
    misconfigured install without starting the server. Exits non-zero if any
    FATAL check fails (so it doubles as a CI/healthcheck probe).
    """
    config = LotsaConfig.load(config_path=config_path, data_dir=data_dir)
    results = run_all_checks(config)
    for result in results:
        click.echo(format_line(result))

    fatal = [r for r in results if r.severity is Severity.FATAL and not r.ok]
    confirm = [r for r in results if r.severity is Severity.CONFIRM and not r.ok]
    click.echo("")
    if fatal:
        click.echo(f"{len(fatal)} blocking issue(s) — `lotsa serve` will refuse to start.")
        raise SystemExit(1)
    if confirm:
        click.echo(f"Ready, but {len(confirm)} item(s) need confirmation at startup (or pass --yes).")
    else:
        click.echo("All checks passed — `lotsa serve` is good to go.")


def _env_truthy(name: str) -> bool:
    """True when an env var is set to a non-empty, non-falsey value."""
    return os.environ.get(name, "").strip().lower() not in ("", "0", "false", "no")


def _run_preflight_gate(config: LotsaConfig, assume_yes: bool, *, isatty: bool | None = None) -> None:
    """Gate `lotsa serve` on the preflight checks (ADR-036 §2).

    FATAL failures abort. CONFIRM failures need acknowledgement: ``--yes`` /
    ``LOTSA_ASSUME_YES`` pre-acknowledges them; otherwise we prompt, and in a
    non-interactive terminal we fail closed so a misconfigured deployment
    surfaces the gap instead of running degraded forever. WARN items just print.

    ``isatty`` is injectable for testing; it defaults to stdin's tty status.
    """
    if isatty is None:
        isatty = sys.stdin.isatty()

    results = run_all_checks(config)
    for result in results:
        click.echo(format_line(result), err=True)

    if any(r.severity is Severity.FATAL and not r.ok for r in results):
        click.echo("\nlotsa serve: refusing to start — fix the ✖ items above (run `lotsa doctor`).", err=True)
        raise SystemExit(1)

    for result in (r for r in results if r.severity is Severity.CONFIRM and not r.ok):
        if assume_yes:
            click.echo(f"Continuing without {result.name} (--yes / LOTSA_ASSUME_YES).", err=True)
            continue
        if not isatty:
            click.echo(
                f"\n{result.name} needs confirmation, but this is not an interactive terminal. "
                "Re-run with --yes (or set LOTSA_ASSUME_YES=1) to proceed.",
                err=True,
            )
            raise SystemExit(1)
        if not click.confirm(f"Continue without {result.name}? ({result.detail})", default=False):
            click.echo("Aborted.", err=True)
            raise SystemExit(1)


# Fixed page size for draining the session JSONL in ``lotsa inspect``. Kept
# independent of the display ``--limit`` so a small ``--limit`` doesn't fan out
# into many small file reads (each ``read_activity`` re-reads from the start).
_DRAIN_BATCH = 200


def _format_inspect_event(ev: ActivityEvent) -> str:
    """One terminal-friendly line per activity event for ``lotsa inspect``."""
    ts = ev.timestamp.strftime("%H:%M:%S") if getattr(ev, "timestamp", None) else "--:--:--"
    return f"[{ev.index:>4}] {ts}  {ev.kind:<11} {ev.summary}"


@cli.command()
@click.argument("task_id")
@click.option(
    "--limit",
    default=50,
    type=click.IntRange(min=1),
    help="Max events to print (default: 50)",
)
@click.option("--since", default=0, type=int, help="Only events with index >= since (default: 0)")
@click.option("--watch", is_flag=True, default=False, help="Re-poll every 2s like the dashboard")
@click.option("--data-dir", default=None, type=click.Path(path_type=Path), help="Data directory (default: ~/.lotsa)")
def inspect(task_id: str, limit: int, since: int, watch: bool, data_dir: Path | None) -> None:
    """Print recent agent activity for TASK_ID (ADR-017).

    Reads the task's ``session_id`` from the local SQLite store and tails the
    Claude Code session JSONL via the shared parser — no running server needed.
    Useful for headless/CI debugging and for developing new runners.

    Without ``--watch`` it prints the *last* ``--limit`` events (the tail, like
    ``tail -n``), not the first ``--limit`` from the start of the session.
    ``--watch`` then follows new events live until the session completes (like
    ``tail -f``).
    """
    import asyncio

    from lotsa.db import TaskDB
    from rigg.activity import read_activity

    config = LotsaConfig.load(data_dir=data_dir)

    async def _drain(
        session_id: str, work_dir: Path, start: int, keep_last: int | None = None
    ) -> tuple[list[ActivityEvent], int, bool]:
        """Read every event from index *start* to the end of the session.

        Pages ``read_activity`` in fixed ``_DRAIN_BATCH``-sized reads until a
        batch yields no new events (``next_index`` stops advancing) so an
        arbitrarily long backlog drains in full. The batch size is deliberately
        decoupled from the display ``--limit``: a small ``--limit`` must not turn
        into many tiny file reads. Returns ``(events, next_index, session_complete)``.

        ``keep_last`` bounds the in-memory accumulator to the last N events via a
        ring buffer — the initial tail only ever prints ``events[-limit:]``, so
        holding the whole (possibly 10k-event) backlog just to slice the tail off
        wastes memory. The cursor still advances to the true end so a subsequent
        ``--watch`` resumes correctly. ``None`` keeps everything (the follow loop
        must surface *all* new events, not just a tail). The per-batch from-zero
        re-read in ``_read_activity_sync`` is a separate, documented deferral.
        """
        collected: deque[ActivityEvent] | list[ActivityEvent] = deque(maxlen=keep_last) if keep_last is not None else []
        cursor = start
        while True:
            result = await read_activity(session_id, work_dir, cursor, _DRAIN_BATCH)
            collected.extend(result.events)
            if result.next_index == cursor:
                return list(collected), cursor, result.session_complete
            cursor = result.next_index

    async def _run() -> int:
        db = TaskDB(config.data_dir / "lotsa.db")
        await db.initialize()
        try:
            row = await db.get_task(task_id)
            if row is None:
                click.echo(f"Task {task_id} not found.", err=True)
                return 1
            session_id = row.metadata.get("session_id")
            if not session_id:
                click.echo("No session_id yet — the agent has not dispatched for this task.")
                return 0
            work_dir = config.data_dir / "worktrees" / task_id

            # Initial read: drain to the end, keeping only the *last* ``limit``
            # events (ADR-017 §7 — "the last N"), matching ``tail -n``. ``--limit``
            # is ``IntRange(min=1)``, so ``keep_last`` bounds the accumulator to a
            # non-empty tail rather than holding the whole backlog to slice it.
            events, cursor, complete = await _drain(session_id, work_dir, since, keep_last=limit)
            for ev in events:
                click.echo(_format_inspect_event(ev))
            if not watch:
                return 0

            # Follow: print new events as they arrive until the session's final
            # ``summary`` record lands AND the backlog is fully drained — never
            # break mid-batch on ``session_complete`` (that dropped events).
            while not complete:
                await asyncio.sleep(2)
                new_events, cursor, complete = await _drain(session_id, work_dir, cursor)
                for ev in new_events:
                    click.echo(_format_inspect_event(ev))
            return 0
        finally:
            await db.close()

    raise SystemExit(asyncio.run(_run()))
