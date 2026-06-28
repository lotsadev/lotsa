# ADR-038: Host-sandboxed agent execution — native runs drop the permission bypass

**Status**: Accepted — pre-launch; phased. **Phases 0–2 implemented.** Phase 0 spike + Phase 1 native runner shipped. **Phase 2 field-validation finding: Claude's native OS sandbox is reliable on macOS (Seatbelt) but does NOT reliably start on Linux servers** — its sandbox "HTTP bridge" fails to initialize on a stock Ubuntu 24.04 box even with bubblewrap working, userns unrestricted, no AppArmor/seccomp denials. So **on Linux, isolation is Docker mode** (the container is the sandbox); the deploy installs Docker, builds the agent image, and runs `--docker`. Native sandbox = macOS/desktop. Phase 3 (SDK-runner parity) pending.
**Date**: 2026-06-25
**Related**: `SECURITY.md` + `CONSTITUTION.md` (the host-safety thesis this realizes), ADR-028 (runner shapes — CLI / Docker / SDK), ADR-013 (orchestrator-owned git — the complementary containment), ADR-036 (the `doctor`/serve preflight this extends). Scope: CE (shared `rigg` runner + `lotsa` preflight).

---

## Context

Host safety has been the thesis from the start: **Claude Code should not be able to damage the machine it runs on.** This ADR is not a new idea — it is a **correction of the security *implementation*** after investigating what Claude Code can actually enforce.

