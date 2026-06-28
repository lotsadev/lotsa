# ADR-037: Web-UI first-run — UI-managed config, secure secrets, GitHub integration, and onboarding

**Status**: Proposed — post-launch flagship (Layer 2 of the FTUE workstream); phased
**Date**: 2026-06-24
**Related**: ADR-036 (CLI preflight — this moves configuration into the UI and softens 036's fatal-on-missing-auth gate into a guided setup), ADR-029 (projects — UI project management replaces hand-edited YAML), ADR-034 (chat-first — the onboarding teaches its loop), CONSTITUTION §1.2 (the secret-handling constraint this must satisfy), the self-hostable dependency rule. Scope: CE.

---

## Context

The north star: the only terminal step is **`pip install lotsa && lotsa serve`** (the build is handled by ADR-036). Everything else — credentials, connecting GitHub, choosing repos, and learning the chat→promote loop — happens in a **guided web first-run**.

Today these live in env vars and `lotsa.yaml`. ADR-036 makes the CLI *fail clearly* when they're missing; this ADR makes them *fixable in the dashboard*.

**Why env vars specifically fall short** (the problem this ADR removes): a shell `export` is **not inherited** by how the app is usually launched — a launchd/systemd service, a Docker container, or a cron start (the classic "works in my terminal, breaks as a service"); they're **undiscoverable** (you learn `GITHUB_TOKEN` exists only when something downstream fails); they're **not editable in place** (changing one means editing a shell rc or relaunching); and a **secret in the environment leaks** into `ps`, every child process (including agent subprocesses), and crash dumps. ADR-036's `doctor`/boot gate makes these gaps *loud and upfront*, but it doesn't remove them.

The hard part isn't the UI — it's **storing secrets without violating CONSTITUTION §1.2** (credentials never appear in plaintext in the database, logs, or API responses). That's the decision this ADR exists to force before any code is written.

## Decision (proposed; phased)

### 1. UI-managed configuration

Settings that live in `lotsa.yaml`/env today become editable in the dashboard and persisted by Lotsa. `lotsa.yaml` stays supported for declarative/advanced use, with a clear precedence (explicit config file > UI-stored > defaults). Non-secret settings (model, budget, default process, …) persist in the existing store.

### 2. Secure secret storage — the central decision

Credentials set from the UI (Anthropic key/OAuth, GitHub token/OAuth) **must not** sit in plaintext in `lotsa.db` (§1.2). Options:

- **(a) OS keychain** (`keyring`: macOS Keychain / libsecret / Windows Credential Manager). Clean at-rest, but typically absent on headless servers / Docker.
- **(b) Encrypted-at-rest** — a dedicated secrets store, encrypted, with the master key from a single `LOTSA_SECRET_KEY` env var (or an OS-keyring-held key). Works headless/Docker; reduces the env-secret surface to one bootstrap key.
- **(c) Env-only** — never persist; the UI shows present/absent + guidance. Trivially compliant, but can't "set keys from the UI" — fails the goal.

**Recommendation:** **(b)** as the portable default — encrypted-at-rest keyed by one `LOTSA_SECRET_KEY` — with **(a)** as a desktop enhancement. Secret values are **write-only over the API** (never returned; the UI shows only set/unset, perhaps a last-4) and never logged.

#### How (b) works — the concrete mechanics

*Encryption.* Use the `cryptography` library's **Fernet** tokens (authenticated AES-128-CBC + HMAC-SHA256; a versioned, hard-to-misuse format) for v1 — AES-256-GCM via `hazmat` is the alternative if a larger key is wanted. The stored token already embeds version + IV + ciphertext + HMAC, so persistence is just a blob.

*Where the ciphertext lives.* A dedicated **`secrets` table** (`name TEXT PK, ciphertext BLOB, key_version INT, updated_at`) — preferably in a **sibling `secrets.db`** rather than `lotsa.db`, so the crypto material has its own file permissions and backup story and never rides along in a task-DB dump. (§1.2 cares about *plaintext* in the DB; ciphertext is fine, but separating the file is cheap defence-in-depth.)

*The root key (`LOTSA_SECRET_KEY`) — "the key for the key".* Every at-rest scheme bottoms out at one bootstrap secret; the win is reducing *N* secrets to *one* with a sane default. Resolve it in priority order:
1. **`LOTSA_SECRET_KEY` env var** if set — for Docker/orchestrated deploys that inject it.
2. else the **OS keychain** (`keyring`) — desktop installs store the generated key there.
3. else **auto-generate** and write `~/.lotsa/secret.key` (mode `0600`) — the headless/zero-config path so first-run "just works" while still being encrypted-at-rest. Losing/rotating this key is the operator's responsibility (documented); lose it and stored secrets must be re-entered.

*Rotation.* The `key_version` column + `MultiFernet` (decrypt-with-old, re-encrypt-with-new) lets the root key rotate without downtime.

*Pluggability.* Define a small `SecretStore` interface (`get`/`set`/`delete`/`list_names`) with the encrypted-DB impl as the CE default; a Vault or pure-keychain backend can be swapped in (Enterprise reuse) without touching call sites — keeping CE's footprint minimal while leaving the door open.

*Dependencies.* `cryptography` (and optionally `keyring`) become new top-level deps when this phase ships — both are self-hostable with no mandatory outbound calls, so they satisfy the self-hostable rule; this ADR is their record.

The exact choice (Fernet vs GCM, single vs sibling DB) is locked when the phase is built; the shape above is the working design.

### 3. GitHub connection & repo/project management

- Connect a GitHub account via OAuth; the token is stored per §2. Configurable host for GitHub Enterprise Server.
- Browse the operator's repos, clone selected ones to a managed location, and register them as **projects** (ADR-029) — replacing hand-edited `projects:` YAML for the common case (YAML stays for advanced/portable setups).
- Project CRUD (add / edit / remove) in the dashboard.
- The self-hostable rule holds: the only outbound calls are to the operator's own configured Git host.

### 4. Boot-then-configure (supersedes ADR-036's fatal-auth gate)

Once there's a UI to fix problems, the server no longer *refuses to boot* on missing auth/projects. It **boots into a guided setup state**: ADR-036's `doctor` checks become the setup wizard's checklist. What was fatal-in-CLI becomes "this step isn't done yet" in the UI — the dashboard won't dispatch agents until auth and a project exist, but it starts so you can provide them. ADR-036's CLI `lotsa doctor` and headless gating remain for automation/non-UI use.

### 5. Onboarding / teach

- A first-run wizard driven by the `doctor` checklist (auth → GitHub/project → first task).
- The chat-first empty-state (ADR-034) teaches the chat→promote loop, with optional starter prompts / a sample task.

## Consequences

### Positive
- The terminal does almost nothing; setup, repos, and learning live in the dashboard — the core FTUE goal ("not a bunch of YAML files"), on the config and projects axes that chat-first didn't cover.
- Secrets get a real, §1.2-compliant home instead of bare env vars.

### Negative
- Large new surface: secret storage, OAuth, repo cloning, project CRUD UI, a setup wizard. Post-launch flagship work; must be phased.
- Boot-then-configure weakens the fail-fast guarantee for headless deploys — mitigated by keeping ADR-036's CLI `doctor`/gating for automation.

### Migration
Additive. `lotsa.yaml`/env keep working; the UI becomes the primary path. Existing operators are unaffected.

## Phases
1. **UI-managed config + secure secret storage** (§1–2) — the foundation; resolves the §1.2 constraint.
2. **GitHub connect + repo/project management** (§3).
3. **Boot-then-configure + onboarding wizard / teach** (§4–5).

## Out of scope
- Multi-user / team auth on the dashboard (it stays loopback-bound by default; see `SECURITY.md`). Multi-operator approval is a separate concern.
- A hosted/managed Lotsa offering.
