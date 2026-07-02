# ADR-042: `lotsa deploy` — a CLI command for single-host deployment (no repo checkout)

**Status**: Implemented — Phases 1–4 shipped (`feature/lotsa-deploy-cli`). Phase 5 (additional platforms) deferred post-launch.
**Date**: 2026-06-29
**Related**: ADR-036 (first-run reliability — the wheel-bundles-dashboard packaging this extends to also bundle the deploy assets), ADR-038 (host-sandboxed execution — the Docker-on-Linux the installer sets up), the `deploy/` scripts + the Makefile `deploy` target (what this lifts into the CLI). Scope: CE.

---

## Context

Lotsa now installs via **`pip install lotsa`** (the wheel bundles the dashboard +
`rigg`). But *deploying* to a server is driven by **`make deploy`**, which only
works from a **source checkout** — the Makefile and the `deploy/` scripts aren't
in the wheel. So a pip user — the *primary* install path — can't deploy to a
server without cloning the repo. That breaks the "pip install and self-host"
story. `make deploy` also **builds a wheel locally and scp's it**, which a pip
user has no source to do.

Separately, the installer (`deploy/install.sh`) does **zero environment
detection** and hard-assumes **Debian/Ubuntu + systemd + ufw + apt**. On anything
else it fails with a cryptic `apt-get: command not found`, and `set -e` can leave
a half-applied partial state.

## Decision

Add **`lotsa deploy`** as a first-class operator CLI command, and bundle the
deploy assets into the package.

1. **Bundle deploy assets as package data.** `install.sh`, `lotsa.service`, the
   Caddyfile template, and `check-sandbox.sh` ship *inside* the wheel (hatch
   package-data), so they're present after `pip install lotsa`.
2. **Install on the server from PyPI by default.** The bundled installer's source
   precedence is: explicit local wheel (dev) → explicit git url → **PyPI**
   (default — `pip install lotsa[==VERSION]`). `lotsa deploy` deploys the *same
   version that's running it*.
3. **`lotsa deploy` orchestrates** with plain `ssh`/`scp` (no new dependency):
   read `deploy.yaml`, ssh to the host, push the bundled assets, run the
   installer. A **`--wheel <path>`** flag deploys a local build (the contributor
   path — the Makefile `deploy` target becomes a thin alias).
4. **`deploy.yaml` config.** A declarative `deploy.yaml` (domain, basic-auth,
   agent credential, GitHub token, projects, model) replaces hand-editing
   `deploy.env`. The CLI parses it and feeds `install.sh` the same env vars it
   reads today, so **the installer's contract is unchanged**. `lotsa deploy
   --init` scaffolds a commented `deploy.yaml`; flags override individual fields.
5. **Platform honesty + fail-fast.** The installer runs a **platform preflight**
   first (Linux + Debian/Ubuntu via `/etc/os-release` + `apt-get` + `systemctl`);
   on an unsupported host it exits cleanly with a clear message **before mutating
   anything**. `lotsa deploy --help` and the docs state the supported target
   (Debian/Ubuntu LTS + systemd). Other platforms are *designed-for* (the
   preflight is the seam to add them) but **not shipped in v1**.

### Scope boundary — what does NOT move into the CLI

The **dev/build** make targets (`setup`, `frontend`, `lint`, `typecheck`,
`build`) operate on the *source* and are only ever run by a contributor who has
the repo. They stay in the Makefile — moving them into the `lotsa` CLI would be
dead weight for pip users. Only **operator** commands move; `init` / `serve` /
`doctor` / `build` (Docker image) are already CLI commands. `deploy` is the gap.

## Consequences

**Positive**
- `pip install lotsa && lotsa deploy` is a complete self-host path with **no repo
  clone** — it matches the product story.
- Deploying from PyPI means the server runs a **released, reproducible** version.
- Unsupported platforms **fail fast and clearly** instead of half-installing.

**Negative / risks**
- The wheel grows by a few KB (the deploy scripts — negligible).
- The deploy contract now lives in the package; the `deploy.yaml` schema is a new
  surface to version.
- Still **Debian/Ubuntu + systemd only** — the preflight makes that explicit, but
  it's a real limit for other operators (mitigated by the documented manual path
  and the extension seam).
- SSH orchestration from Python is new surface; kept to subprocess `ssh`/`scp`
  (no new library) to stay self-hostable-rule clean.

## Alternatives

- **Keep deploy source-only**, document "clone the repo to deploy." Rejected —
  defeats the pip-install story for the primary install path.
- **A separate `lotsa-deploy` package.** Rejected — more moving parts; the assets
  are tiny and belong with the thing they deploy.
- **Full multi-distro support now.** Deferred — ship the tested target
  (Debian/Ubuntu) with a fail-fast preflight; add distros behind the same seam.

## Phasing

1. **Platform preflight** in `install.sh` + **PyPI-default** install source.
2. **Bundle deploy assets** as package data.
3. **`lotsa deploy` CLI** + `deploy.yaml` (+ `--init` scaffold, `--wheel`
   override). Makefile `deploy` → thin alias.
4. **Docs**: README self-host section uses `lotsa deploy`.
5. *(Later)* additional platforms behind the installer's preflight seam.

## Out of scope

- Multi-host / orchestrated (k8s) deploys — single-host only (the ADR-038 model).
- Managed/hosted Lotsa — a separate product.