The initial implementation ran the agent with `--dangerously-skip-permissions` (and the SDK's `permission_mode="bypassPermissions"`) in **every** mode, native included. It leaned on *convention* for safety — the per-task git worktree as the agent's cwd, orchestrator-owned git (ADR-013), and the operational preamble — rather than an enforced boundary. That was an expedient: it made `--allowedTools` a no-op (so an allowlist gave false assurance), and in native mode a single `Bash` call can still `rm -rf` outside the worktree, read `~/.ssh`, or exfiltrate. Docker mode added real isolation, but that made the host-safety guarantee *conditional on opting into containers* — the opposite of a thesis that should hold by default.

Investigating Claude Code (v2.1.191) showed it ships a built-in **OS-level sandbox** (macOS Seatbelt; Linux bubblewrap + socat) that confines filesystem writes to the working directory, OS-enforced for the agent *and every child process it spawns*. That makes the original intent achievable natively, without Docker. So we change the implementation accordingly.

## Decision

### 1. Native runner: two-layer confinement, bypass off
The CLI runner stops passing `--dangerously-skip-permissions`. The Phase 0 spike proved confinement needs **two layers**, because the OS sandbox confines subprocesses but **not** Claude's in-process file tools (`Write`/`Edit`/`MultiEdit`). Per dispatch the runner injects a managed settings file (`--settings`) combining:
- **OS sandbox** — `sandbox.enabled=true`, `failIfUnavailable=true`, `allowUnsandboxedCommands=false`, `filesystem.allowWrite=[<worktree>]`. OS-enforced confinement of **Bash and all subprocesses** (the `rm -rf`/arbitrary-command class). *Validated: a bash write outside the worktree fails `operation not permitted`.*
- **Worktree-scoped permission rules** — `--permission-mode dontAsk` + `permissions.allow = ["Bash", "Read", "Write(//<worktree>/**)", "Edit(//<worktree>/**)", "MultiEdit(//<worktree>/**)"]`. Confines the **file tools** (which bypass the OS sandbox); `dontAsk` auto-denies anything unmatched, so there is no headless hang. *Validated: a `Write` outside the worktree is denied; inside it succeeds.*
- keeps `--setting-sources project` (project `CLAUDE.md` / `.claude` as context); the managed `--settings` layer carries this policy. The worktree path is interpolated into both `allowWrite` and the `Write/Edit` rules per task.

**Filesystem confinement only** in v1 — network is **not** confined (so `npm install` / `pip` / `git fetch` keep working) and **reads are not confined** (the thesis is host *damage* = writes). Network allowlisting and read-side `deny` rules for `~/.ssh` / `~/.aws` are fast-follows (see Out of scope).

### 2. Sandbox unavailable → fail, unless explicitly overridden per-launch
If the OS sandbox can't be established (Linux without bubblewrap/socat, native Windows), `lotsa serve` **refuses to dispatch the agent** and prints exactly how to proceed: install the sandbox prerequisites, run under `--docker`, **or** pass the explicit opt-out.

The opt-out is a **per-launch CLI flag** — `lotsa serve --dangerously-skip-permissions` (the name is borrowed deliberately so the risk is unmistakable). It is **never** a persisted `lotsa.yaml` value: running without host protection must be a conscious choice on **every** launch, the message states it is **not recommended**, and `lotsa doctor` reports sandbox availability as a check (FATAL in native mode unless the flag is set). Fail-loud-every-time beats a one-time acknowledgement that's forgotten.

### 3. Docker / VM mode — the isolation path on Linux
Container modes keep `--dangerously-skip-permissions` inside the container — the container *is* the sandbox, so the in-container OS sandbox is redundant. Per the Phase 2 finding, **Docker is the recommended isolation on Linux servers** (where Claude's native sandbox doesn't start); the single-host deploy installs Docker and runs `--docker` by default on Linux.

### 4. SDK runner parity
The SDK runner's `bypassPermissions` falls under the same rule: off-container it must adopt the sandboxed/`dontAsk` posture or sit behind the same explicit override. The SDK sandbox path may lag the CLI (phasing).

## Consequences

### Positive
- The host-safety thesis becomes an **OS-enforced guarantee in the default native mode** — no container required.
- Running without host protection is impossible *by accident*: it takes a clearly-not-recommended, per-launch flag.
- `--allowedTools`/policy regain meaning (they no longer sit behind a blanket bypass).

### Negative / risks
- **OS-specific.** macOS is turnkey (Seatbelt); Linux needs `bubblewrap` + `socat` (new prerequisite → `doctor` check); native Windows is unsupported (WSL2 only).
- **Headless permission model.** `dontAsk` auto-denies unapproved tools — actions that "just worked" under blanket bypass may now be denied. We likely need a curated `permissions.allow` allowlist and/or a `PreToolUse` policy hook to keep common operations (git, test runners, package installs) flowing without hangs. **Must be settled in the spike.**
- **Model requirement** only if we later adopt `auto` mode (Opus/Sonnet 4.6+); `dontAsk` has no such requirement.
- **Settings-layer interaction.** The managed `--settings` must compose with `--setting-sources project` such that project/operator settings cannot silently *disable* the sandbox.
- Minor per-dispatch overhead writing the managed settings file.

### Migration
A change to the **default** native behavior. On supported OSes, existing native operators gain host protection automatically. On unsupported ones they must install prerequisites, use Docker, or pass the override. `SECURITY.md` is updated to describe the enforced boundary, replacing the "worktree convention" language with "OS-sandboxed by default; bypass only via explicit per-launch flag or container".

## Phases
0. **Spike — DONE** (macOS 15.6.1, claude 2.1.191, haiku). Findings:
   - The recipe in §1 works: file tools confined by worktree-scoped `dontAsk` allow-rules, bash + subprocesses confined by the OS sandbox; in-worktree work succeeds, out-of-worktree writes are blocked via *both* paths; `claude` exits 0 with no hang.
   - **Key gotcha:** the OS sandbox does NOT cover `Write`/`Edit` — relying on the sandbox alone lets a `Write` escape the worktree. Permission-rule scoping of the file tools is mandatory, not optional.
   - Bare `Write`/`Edit` in `permissions.allow` does not grant under `dontAsk` — the rules must be **path-scoped** (`Write(//abs/**)`).
   - Claude treats paths under `~/.claude/**` as "sensitive" and blocks writes there regardless of sandbox — Lotsa worktrees must stay outside `~/.claude` (they do: `~/.lotsa`/project path).
   - Still TODO in a later phase: validate on Linux (bubblewrap+socat); confirm the `.git`-pointer worktree case (agent git writes land outside the worktree → blocked, but commits are orchestrator-owned so this is expected).
1. Native runner: managed-settings injection + `dontAsk`, drop the bypass; preflight sandbox-availability check + the per-launch override flag; `doctor` reporting.
2. Linux prerequisite handling (`doctor`: bubblewrap/socat) + docs (README / SECURITY / runbook).
3. SDK-runner parity.

## Out of scope (for now)
- Network confinement / domain allowlist (filesystem-only in v1).
- A general `PreToolUse` policy engine beyond what's needed to prevent headless hangs.
- Native-Windows sandboxing.
