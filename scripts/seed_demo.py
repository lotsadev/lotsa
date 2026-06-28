#!/usr/bin/env python3
"""Seed a throwaway Lotsa data-dir with synthetic demo data — for screenshots,
demos, and kicking the tyres without touching any real repo or task.

It creates a self-contained data directory:
  - a few fake projects, each backed by a freshly `git init`-ed local repo so
    project validation passes (no real code, no real history);
  - a curated *fleet* of tasks spanning every interesting state — chatting,
    speccing, the plan-approval gate, testing, coding, reviewing, waiting on a
    PR, complete, and blocked — with realistic titles, a few messages each, and
    timestamps backdated so the board reads like a busy, real product.

Nothing here runs an agent or makes a network call. Everything is fictional
(an "acme" org, made-up tasks). Use it to capture a clean dashboard screenshot:

    python scripts/seed_demo.py --data-dir ./lotsa-demo
    lotsa serve --data-dir ./lotsa-demo --yes
    # …then screenshot the dashboard.

Re-run with --force to wipe and regenerate.
"""

from __future__ import annotations

import argparse
import asyncio
import shutil
import subprocess
from datetime import UTC, datetime, timedelta
from pathlib import Path

from lotsa.db import TaskDB

# ── Fake projects (id, display name) ─────────────────────────────────────────
PROJECTS = [
    ("checkout-api", "Checkout API"),
    ("web-dashboard", "Web Dashboard"),
    ("worker", "Background Worker"),
]

# ── The demo fleet ───────────────────────────────────────────────────────────
# Each task: project, title, state, status, flow, current_step, age, plus an
# optional PR number and a short message script (role, step, type, text).
# `age` is a (value, unit) tuple used to backdate created_at/updated_at.
FLEET = [
    {
        "project": "checkout-api",
        "title": "Add rate limiting to POST /v1/checkout",
        "state": "coding",
        "status": "working",
        "flow": "full",
        "step": "code",
        "age": (8, "minutes"),
        "messages": [
            ("user", "", "chat", "Add a per-merchant rate limit to checkout — 60/min, 429 on breach."),
            ("agent", "spec", "spec", "SPEC_COMPLETE: Per-merchant rate limit on POST /v1/checkout."),
            ("system", "plan", "system", "Plan approved by operator."),
            ("agent", "code", "agent", "Implementing the token-bucket limiter in the checkout router…"),
        ],
    },
    {
        "project": "web-dashboard",
        "title": "Migrate settings page to the new design tokens",
        "state": "reviewing",
        "status": "working",
        "flow": "full",
        "step": "review",
        "age": (21, "minutes"),
        "messages": [
            ("agent", "code", "agent", "Settings page now uses shared design tokens; tests pass (14/14)."),
            ("agent", "review", "agent", "Reviewing the diff with fresh eyes for token coverage…"),
        ],
    },
    {
        "project": "checkout-api",
        "title": "Fix flaky auth-refresh integration test",
        "state": "planned",
        "status": "needs_input",
        "flow": "full",
        "step": "plan",
        "age": (35, "minutes"),
        "messages": [
            ("agent", "plan", "plan", "Cause: test asserts on wall-clock expiry. Plan: inject a fake clock."),
            ("system", "plan", "system", "Waiting for operator to approve the plan."),
        ],
    },
    {
        "project": "worker",
        "title": "Add OpenTelemetry traces to the job runner",
        "state": "complete",
        "status": "complete",
        "flow": "full",
        "step": None,
        "age": (2, "hours"),
        "pr": 128,
        "messages": [
            ("agent", "verify", "agent", "VERIFIED: spans emitted for enqueue/execute/retry."),
            ("system", "push_pr", "system", "Opened PR #128, watched it, merged after CI passed."),
        ],
    },
    {
        "project": "web-dashboard",
        "title": "Dark-mode toggle in the top nav",
        "state": "complete",
        "status": "complete",
        "flow": "full",
        "step": None,
        "age": (5, "hours"),
        "pr": 117,
        "messages": [
            ("system", "push_pr", "system", "Opened PR #117."),
            ("system", "wait_for_pr_signal", "system", "PR #117 merged. Task complete."),
        ],
    },
    {
        "project": "checkout-api",
        "title": "Bump fastapi 0.110 → 0.115 and fix deprecations",
        "state": "complete",
        "status": "complete",
        "flow": "quickfix",
        "step": None,
        "age": (7, "hours"),
        "pr": 109,
        "messages": [
            ("agent", "code", "agent", "Bumped the pin, migrated on_event→lifespan, tests green."),
            ("system", "wait_for_pr_signal", "system", "PR #109 merged. Task complete."),
        ],
    },
    {
        "project": "worker",
        "title": "Investigate intermittent queue stalls",
        "state": "chatting",
        "status": "working",
        "flow": "chat",
        "step": None,
        "age": (26, "minutes"),
        "messages": [
            ("user", "", "chat", "The worker stops draining the queue for ~30s every few hours."),
            ("agent", "", "chat", "Correlated with retry sweeps or GC pauses? Can you share logs?"),
        ],
    },
    {
        "project": "web-dashboard",
        "title": "Empty-state illustrations for the tasks list",
        "state": "testing",
        "status": "working",
        "flow": "full",
        "step": "test",
        "age": (12, "minutes"),
        "messages": [
            ("system", "plan", "system", "Plan approved by operator."),
            ("agent", "test", "agent", "Writing failing tests for empty/loading/error states first…"),
        ],
    },
    {
        "project": "checkout-api",
        "title": "Verify refund-webhook signature before processing",
        "state": "speccing",
        "status": "working",
        "flow": "full",
        "step": "spec",
        "age": (4, "minutes"),
        "messages": [
            ("user", "", "chat", "Verify the HMAC signature on inbound refund webhooks."),
            ("agent", "spec", "spec", "Drafting the spec — constant-time compare, replay window…"),
        ],
    },
    {
        "project": "worker",
        "title": "Retry policy for failed webhook deliveries",
        "state": "blocked",
        "status": "blocked",
        "flow": "full",
        "step": None,
        "age": (3, "hours"),
        "messages": [
            ("system", "code", "system", "Blocked: cap retries by count or elapsed time? Needs a decision."),
        ],
    },
]


