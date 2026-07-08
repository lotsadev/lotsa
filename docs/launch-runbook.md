# Launch runbook

Manual checks to run before tagging/announcing a release. Two parts: an
automated **pre-push gate** (what CI runs) and a hands-on **functional
run-through** that exercises the things tests can't — a real `claude`
invocation, the Docker runner, and the PR loop.

---

## Part A — Pre-push gate (automated)

`scripts/verify.sh` runs the same gate as `.github/workflows/ci.yml`:

```bash
cd ~/code/lotsa-oss
scripts/verify.sh            # ruff check + format-check + pytest   (~2 min)
scripts/verify.sh --build    # + wheel build (confirms the dashboard bundles)
```

Green = the repo passes everything GitHub Actions would. `mypy` is intentionally
not a hard gate (the tree has pre-existing typing gaps / missing third-party
stubs that CI also skips); run `make typecheck` separately for that report.

---

## Part B — Functional run-through (hands-on)

The static checks and the test suite never shell out to a real `claude` or build
a container. This part is human-driven. **Use a throwaway GitHub repo as the
target — never a real repo.**

### 0. Prerequisites
- [ ] Docker daemon running (`docker info` succeeds) — for the Docker leg
- [ ] Agent auth: `ANTHROPIC_API_KEY` exported **or** `claude login` done
- [ ] `GITHUB_TOKEN` scoped to one throwaway repo — for the PR leg
- [ ] A throwaway repo cloned locally (a real git repo with a `main`), e.g. `~/tmp/lotsa-smoke`

### 1. Clean install (proves packaging)
```bash
cd ~/code/lotsa-oss && python -m venv /tmp/lotsa-venv && source /tmp/lotsa-venv/bin/activate
make setup            # pip install -e . + builds the dashboard
```
- [ ] Completes with no error; `lotsa --version` prints a version.

### 2. `lotsa doctor` (the ADR-036 Layer 1 gate)
```bash
lotsa init ~/tmp/lotsa-data
lotsa doctor --data-dir ~/tmp/lotsa-data
```
- [ ] Reports ✔ / ⚠ / ? lines; exits 0 with everything configured.
- [ ] **Negative test:** `env -u GITHUB_TOKEN lotsa serve --data-dir ~/tmp/lotsa-data` piped (non-TTY) → **fails closed** asking for `--yes`.
- [ ] `… --yes` → boots past the missing-`GITHUB_TOKEN` confirm.

### 3. `lotsa serve` + dashboard
Point a project at the throwaway repo (edit `~/tmp/lotsa-data/lotsa.yaml`’s
`projects:` block, or launch from inside the repo), then:
```bash
lotsa serve --data-dir ~/tmp/lotsa-data
```
- [ ] Dashboard loads at `http://127.0.0.1:8420` — no blank page (Layer 0 self-heal).
- [ ] Stop, `rm -rf lotsa/server/static/dist/`, restart → it **auto-rebuilds** and still loads.

### 4. Chat-first task (no git needed)
- [ ] New task → defaults to **chat**; send a message, get a reply, watch the **Activity tab** stream live tool calls.
- [ ] **Hand off** the chat task to `build` or `fix` → it picks up the structured Execute flow.

### 5. Structured flow → PR (needs `GITHUB_TOKEN` + repo)
- [ ] Create a `build`/`fix` task like *“add a CONTRIBUTORS.md with one line”* against the throwaway repo.
- [ ] Watch it implement → **commit** (orchestrator-owned) → **push** → **PR opens** (the orchestrator owns the branch; the agent never branches, and `build`'s plan is ungated — no approval gate).
- [ ] Leave a review comment on the PR → the **PR monitor** picks it up and dispatches a fix round.

### 6. Docker runner
Needs the agent image **and** an env credential — `claude login` keychain auth
does not cross into the container, so export a key first:
```bash
export ANTHROPIC_API_KEY=sk-ant-...           # or CLAUDE_CODE_OAUTH_TOKEN
lotsa build                                   # builds lotsa-agent:latest
lotsa serve --data-dir ~/tmp/lotsa-data --docker
```
- [ ] `lotsa doctor` in docker mode is green (daemon up, image present, env auth set).
- [ ] A task dispatches **inside a container** and completes the same as the native run.

### 7. Security spot-checks (validate the fixes live) 🔒
- [ ] **Sandbox confinement (ADR-038)** — on the deployed box run `sudo ./check-sandbox.sh`: it runs a real sandboxed agent and must report `out-of-worktree write denied` / `SANDBOX OK`. This is the Phase 2 validation that bubblewrap actually confines the agent on Linux.
- [ ] **Credential scrub (§1.2)** — create a task whose body says *“run `env` and print the output”*. After it runs, open the message + Activity tab → **the token shows as `***`, not the real value**.
- [ ] `grep -ri "ghp_\|sk-ant-" ~/tmp/lotsa-data/lotsa.db` → no real tokens in the DB.

### 8. Teardown
```bash
deactivate; rm -rf /tmp/lotsa-venv ~/tmp/lotsa-data
```

---

The **#7 security spot-check** is the highest-value one — it validates the
credential-leak fix in a way no unit test can.
