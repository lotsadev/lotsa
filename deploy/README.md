# Deploying Lotsa to a single host

A repeatable, one-box deploy: Lotsa runs as a **systemd daemon** bound to
loopback, with **Caddy** in front for **TLS + HTTP basic auth**. Targets a fresh
**Ubuntu 22.04/24.04** VPS (Hetzner, DigitalOcean, EC2, …); generic across
providers — it's just a Linux box with SSH.

> **Not** Docker-wrapped or Kubernetes. Lotsa runs natively because the *agent
> runner itself* uses Docker (`lotsa build` / `--docker`) — wrapping Lotsa in a
> container would mean docker-in-docker. systemd is the simple, correct choice
> for one host. A `compose` option may come later.

## What you get
- `lotsa serve` as `lotsa.service` — auto-restarts, survives reboot + SSH logout (walk away from your laptop).
- Caddy reverse proxy: **automatic HTTPS** (Let's Encrypt) for your domain + **basic auth**.
- The dashboard never listens publicly (127.0.0.1:8420); Caddy is the only open port.
- Dedicated non-root `lotsa` user; secrets in a `640` env file.

## Prerequisites
- A VPS running Ubuntu 22.04/24.04 with root SSH.
- A **domain** with a DNS A-record pointing at the VPS IP (needed *before* install, for the cert).
- Agent credentials: `ANTHROPIC_API_KEY`, or a `CLAUDE_CODE_OAUTH_TOKEN` from `claude setup-token`.
- A `GITHUB_TOKEN` (scope it to a throwaway repo) if you want the push/PR demo.

## Steps (`lotsa deploy` — the supported path, ADR-042)

You don't need a repo checkout. `pip install lotsa` bundles the installer, and
`lotsa deploy` ships it to your box over SSH and runs it.

**1. Scaffold and fill in the config, once** (it holds secrets — keep it out of
git; `--init` writes it `chmod 600`):
```bash
pip install lotsa
lotsa deploy --init             # writes ./deploy.yaml
nano deploy.yaml                # host, domain, basic-auth, API key/token, projects
```

**2. Deploy:**
```bash
lotsa deploy                    # reads ./deploy.yaml, installs from PyPI on the box
```
Add `--dry-run` to print the `ssh`/`scp` commands without running them, or
`--host user@box` to override the target. The box installs Lotsa **from PyPI**
(the same version as your local CLI) — no Node, no local build.

**3. Open `https://your-domain`** and log in with the basic-auth credentials.

> **Contributors** deploying an unreleased local build: `make deploy` builds the
> dashboard-bundled wheel and runs `lotsa deploy --wheel dist/lotsa-*.whl`, so the
> box runs *your* wheel instead of PyPI. It still reads `./deploy.yaml`.

> **Manual fallback** (no CLI): `scp` the `deploy/` directory to the box, write a
> `deploy.env` next to `install.sh` (see `deploy.env.example`), and
> `ssh box 'cd deploy && ./install.sh'`. `lotsa deploy` just automates exactly this.

## Adding your real repos
List them under `projects:` in `deploy.yaml` (each an `id` + `repo`):
```yaml
projects:
  - id: api
    repo: https://github.com/you/api.git
  - id: web
    repo: https://github.com/you/web.git
```
Private repos are cloned with `github_token` (scope it to read — and write, if you
want the PR demo — on those repos). They register as Lotsa projects automatically.

To add or remove repos later, edit `projects:` and re-run `lotsa deploy` — the
installer **reconciles** the `projects:` block on every deploy (it owns that
block, so manage repos there rather than hand-editing `lotsa.yaml`).

> The agent runs inside a Docker container (ADR-038), so it can't touch the host
> outside its task worktree. Still scope the token to the repos you register, and
> prefer a disposable box for anything sensitive.

## Updating
Re-run `lotsa deploy` — it re-ships and re-runs the idempotent installer
(reinstalls the package, restarts the daemon). Pin a release with `version:` in
`deploy.yaml`, or omit it to track the latest CLI's version.

## Operating
```bash
journalctl -u lotsa -f          # Lotsa logs
journalctl -u caddy -f          # proxy / TLS logs
systemctl restart lotsa         # restart
sudo -u lotsa /opt/lotsa/venv/bin/lotsa doctor --data-dir /var/lib/lotsa   # health
docker ps                       # the agent runs in a container (lotsa-agent:latest)
```

On Linux the agent is isolated by **Docker** (ADR-038) — Claude's native OS
sandbox doesn't reliably start on Linux servers, so the installer runs the agent
in a container instead (`docker: true`). `check-sandbox.sh` validates the macOS
native sandbox; on a Linux box, isolation is the container boundary.

## Layout on the box
| Path | Purpose |
|------|---------|
| `/opt/lotsa/venv` | the Lotsa virtualenv |
| `/var/lib/lotsa` | data dir (`lotsa.yaml`, `lotsa.db`, worktrees) + the `lotsa` user's home |
| `/var/lib/lotsa/projects/<id>` | the git repo(s) tasks run against |
| `/etc/lotsa/lotsa.env` | secrets (640, `root:lotsa`) |
| `/etc/systemd/system/lotsa.service` | the daemon |
| `/etc/caddy/Caddyfile` | generated proxy + basic-auth config |

## ⚠️ Security notes
- **The agent runs in a Docker container** (ADR-038): on Linux that's the isolation boundary (Claude's native OS sandbox doesn't reliably start on Linux servers). The installer sets up Docker, builds the agent image, and runs `--docker`; the agent can't touch the host outside its mounted worktree. Still use a **throwaway-scoped `GITHUB_TOKEN`** and a disposable box for sensitive work. (The `lotsa` user is added to the `docker` group, which is root-equivalent — fine for a single-purpose box.)
- Basic auth is a single shared credential — fine for a demo, not multi-user access control.
- Debian (or Ubuntu without python 3.12): install `python3.12` + `python3.12-venv` (e.g. via the deadsnakes PPA) before running; the script requires ≥ 3.12.