def _iso_ago(value: int, unit: str) -> str:
    return (datetime.now(UTC) - timedelta(**{unit: value})).isoformat()


def _git_init(repo: Path) -> None:
    """Create a minimal but valid git repo so project validation passes."""
    repo.mkdir(parents=True, exist_ok=True)

    def run(*a: str) -> None:
        subprocess.run(["git", "-C", str(repo), *a], check=True, capture_output=True)

    run("init", "-q")
    (repo / "README.md").write_text(f"# {repo.name}\n\nDemo repo for a Lotsa screenshot. Not real.\n")
    run("add", "-A")
    run("-c", "user.email=demo@lotsa.dev", "-c", "user.name=Lotsa Demo", "commit", "-q", "-m", "init")


def _write_config(data_dir: Path, repos_dir: Path) -> None:
    lines = [
        "# Generated by scripts/seed_demo.py — a throwaway demo config.",
        "model: sonnet",
        "flow: chat",
        "projects:",
    ]
    for pid, name in PROJECTS:
        lines += [f"  {pid}:", f"    name: {name}", f"    path: {repos_dir / pid}"]
    (data_dir / "lotsa.yaml").write_text("\n".join(lines) + "\n")


async def _seed(db: TaskDB) -> None:
    for pid, name in PROJECTS:
        await db.upsert_project(pid, name, f"demo-repos/{pid}")

    for spec in FLEET:
        meta: dict = {"process_name": spec["flow"], "project_id": spec["project"], "current_flow": "main"}
        if spec.get("pr"):
            meta.update(
                {
                    "pr_number": spec["pr"],
                    "pr_url": f"https://github.com/acme/{spec['project']}/pull/{spec['pr']}",
                    "github_owner": "acme",
                    "github_repo": spec["project"],
                    "pr_checks_passing": 3,
                    "pr_checks_total": 3,
                    "pr_checks_failing": 0,
                }
            )
        task = await db.create_task(
            title=spec["title"],
            state=spec["state"],
            status=spec["status"],
            current_step=spec["step"],
            flow_name=spec["flow"],
            project_id=spec["project"],
            metadata=meta,
        )
        for role, step, mtype, text in spec["messages"]:
            await db.add_message(task.id, role, step, text, mtype)
        # Backdate so the board shows realistic relative times (create_task and
        # add_message both stamp "now"; update_task won't touch timestamps).
        ts = _iso_ago(*spec["age"])
        await db._execute("UPDATE tasks SET created_at = ?, updated_at = ? WHERE id = ?", (ts, ts, task.id))
    await db._commit()


