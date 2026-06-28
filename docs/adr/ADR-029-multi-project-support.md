# ADR-029: Multi-project support — tasks carry a project, the server doesn't

**Status**: Implemented
**Date**: 2026-06-11
**Related**: ADR-021 (per-task process dispatch — the precedent for lifting a startup-time singleton to a per-task property), ADR-027 (process promotion — the chat/process-selector UI this must harmonize with), ADR-014 (jobs as unified primitive), CLAUDE.md self-hostable dependency rule.

> ⚠️ **BREAKING CHANGE (pre-release alpha).** Introducing `tasks.project_id` as a `NOT NULL` column is shipped as a clean schema break, **not** a data-preserving migration: the `tasks` table is recreated (and the old flat `~/.lotsa/worktrees/` tree is cleared), so tasks created before multi-project support are dropped. This is acceptable only because Lotsa is pre-release alpha with no install base to protect. The implementation PR and its release notes must repeat this warning prominently. See Decision §2.

---

## Context

Lotsa CE binds "the project" once, at server startup:

```python
# orchestrator.py
self.worktree_manager = WorktreeManager(config.work_dir, config.data_dir / "worktrees")
```

`work_dir` comes from `lotsa.yaml`'s `work_dir:` key (default `.` — CWD-relative), so the operator must launch `lotsa serve` *from inside the target repo*, and every task the server ever creates implicitly belongs to that one repo. Tasks carry no project reference; the SQLite DB at `~/.lotsa/lotsa.db` is already global but its tasks are meaningful only relative to whichever directory the server happened to be started from.

(A cautionary footnote discovered while writing this ADR: the loader silently ignores unknown top-level keys, and real-world configs prove it — the reference operator install's `lotsa.yaml` carries `project_dir:` *and* `process:`, **neither of which is a `LotsaConfig` field** (the code keys are `work_dir` and `flow`). That install only behaves correctly because `work_dir` defaults to `.` and the `--process` CLI flag overrides the flow on every launch. The Phase 1 config change must warn on unknown top-level keys so the `projects:` rollout doesn't repeat this silent-no-op failure mode.)

This was the right simplification while Lotsa was dogfooding against its own repo. It now blocks the next step: using Lotsa on other projects. The operator wants to point Lotsa at any repo or folder on the machine and create a task *in that repo* — without running one server per project.

ADR-021 already lifted one startup singleton (`--process`) to a per-task property, establishing the pattern this ADR follows: **resolve per-task at dispatch time, not per-server at startup.**

### What is already multi-project-clean

