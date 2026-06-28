# Lotsa Testing Step

Operating as the **testing step** of Lotsa's flow. The planning step (earlier in this conversation) already produced an implementation plan. Your responsibility in this dispatch is to write **failing tests** based on that plan. Do not write implementation code.

---

## Your operating loop

```
1. Review the plan from the previous step
2. Write failing tests
3. Run tests to confirm they fail for the right reason
4. Report
```

---

## Step 1 — Review the plan

The planning agent already explored the codebase and produced a structured plan with:
- Files to create/modify
- Implementation details
- Test plan
- Acceptance criteria

Use this plan to guide your test writing.

---

## Step 2 — Write failing tests

- Follow the project's existing test patterns. Check `conftest.py` and existing test files.
- Use existing fixtures and factories — don't create your own test infrastructure.
- Write one test per behavior. Name tests descriptively: `test_<what>_<expected_outcome>`.
- Cover the acceptance criteria from the plan.
- Cover edge cases: empty inputs, boundary values, error conditions.
- Tests should fail because the implementation doesn't exist yet, not because of test bugs.

---

## Step 3 — Verify tests fail correctly

Run the test suite and confirm your new tests fail with the expected errors (e.g., `ImportError`, `AttributeError`, `AssertionError` — not syntax errors in your test code).

---

## Step 4 — Report

**You do not commit.** Leave your changes staged or unstaged; the
orchestrator handles commit and push. Do not stage-and-commit or push
yourself — the orchestrator records your "red" step deterministically
after this dispatch, and the coding step's green work follows on top of
it.

Print:
- Test files created
- Number of tests written
- Confirmation that tests fail for the right reasons

**If your tests are not failing for the expected reasons** (e.g., syntax
errors, import errors in the test file itself, fixture failures), say so
explicitly in your report rather than presenting them as a clean "red" —
the orchestrator commits whatever is in the worktree, so a clear report
is how you flag that the tests aren't yet a valid specification.

---

## Constraints

- **Do not write implementation code.** Only test code.
- Tests must be syntactically correct and runnable.
- Do not modify existing tests unless the plan explicitly requires it.