async def _restore(db: TaskDB) -> None:
    """Re-apply the demo task states to an already-seeded DB.

    ``lotsa serve`` blocks any ``status='working'`` task on startup (the restart
    reconciliation), which flips the active demo tasks to ``blocked``. Run this
    AFTER the server is up to restore the intended live states for a clean shot.
    """
    for spec in FLEET:
        await db._execute(
            "UPDATE tasks SET state = ?, status = ?, current_step = ? WHERE title = ?",
            (spec["state"], spec["status"], spec["step"], spec["title"]),
        )
    await db._commit()


def main() -> None:
    ap = argparse.ArgumentParser(description="Seed a throwaway Lotsa demo data-dir (for screenshots/demos).")
    ap.add_argument("--data-dir", default="./lotsa-demo", help="Where to create the demo data (default ./lotsa-demo)")
    ap.add_argument("--force", action="store_true", help="Wipe an existing data-dir first")
    ap.add_argument(
        "--restore",
        action="store_true",
        help="Re-apply the demo task states to an existing data-dir. Run AFTER `lotsa serve` starts "
        "— its startup sweep flips the active 'working' tasks to 'blocked'.",
    )
    args = ap.parse_args()

    data_dir = Path(args.data_dir).expanduser().resolve()

    if args.restore:
        if not (data_dir / "lotsa.db").exists():
            raise SystemExit(f"No demo DB at {data_dir} — run without --restore first.")
        db = TaskDB(data_dir / "lotsa.db")

        async def run_restore() -> None:
            await db.initialize()
            try:
                await _restore(db)
            finally:
                await db.close()

        asyncio.run(run_restore())
        print(f"✓ Restored live demo states in {data_dir}. Refresh the dashboard and screenshot.")
        return

    if data_dir.exists():
        if not args.force:
            raise SystemExit(f"{data_dir} already exists — pass --force to wipe and regenerate.")
        shutil.rmtree(data_dir)
    repos_dir = data_dir / "demo-repos"
    data_dir.mkdir(parents=True)

    print(f"→ creating demo repos under {repos_dir}")
    for pid, _ in PROJECTS:
        _git_init(repos_dir / pid)

    print("→ writing lotsa.yaml")
    _write_config(data_dir, repos_dir)

    print("→ seeding tasks")
    db = TaskDB(data_dir / "lotsa.db")

    async def run() -> None:
        await db.initialize()
        try:
            await _seed(db)
        finally:
            await db.close()

    asyncio.run(run())

    print(
        f"\n✓ Demo seeded: {len(FLEET)} tasks across {len(PROJECTS)} projects.\n\n"
        "Next — start the server, then restore the live states its startup sweep blocks:\n\n"
        f"  lotsa serve --data-dir {data_dir} --yes &\n"
        "  sleep 5\n"
        f"  python scripts/seed_demo.py --data-dir {data_dir} --restore\n\n"
        "Then open the dashboard, click a task, and screenshot. All data is fictional."
    )


if __name__ == "__main__":
    main()
