# ADR-016: Task artifact persistence & PR-inclusion policy

**Status**: Accepted (revised 2026-06-14 — see Revision note)
**Date**: 2026-05-23
**Related**: ADR-014 (jobs as unified flow primitive — the DB handover channel this adds a parallel sink to), ADR-029 (multi-project — the per-project config the persistence policy lives in), ADR-031 (runtime verification — owns the out-of-tree *evidence* half of the taxonomy below)

---

## Revision note (2026-06-14)

The original ADR framed file persistence as a *visibility* nicety ("so
PR reviewers can see artifacts without opening Lotsa"). Two developments
gave it real strategic weight and sharpened the policy:

1. **Tasks increasingly start in Lotsa, skipping the ADR step.** Lotsa's
   historical flow is ADR → plan → build. As more work originates as a
   Lotsa task with no ADR, the **committed spec becomes the durable
   design record the ADR used to be** — versioned, in-tree, grep-able
   from a checkout. That makes "commit the spec by default" a
   first-class decision, not an optional nicety.

2. **A clean three-home taxonomy emerged** (and ADR-031 needs it). Not
   everything Lotsa produces belongs in git history:

   | Artifact | Home | In git history? | Default |
   |---|---|---|---|
   | code | worktree → commit | yes | — |
   | **spec** (design record) | committed file (this ADR) | **yes** | **`commit: true`** |
   | plan | DB / task context | no | `commit: false` |
   | pr_description | the PR description | no | — |
   | screenshots / logs (ADR-031) | out-of-tree store + PR comment | **no** | (ADR-031) |

   **Design records are committed; evidence is not.** A committed file is
   the right home for text that should live with the code forever (the
   spec). Binary/transient verification evidence (ADR-031's screenshots)
   goes to an out-of-tree standard location and is surfaced into the PR
   as an uploaded comment attachment — visible on the PR, never in the
   repo's history.

Both axes are **configurable per artifact and per project** (ADR-029):
*persist + commit* (in-repo, shows in the PR diff) and *include-in-PR*
(for non-committed artifacts, posted as a PR comment). The defaults
below (spec committed, plan not) are starting points, not fixed policy.

The mechanism the original ADR specified — orchestrator-owned writes,
`output_file:` + `commit:` per job, secrets scan, worktree-escape
rejection — is unchanged and correct. This revision changes the
*motivation and defaults*, not the machinery.

---

## Context

Under ADR-014, the audit/message log is the durable handover channel
between jobs. It works correctly: every job's stdout is persisted to
the task's message history before any downstream rule transition
fires, and resumed subprocesses load recent messages into their
context.

The remaining gap is visibility. The content of agent outputs
(specs, plans, review notes) is only accessible to PR reviewers if
they open Lotsa — it doesn't appear in the PR diff and isn't part
of the repo's git history. Putting these artifacts in the repo
alongside the code they describe would make reviews self-contained
and give the work product a durable home.

Two implementation paths exist:

1. **Agents write files directly to the worktree.** Tried in earlier
   iterations. Agents are unreliable at choosing correct paths,
   formats, and locations. Brittle.
2. **The orchestrator captures agent stdout (as it already does for
   the audit log) and ALSO writes it to a declared file path.** The
   agent doesn't need to know about file layout; one capture path
   feeds two sinks.

Option (2) avoids the agent-reliability problem entirely. That's the
direction this ADR proposes.

---

## Decision

**The orchestrator writes agent outputs to declared file paths in
the worktree as an optional second persistence layer.** The audit/
message log remains the canonical channel for content and handover;
file artifacts are additive — for PR visibility, durable in-tree
storage, and easier review of historical decisions.

### Per-job declarations

Two new optional attributes on a job:

- `output_file: <path>` — destination in the worktree, with
  `{task_id}` and `{name}` placeholders. If unset, the orchestrator
  writes only to the audit log (current behavior).
- `commit: bool` — whether the file is added to the git commit
  that ships with the PR. Default `false`. Has no effect unless
  `output_file:` is also set.

This gives four configurations per job:

| `output_file:` | `commit:` | Effect                                          |
|----------------|-----------|-------------------------------------------------|
| unset          | (n/a)     | DB only — current behavior                      |
| set            | false     | DB + worktree file (gitignored scratch)         |
| set            | true      | DB + worktree file + part of the PR commit      |
| unset          | true      | invalid — parser rejects                        |

Example:

```yaml
jobs:
  - name: spec
    output_file: "docs/tasks/{task_id}/spec.md"
    commit: true             # spec ships with the PR
  - name: plan
    # no output_file — DB only, current behavior
  - name: review
    output_file: "docs/tasks/{task_id}/review.md"
    commit: false            # durable on disk, not committed
```

### Write semantics

When a job emits its terminal output:

1. Orchestrator captures stdout as today (audit/message log write).
2. If `output_file:` is declared, the orchestrator writes the same
   content to the resolved path atomically (write to temp, rename).
3. If `commit: true`, the orchestrator stages the file via
   `git add --force` (bypassing the default `docs/tasks/*/`
   gitignore) as part of the same dispatch's git operations and
   includes it in the next commit (typically push_pr's commit).

The agent is unchanged — it emits stdout. The orchestrator owns all
filesystem and git interactions. This is the property that solves
the agent-reliability problem: there is no "agent writes file"
codepath to get wrong.

### Path convention

The default placeholder pattern is:

```
docs/tasks/<task-id>/<job-name>.md
```

Lotsa interpolates `{task_id}` from `task.metadata.task_id` and
`{name}` from the job's `name:` at runtime. Task-scoped
subdirectories avoid post-merge collisions: tasks A and B both
writing `spec.md` would conflict on `docs/tasks/spec.md`, but
`docs/tasks/<id-a>/spec.md` and `docs/tasks/<id-b>/spec.md` coexist
forever.

Operators can override the default in `output_file:` for repos with
different conventions (e.g. `.lotsa/tasks/{task_id}/{name}.md`).

### Handoff stays in the DB

ADR-014's "Failure-reason handoff" channel — the audit/message log
— is unchanged by this ADR. Downstream jobs continue reading prior
outputs via resume context loaded from the DB. File artifacts are a
parallel sink for human-visible artifacts and PR review, not a
replacement for the handoff carrier.

Rationale: dual persistence is intentional. The DB write is durable
and always present; the file write is opt-in and may be absent.
Downstream jobs that read from the DB work in all configurations;
pointing them at files would require a DB fallback for the "no
`output_file`" case. One read path is simpler than two with
fallback logic, and the DB is the more reliable surface anyway.

### Sensitive content

Three layered protections, all easier to enforce under
orchestrator-owned writes than they would be with agent-owned
writes:

1. **`commit:` is never implicit — it is always a declared choice.**
   The schema attribute defaults to `false` when unset, so `output_file:`
   alone never commits. Committing happens only where a job explicitly
   sets `commit: true` — including the bundled per-job defaults the
   Revision note establishes (`spec: commit: true` as the design record,
   `plan`/`review: false`). The safety property is "no commit without a
   declared `commit: true`, visible in the process YAML," *not* "nothing
   ever commits by default" — the spec deliberately does.
2. **Default `.gitignore` template** (added by `lotsa init`)
   excludes `docs/tasks/*/` entirely. Intentional artifact commits
   go through the orchestrator's `git add --force <path>`, which
   bypasses the ignore. Negation patterns (`!docs/tasks/*/spec.md`)
   are deliberately not used — git skips ignored parent directories
   and silently drops negations on their contents, so per-file
   exceptions wouldn't behave as written. The broad directory
   ignore stays safe against accidental adds via other paths
   (manual `git add`, third-party tooling); intentional artifacts
   come through the orchestrator's force-add path.
3. **Secrets scan on the commit path.** Before `git add`, the
   orchestrator runs a secrets scanner (`gitleaks`-class) on the
   file content. A scanner hit blocks the task with a clear error;
   the operator can mark the job `commit: false` or fix the
   content.

The combination makes "secret accidentally committed via artifact"
multiple-failure rather than single-failure.

### Cleanup of non-committed files

Files written with `commit: false` accumulate on disk over the task
lifecycle. Two viable cleanup points:

- **On task terminal state** (complete, abandoned, blocked-final):
  delete `docs/tasks/<task-id>/` if it contains only uncommitted
  artifacts.
- **Never** — leave the worktree as-is; non-committed files vanish
  when the worktree itself is removed.

This ADR defers the choice to the implementation PR. The path
convention (`docs/tasks/<task-id>/`) supports either policy.

---

## Tradeoffs

**Pros:**

- Orchestrator-owned writes are reliable. Agents emit stdout; the
  orchestrator handles all file and git operations. No more
  "agent wrote to the wrong path" failure mode.
- DB-handoff semantics unchanged. ADR-014's carrier contract stays
  intact; this ADR is purely additive.
- Per-job opt-in keeps the repo clean by default. Operators decide
  which artifacts deserve durable in-tree storage.
- PR reviewers can see spec/plan/review without opening Lotsa
  (for committed artifacts).
- Smallest viable change: two new YAML attributes, one orchestrator
  write path, one gitignore template, optional cleanup hook.

**Cons:**

- Dual persistence means two places to look for the same content
  when debugging. Mitigation: file is a derived artifact of the DB
  write — if they diverge, DB is authoritative.
- Worktree files for tasks that don't commit them accumulate on
  disk. Mitigation: cleanup policy in the implementation PR.
- Operators authoring new jobs must remember to declare
  `output_file:` if they want file persistence. Reasonable: opt-in
  is the safer default and current behavior (DB only) is a working
  baseline.

---

## Scope

This ADR proposes the direction. A subsequent plan and PR will:

1. Add `output_file:` and `commit:` to `Job` / `ResolvedJob`;
   parser updates in `flows.py`. Validate that `commit: true`
   requires `output_file:` set. **`output_file:` is distinct from
   the existing `Job.output` field** (`lotsa/flows.py`, the named
   artifact a step produces for downstream `inputs:` consumption).
   The two are orthogonal: `output:` is a logical artifact label
   used for inter-job handoff bookkeeping; `output_file:` is a
   physical worktree path. A job can declare both (named artifact
   *and* persisted file), either, or neither. They do not replace
   one another.
2. Orchestrator: write the captured agent output to the resolved
   `output_file` path alongside the audit-log write, atomically.
   **Path resolution must reject worktree-escapes** —
   `Path(worktree / output_file).resolve()` must be
   `is_relative_to(worktree.resolve())`. An `output_file:` like
   `../../.env` blocks the task with a clear error rather than
   writing outside the worktree boundary. Operators are trusted
   config authors, so the risk is low; the check is cheap and
   removes a footgun.
3. Orchestrator: `git add --force` the file when `commit: true`
   (bypassing the default `docs/tasks/*/` gitignore); run the
   secrets scan before staging.
4. Gitignore template added to the `lotsa init` flow.
5. Update existing flow YAMLs to declare `output_file:` and
   `commit:` for the jobs that benefit (spec/plan/review). Default
   to sensible commit policies (e.g., spec ships with the PR,
   review does not).
6. Cleanup policy for non-committed artifacts on task terminal
   states (decision left to the implementation PR).

In scope for **Lotsa Community Edition**.

---

## Out of scope

- **Replacing the DB-based handoff.** This ADR does not change
  ADR-014's carrier. Downstream jobs continue to read from the
  audit/message log.
- **Reading from files at agent runtime.** Downstream jobs read
  from the DB. A future ADR could revisit if file-based read paths
  offer concrete benefit.
- **Migrating historical task outputs into files.** Closed beta;
  re-run tasks if needed.
- **Cross-task artifact sharing.** Task B referencing task A's
  spec is a real future case but introduces lookup, versioning,
  and provenance questions worth their own ADR.
- **Binary artifacts.** Scope is text/Markdown design records. Binary
  outputs (screenshots, traces) are *not* committed at all — they are
  verification **evidence**, owned by **ADR-031**: written to an
  out-of-tree standard location and surfaced into the PR as uploaded
  comment attachments, never added to git history. This ADR's committed
  files and ADR-031's out-of-tree evidence are the two halves of the
  taxonomy in the Revision note; neither should leak into the other's
  home (no committed screenshots, no out-of-tree specs).
- **Retention.** Once `docs/tasks/<id>/` is committed, it lives in
  git history forever. A retention policy (archive after N months,
  purge from main after acceptance) is a future ADR if the volume
  warrants.
