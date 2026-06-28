# ADR-034: Chat-first task creation — load the full process catalog, default to chat

**Status**: Implemented (2026-06-22)
**Date**: 2026-06-22
**Related**: ADR-027 (process promotion — the chat→structured journey this makes reachable), ADR-021 (per-task process dispatch — whose "load only the active preset" rule this lifts), ADR-029 (multi-project — the picker surface this extends). Scope: CE.

---

## Context

ADR-027 shipped the pieces of a chat-first workflow: a bundled `chat`
process (one conversational REPL step, no completion marker, no commit
pressure) and `promote_task` to grow a task from chat into a structured
process (`full`/`quickfix`/…) without losing its worktree or history. Its
stated vision: *"the operator should not have to choose between exploratory
chat and structured execution at task creation — they should start one and
grow into the other."*

That journey is unreachable today, because ADR-021's catalog loader only
loads **one bundled preset** — the active one named by `--flow`/`--process`
— plus any inline `processes:` from `lotsa.yaml`. The rationale at the time
(`orchestrator.py`): *"Non-active bundled presets are NOT loaded — they
would cost a full process load just to surface in the dropdown, with no
callsite that would dispatch against them."* ADR-027 then added exactly such
a callsite (promotion), but the loader was never revisited.

The result, observed in practice (operator running `lotsa serve --flow
full`):

- Only `full` is in the catalog. `chat` isn't loaded, so no task can start
  as a conversation.
- `GET /api/processes` returns one process, so the new-task **ProcessPicker
  hides itself** (it renders nothing at ≤1 process) — same for the project
  picker at one project. The operator sees no choice and every task goes to
  `full`.
- Switching to `--flow chat` doesn't help: then only `chat` loads, and
  `promote_task` rejects promoting to `full` because the destination must be
  in the catalog (`to_process not in self._processes`). The canonical
  **chat → promote-to-full** journey needs *both* loaded, which no flag or
  supported config achieves for bundled presets.

There is also no CLI task-submission command (`lotsa` exposes only `init`,
`build`, `promote`, `serve`, `inspect`), so `--flow`'s "which process do new
tasks use" role is exercised solely by the dashboard.

The pieces exist; they were never wired to coexist. This ADR makes the
chat-first journey the default and removes the loader rule that blocks it.

## Decision

### 1. Load the full bundled catalog at startup

Drop the "non-active bundled presets are not loaded" rule. At `start()` the
catalog (`_processes`) loads **every** bundled preset in `PRESET_NAMES`
(`simple`, `standard`, `full`, `chat`, `quickfix`) **plus** every inline
`processes:` entry. Each loads under its operator-facing name (the preset
name for bundled, the YAML key for inline), exactly as the active preset
does today.

The original cost concern is negligible: a preset load is a handful of
prompt-file reads, done once at startup. The benefit — every process is a
valid promotion target and a pickable new-task option — is the whole point
of ADR-027.

`--flow-file` and inline-name collisions keep their existing precedence
(`_select_active_process_name`): an explicit file or an inline entry named
the same as a preset still wins for the *active* slot. Loading the bundled
catalog never overwrites an inline entry that shares a name — inline wins,
the bundled preset of that name is skipped.

### 2. New-task default is `chat`

The default process applied when a task is created without an explicit pick
becomes `chat`. New tasks open as a conversation the operator grows from;
the agent's chat triage suggests the matching process (ADR-027 §3) and the
operator promotes when ready.

`LotsaConfig.flow` defaults to `"chat"` (was `"full"`). The zero-config
path (`lotsa serve` with no `--flow`) is therefore chat-first.

### 3. The picker always shows, lists all, pre-selects the active process

With the full catalog loaded, `GET /api/processes` returns ≥5 entries, so
the ProcessPicker always renders. It lists every loaded process; the
operator can **pick a structured process (e.g. `full`) directly** when they
already know they're building, skipping chat entirely. The picker's
"Default" option maps to the active process (§4), which is `chat` out of the
box.

### 4. `--flow` / `--process` selects the *default-selected* process, not what loads

`--flow`/`--process` no longer gates **what** loads (the whole catalog
always loads). It now only sets **which process the picker pre-selects** —
i.e. the active/default process for new-task creation. Its default is `chat`
(§2).

- `lotsa serve` → chat is the default selection (chat-first).
- `lotsa serve --flow full` → `full` is pre-selected for an operator who
  always builds; chat and the others remain one click away.

This keeps the flag meaningful and non-destructive rather than making it a
dead no-op. An operator currently passing `--flow full` to *get* full simply
drops the flag to get chat-first, or keeps it to keep full as their default.

The reserved future home for a per-task process choice on a non-dashboard
entry point is a `--process` option on a future `lotsa task`-style submit
command (see Out of scope); this ADR does not add one.

### 5. Promotion is unchanged but now usable

`promote_task` already requires the destination to be loaded and already
rejects promoting *into* `chat` (ADR-027 §7). With the full catalog loaded,
its destination set is every non-`chat` process — so chat → `full` /
`quickfix` / `standard` / `simple` / any inline process all work. No change
to the promotion mechanics themselves.

## Consequences

### Positive

- The ADR-027 chat-first journey works out of the box: start in chat, grow
  into the right process, one task with one history.
- Better FTUE: a fresh `lotsa serve` opens to a conversation rather than
  committing the operator to `full`'s spec→plan→… pipeline before they've
  decided what they're building. Directly addresses the "it shouldn't be a
  bunch of yaml files" onboarding goal.
- The picker is populated and useful for both projects (ADR-029) and
  processes; "pick full directly" is a first-class path.
- Every loaded process is a valid promotion target — no more "Unknown
  process: full" when promoting.

### Negative

- Marginally higher startup cost (loads ~5 presets instead of 1). Measured
  in prompt-file reads at boot; negligible.
- Behaviour change for operators relying on `--flow X` to *restrict* the
  catalog to one process — that restriction is gone; all processes are now
  pickable. There is no use case in CE that depended on hiding processes.
- The new-task default flips from `full` to `chat`. An operator who wants
  full by default now passes `--flow full` (previously the bare default).

### Migration

Pre-alpha, no install base to preserve. No data migration. Operators who
want the old "default to full" behaviour pass `--flow full`; everyone else
gets chat-first by dropping the flag. README + the `lotsa init` scaffold
document the new default.

## Out of scope

- **CLI task submission.** A `lotsa task "<message>" [--process X] [--project
  Y]` command is a natural follow-up (it's where a per-task `--process`
  belongs), but is not part of this ADR.
- **Per-project default process** (ADR-029 interaction). A project could
  carry its own default process so some repos are chat-first and others go
  straight to full; deferred until there's demand.
- **Curating the picker list.** This ADR loads all five bundled presets. If
  the dropdown proves noisy, a config to hide processes is a later,
  additive change.
