# Pre-Landing PR Review

Canonical Lotsa review workflow. Analyse the current branch's diff against
main for structural issues that tests don't catch.

This file is the source of truth for Lotsa's review behaviour. The Lotsa
CE orchestrator's review step reads this file as its operating workflow.
The cloud-side reviewer (`.claude/skills/review/SKILL.md`, used by the
`/review` slash command and the GitHub Actions reviewer) is a near-duplicate
kept in sync manually; a follow-up will collapse it to a thin pointer
referencing this file as the single source of truth.

Callers MAY adapt Step 5 (Output findings) for their context — interactive
slash command, GitHub PR comment, or orchestrator marker emission. The
workflow steps 0–4 are identical across callers.

---

## Phase 0: Mechanical checks

Run lint and typecheck first to catch mechanical issues before spending time on the diff review:

```bash
make lint 2>&1; make typecheck 2>&1
```

- If lint or typecheck fails, report the failures and ask the user whether to fix them now or continue to the diff review.
- If both pass, proceed silently to Step 1.

---

## Step 1: Check branch

1. Run `git branch --show-current` to get the current branch.
2. If on `main`, output: **"Nothing to review — you're on main or have no changes against main."** and stop.
3. Run `git fetch origin main --quiet && git diff origin/main --stat` to check if there's a diff. If no diff, output the same message and stop.

---

## Step 2: Read the checklist(s)

Read `lotsa/prompts/review/checklist.md` (the generic checklist — this is the canonical CE-owned copy).

**If the file cannot be read, STOP and report the error.** Do not proceed without the generic checklist.

Then check whether the repo carries a project-specific checklist at `.claude/review-checklist.md`. If it exists, read it too and apply it as an additional pass alongside the generic one. If it does not exist, skip silently — the generic checklist is enough.

The repo-level checklist is for invariants that only make sense in this codebase (e.g. "every action method that mutates task state must use `atomic_transition` and check `result.won` before any side effect"). They run as Pass 3 — same severity rules as Pass 2 unless the file says otherwise.

---

## Step 3: Get the diff

Fetch the latest main to avoid false positives from a stale local main:

```bash
git fetch origin main --quiet
```

Run `git diff origin/main` to get the full diff. This includes both committed and uncommitted changes against the latest main.

---

## Step 4: Multi-pass review

Apply the checklist against the diff in passes:

1. **Pass 0 (INTENT):** Does the diff actually accomplish what the PR description / linked issue / referenced plan says it should? Read the PR description first, then the diff — flag any goal that has no corresponding code change, and any code change that has no corresponding stated goal.
2. **Pass 1 (CRITICAL):** Injection & Data Safety, Authentication & Authorization, Credential & Secret Safety, Cryptographic Correctness, Audit Trail Integrity.
3. **Pass 2 (INFORMATIONAL):** API Layer Discipline, Database Conventions, Frontend Patterns, Concurrency & Race Conditions, Performance & Resource Use, LLM Prompt Issues, Test Gaps, SDK Contract, Dead Code & Consistency, Type Safety.
4. **Pass 3 (REPO-SPECIFIC):** items from `.claude/review-checklist.md` if it exists.

**Pattern sweep.** When you find one instance of an anti-pattern, do not stop at that instance. Name the class of bug in one sentence (e.g. "missing `won`-check after CAS", "error message leaks Python conditional", "comment claims behaviour the code no longer has"), then grep for siblings across the diff and the touched files. Report the class with all instances, not the single instance.

**Confidence filter:** For every potential issue, silently rate your confidence it is a real bug (not a style nit, not already handled, not a false positive). Only report issues at 7/10 confidence or above.

**Verification discipline:** Before reporting any finding, read the actual source file (not just the diff) to confirm the issue exists in context. A diff can mislead — the surrounding code may already handle the case you're about to flag.

Follow the output format specified in the checklist. Respect the suppressions — do NOT flag items listed in the "DO NOT flag" section.

---

## Step 5: Output findings

**Always output ALL findings** — both critical and informational. The user must see every issue.

- If CRITICAL issues found: output all findings, then for EACH critical issue use a separate AskUserQuestion with the problem, your recommended fix, and options (A: Fix it now, B: Acknowledge, C: False positive — skip).
  After all critical questions are answered, output a summary of what the user chose for each issue. If the user chose A (fix) on any issue, apply the recommended fixes. If only B/C were chosen, no action needed.
- If only non-critical issues found: output findings. No further action needed.
- If no issues found: output `Pre-Landing Review: No issues found.`

---

## Important Rules

- **Read the FULL diff before commenting.** Do not flag issues already addressed in the diff.
- **Read-only by default.** Only modify files if the user explicitly chooses "Fix it now" on a critical issue. Never commit, push, or create PRs.
- **Be terse.** One line problem, one line fix. No preamble.
- **Only flag real problems.** Skip anything that's fine.