- The DB and data dir (`~/.lotsa/`) are machine-global, not repo-local.
- Per-task worktrees live under `data_dir/worktrees/`, not inside the project.
- Process prompts are repo-agnostic by design: conditional tool discovery ("if a `Makefile` exists…", "if `package.json` exists…"), "read the project's CLAUDE.md if it exists". The portability sweep (2026-06-11) found exactly one violation — see Decision §6.
- Git auth is ambient (operator's environment / `gh` CLI), the same mechanism `push_pr` already relies on.

### What is not

- `LotsaConfig.work_dir` — one path, startup-bound, CWD-relative default.
- `WorktreeManager` — constructed once with one `repo_path`.
- Tasks have no `project_id`; the dashboard has no concept of project.
- The chat process has no way to know *which repo* a conversation is about.
- `full/review-system.md` references its SKILL/checklist files by repo-relative path (`lotsa/prompts/review/SKILL.md`) — works only when the task's repo *is* the Lotsa repo.

---

## Decision

### 1. Project is a first-class entity

A **project** is a named, registered repository root:

```yaml
# ~/.lotsa/lotsa.yaml
projects:
  lotsa:
    path: ~/code/lotsa
  otherapp:
    path: ~/code/otherapp
```

**Project `id` is the YAML key, and it is constrained**: `[a-z0-9_-]{1,64}`, validated at startup parse. The id is used verbatim as a filesystem path segment (`worktrees/<project_id>/…`) and as the DB primary key, so unconstrained YAML keys (which may contain `/`, `..`, spaces) would be a silent path-traversal hazard. An invalid id is a hard startup error naming the offending key.

**Path values are normalized at parse time**: `Path(raw).expanduser().resolve()` — `pathlib.Path` does NOT expand `~` on its own, and the example above uses tilde paths deliberately because operators will. Validation (path exists, is a git repository) runs against the normalized path, and only the normalized form is stored/used anywhere downstream.

Phase 1 registration is YAML-only (restart to add). Web-UI registration (`POST /projects`, persisted in the DB) is an explicit follow-up — it is also where a future GitHub-connection flow naturally lives, since by then registration is dynamic. The schema is designed so YAML-seeded and UI-added projects coexist later.

**Source is polymorphic by design, local-path-only by implementation (Phase 1).** A project's `path:` points at an existing local checkout — in Phase 1 the operator supplies and maintains that checkout, so its location is the operator's concern. A future `repo:` source (git URL) normalizes into a Lotsa-managed clone under `data_dir/repos/<project>/` — after which everything downstream is identical, because the orchestrator only ever sees "a local repo path". That is the point of the polymorphic source: once clones are managed, where a repo lives stops mattering to anyone, and nothing downstream of registration changes. Remote sources, and any hosted-version auth story beyond ambient env credentials, are out of scope here (future ADR).

### 2. Tasks carry `project_id`; the server carries nothing

- New `projects` table: `id` (slug), `name`, `path`, `created_at`, `updated_at` (`name` and `path` are mutable — YAML upserts overwrite them at startup — so the table carries `updated_at` per DB conventions). `name:` is an optional YAML field; when omitted it defaults to the project id. Seeded/synced from `lotsa.yaml` at startup (upsert by id; YAML is authoritative for YAML-declared projects in Phase 1).
- **Changing a project's `path` relocates it and invalidates that project's worktrees.** `path` is mutable, but each existing worktree under `worktrees/<project_id>/<task_id>/` carries a `.git` file with an absolute `gitdir:` pointing into the *old* repo's `.git/worktrees/…`; git operations there break (or silently hit the wrong object store) after a move. On detecting a changed `path` at startup, the implementation removes that project's worktree tree and resets its non-terminal tasks to re-create a fresh worktree from their branch on next dispatch — safe because the branch (pushed to its PR) is the source of truth and `WorktreeManager.create` is idempotent and bases off `origin`. A path change while a task is mid-flight with uncommitted worktree state loses that uncommitted state; path changes are an at-rest operator action. (The alternative — rejecting path changes once any task row exists — was considered and rejected as too restrictive: operators legitimately move repos.)
- **Removal policy: startup never deletes project rows.** A project removed from `lotsa.yaml` persists in the DB and its existing tasks remain dispatchable; new-task creation only offers YAML-declared projects. Explicit removal (with its orphaned-task story) is deferred to the web-UI registration follow-up — upsert-only keeps Phase 1's startup sync non-destructive.
- `tasks.project_id` (FK, NOT NULL). **No backfill, no in-place migration — pre-alpha clean break (see the breaking-change banner above).** Rather than backfill existing tasks to a synthesized `default` project and fight SQLite's `ALTER TABLE … ADD COLUMN … NOT NULL` restriction (which would force a table-rebuild dance), the schema ships as a fresh break: pre-multi-project tasks are not carried over. Lotsa is pre-release alpha with no install base to preserve, so the implementation **recreates the `tasks` table** with the new `project_id` column (dropping any pre-multi-project tasks) rather than writing a data-preserving migration — chosen over "require the operator to wipe `~/.lotsa`" so the break is automatic, not a manual operator step. This is the single behavioural break this ADR introduces; everything else is additive. **The append-only `messages` log is cleared in the same migration**: every pre-break message references a now-dropped task, so leaving them would strand orphaned audit rows. This is a deliberate, one-time exception to the `messages`-is-append-only invariant (`lotsa/CLAUDE.md`), and *this ADR is the explicit product decision that authorizes it* — the append-only rule continues to govern application code (`add_message` stays INSERT-only); only this pre-alpha clean-break migration drops rows whose parent task no longer exists.
- `lotsa serve` no longer needs to run from inside a repo. `work_dir:` is still read **going forward** as the seed for a `default` project record (so a single-project `lotsa.yaml` keeps working without a `projects:` block), but it seeds no existing-task history. If both `work_dir:` and `projects:` are present, `projects:` takes precedence and `work_dir:` seeds/updates only the `default` project — unless the operator explicitly declared `projects: default:`, in which case that entry is authoritative and `work_dir:` seeding is a no-op (declared entries always win over derived seeding, regardless of parse order). `work_dir:` carries a deprecation warning and is dropped in a later release. (The stale `project_dir:` key seen in older real-world configs gets an unknown-key warning — it never did anything.)

### 3. Per-project WorktreeManager resolution

The orchestrator holds a `WorktreeManager` **per project**, built lazily and cached:

```python
def _worktree_manager_for(self, project: ProjectRow) -> WorktreeManager:
    if project.id not in self._worktree_managers:
        self._worktree_managers[project.id] = WorktreeManager(
            Path(project.path), self.config.data_dir / "worktrees" / project.id
        )
    return self._worktree_managers[project.id]
```

Worktrees move from `data_dir/worktrees/<task_id>` to `data_dir/worktrees/<project_id>/<task_id>`. The existing `WorktreeManager` class needs no changes — it already takes `repo_path` as a constructor argument; we just stop constructing exactly one. (Same shape as ADR-021's `_process_for(row)`.)

Every site that today reaches for `self.worktree_manager` resolves through the task's project instead. Validation at task creation: project must exist and `path` must be a git repository — fail fast at create, not at first dispatch.

### 4. Config layering gains a project scope — later

Phase 1 keeps `model` / `budget` / `process` global. The resolution order is documented now so the later addition is non-breaking:

```
built-in defaults ← lotsa.yaml globals ← [project overrides — future] ← task-level choice
```

### 5. UI: project picker, harmonized with the process selector

Task creation gains a **project picker** alongside the (in-flight, ADR-027-adjacent) **process selector**. These are the same decision surface: "start with chat in repo X" and "full process in repo Y from the start" are both (project, process) pairs chosen at task creation. The new-task affordance therefore takes both:

- Project: dropdown of registered projects (default: most recently used).
- Process: the existing selector work; chat-first vs full-first per ADR-027's triage model.

Task list gets a project badge and a project filter. The chat process receives the project's name and root path in its dispatch context so spec-stage exploration happens against the right repo.

### 6. Prompt portability fix (the one violation)

`full/review-system.md` must stop addressing its workflow files repo-relatively. The orchestrator injects the installed prompts directory as a template variable (e.g. `{lotsa_prompts_dir}`), and the prompt references `{lotsa_prompts_dir}/review/SKILL.md`. This is a prerequisite, shippable independently — without it, review breaks on every non-Lotsa repo regardless of the rest of this ADR.

---

## Consequences

**Positive**

- One server, any number of repos; `lotsa serve` from anywhere.
- The local-clone normalization keeps the future remote-repo feature additive: `repo:` source + clone/fetch step, zero orchestrator changes.
- No migration to write or get wrong: the pre-alpha clean break (§2) trades a one-time DB reset for a far smaller, lower-risk schema change.
- Worktree namespacing by project prevents task-id collisions ever mattering across repos.

**Negative / risks**

- Every `self.worktree_manager` call site is a touch point — mechanical but broad; the kind of sweep where one missed site means a task's worktree lands in the wrong repo. Mitigation: delete the attribute entirely so stragglers fail loudly at startup, not silently at dispatch.
- Worktree namespacing (`worktrees/<task>` → `worktrees/<project>/<task>`) needs no relocation logic given the clean break: pre-existing tasks aren't carried over (§2), so there are no in-flight worktrees at the old path to migrate. New tasks are created at the namespaced path from the start. The Schema PR must, however, **delete the old flat `data_dir/worktrees/` tree** as part of the break — the tasks-table recreate drops the DB rows but not the on-disk directories, which would otherwise linger as git-registered orphans. (Covered by the breaking-change warning so operators clearing the DB also clear worktrees.)
- The chat process gains repo context but CLAUDE.md-reading agents now genuinely depend on each project carrying its own conventions file — a repo without one gets generic behavior (acceptable; already true today).

**Out of scope (recorded so they're deliberate)**

- Remote `repo:` sources and managed clones (future step; design accommodated).
- GitHub-connection / hosted auth beyond ambient env credentials (`gh` CLI, `GIT_*` / token env vars — same mechanism `push_pr` uses today).
- Web-UI project registration (follow-up; schema accommodates).
- Per-project config overrides (follow-up; resolution order reserved).
- Per-project process catalogs — explicitly rejected: processes are repo-agnostic by design, one catalog serves all projects.

---

## Implementation sketch

1. **Prereq PR** — fix `review-system.md` repo-relative paths via `{lotsa_prompts_dir}` injection. Independently valuable.
2. **Schema PR** — `projects` table + `tasks.project_id` (NOT NULL) as a pre-alpha clean break: **recreate the `tasks` table** with the new column (drops pre-multi-project tasks) and delete the old flat `data_dir/worktrees/` tree, no backfill (§2). Release notes carry the breaking-change warning.
3. **Core PR** — config `projects:` parsing (id slug validation, path `expanduser().resolve()` normalization + exists/is-git checks, `default`-entry precedence over `work_dir:` seeding, unknown-top-level-key warning); per-project WorktreeManager resolution; remove the singleton attribute; task-creation validation; worktree path namespacing.
4. **UI PR** — project picker on new-task (alongside process selector), project badge + filter on task list, project context into chat dispatch.
