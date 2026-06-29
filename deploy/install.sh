#!/usr/bin/env bash
#
# Lotsa single-host deploy — native install behind Caddy (TLS + basic auth),
# run as a systemd daemon. Targets a fresh Ubuntu 22.04/24.04 VPS (Hetzner,
# DigitalOcean, EC2, …); Debian works with a 3.12 python (see README).
#
# Usage (on the box, as root):
#   1. cp deploy.env.example deploy.env && edit it
#   2. ./install.sh
#
# Idempotent: re-run to update (rebuild/replace the wheel locally first, then
# scp it over and re-run — it reinstalls and restarts).

set -euo pipefail

log()  { printf '\n\033[1;36m== %s\033[0m\n' "$*"; }
ok()   { printf '   \033[32mok\033[0m %s\n' "$*"; }
die()  { printf '\033[31mERROR: %s\033[0m\n' "$*" >&2; exit 1; }

[ "$(id -u)" -eq 0 ] || die "run as root (sudo ./install.sh)"

# ── Platform preflight — fail fast, before any mutation ───────────────────────
# The installer targets Debian/Ubuntu (apt) on a systemd host. Detect and refuse
# on anything else rather than half-applying apt/systemctl/ufw commands and
# leaving a confusing partial state. (ADR-042 — `lotsa deploy` surfaces this up
# front; other platforms are designed-for but not yet shipped.)
[ "$(uname -s)" = "Linux" ] || die "lotsa deploy targets a Linux server (Debian/Ubuntu + systemd); detected $(uname -s)."
# shellcheck disable=SC1091
. /etc/os-release 2>/dev/null || true
case " ${ID:-} ${ID_LIKE:-} " in
  *" debian "* | *" ubuntu "*) : ;;
  *) die "unsupported distro '${ID:-unknown}' — the installer targets Debian/Ubuntu (apt + systemd). See the README for the manual path on other systems." ;;
esac
command -v apt-get >/dev/null || die "apt-get not found — the installer requires a Debian/Ubuntu (apt) host."
command -v systemctl >/dev/null || die "systemd (systemctl) not found — the installer manages lotsa as a systemd unit."
ok "platform: ${PRETTY_NAME:-Debian/Ubuntu} (apt + systemd)"

# ── Config ──────────────────────────────────────────────────────────────────
HERE="$(cd "$(dirname "$0")" && pwd)"
if [ -f "$HERE/deploy.env" ]; then
  set -a; . "$HERE/deploy.env"; set +a
fi

: "${LOTSA_DOMAIN:?set LOTSA_DOMAIN in deploy.env}"
: "${LOTSA_BASIC_USER:?set LOTSA_BASIC_USER}"
: "${LOTSA_BASIC_PASS:?set LOTSA_BASIC_PASS}"
: "${PROJECT_ID:=demo}"
: "${LOTSA_MODEL:=sonnet}"
LOTSA_ADMIN_EMAIL="${LOTSA_ADMIN_EMAIL:-}"

if [ -z "${ANTHROPIC_API_KEY:-}" ] && [ -z "${CLAUDE_CODE_OAUTH_TOKEN:-}" ]; then
  die "set ANTHROPIC_API_KEY or CLAUDE_CODE_OAUTH_TOKEN in deploy.env"
fi
# Install source is optional: default to PyPI (`pip install lotsa`); LOTSA_WHEEL /
# LOTSA_GIT override it for dev/source deploys (see the install block below).

LOTSA_USER=lotsa
APP_DIR=/opt/lotsa
DATA_DIR=/var/lib/lotsa
ETC_DIR=/etc/lotsa
VENV="$APP_DIR/venv"

# ── 1. System prerequisites ──────────────────────────────────────────────────
log "Installing system packages"
export DEBIAN_FRONTEND=noninteractive
apt-get update -qq
apt-get install -y -qq ca-certificates curl gnupg git ufw python3 python3-venv python3-pip >/dev/null
ok "base packages"

# Docker — on Linux the agent is isolated via a container, not Claude's native
# OS sandbox (which doesn't reliably start on Linux servers; ADR-038 §Linux).
# The container is the sandbox: the agent can't touch the host.
if ! command -v docker >/dev/null; then
  apt-get install -y -qq docker.io >/dev/null
fi
systemctl enable --now docker >/dev/null 2>&1 || true
ok "docker $(docker --version 2>/dev/null | awk '{print $3}' | tr -d ,)"

# Node 20 (for the claude CLI) via NodeSource, if not already present.
if ! command -v node >/dev/null || [ "$(node -p 'process.versions.node.split(".")[0]' 2>/dev/null || echo 0)" -lt 20 ]; then
  curl -fsSL https://deb.nodesource.com/setup_20.x | bash - >/dev/null
  apt-get install -y -qq nodejs >/dev/null
fi
ok "node $(node -v)"

# Caddy via the official apt repo, if not already present.
if ! command -v caddy >/dev/null; then
  curl -1sLf https://dl.cloudsmith.io/public/caddy/stable/gpg.key | gpg --dearmor -o /usr/share/keyrings/caddy-stable-archive-keyring.gpg
  curl -1sLf https://dl.cloudsmith.io/public/caddy/stable/debian.deb.txt > /etc/apt/sources.list.d/caddy-stable.list
  apt-get update -qq
  apt-get install -y -qq caddy >/dev/null
fi
ok "caddy $(caddy version | head -1)"

# Python 3.12+ (the lotsa package requires it).
PYBIN="$(command -v python3.12 || command -v python3)"
if ! "$PYBIN" -c 'import sys; sys.exit(0 if sys.version_info >= (3,12) else 1)'; then
  die "python >= 3.12 required (have $("$PYBIN" -V)). On Ubuntu: add the deadsnakes PPA and install python3.12-venv; see deploy/README.md."
fi
ok "$("$PYBIN" -V)"

# claude CLI (the agent runner shells out to it).
if ! command -v claude >/dev/null; then
  npm install -g @anthropic-ai/claude-code >/dev/null 2>&1
fi
ok "claude $(claude --version 2>/dev/null || echo '(installed)')"

# ── 2. User + directories ────────────────────────────────────────────────────
log "Creating service user + directories"
id -u "$LOTSA_USER" >/dev/null 2>&1 || useradd --system --create-home --home-dir "$DATA_DIR" --shell /bin/bash "$LOTSA_USER"
install -d -o "$LOTSA_USER" -g "$LOTSA_USER" "$APP_DIR" "$DATA_DIR" "$DATA_DIR/projects"
# Group-owned by lotsa so the service user can traverse it and read lotsa.env
# (640 root:lotsa) — systemd reads it as root regardless, but this also lets
# `sudo -u lotsa lotsa doctor` see the configured credentials.
install -d -m 750 -g "$LOTSA_USER" "$ETC_DIR"
# The service runs `docker run` as the lotsa user → it needs docker socket access.
# (docker group is root-equivalent; acceptable on a single-purpose box.)
usermod -aG docker "$LOTSA_USER"
ok "user $LOTSA_USER (in docker group), dirs $APP_DIR $DATA_DIR $ETC_DIR"

# ── 3. Install Lotsa into a venv ─────────────────────────────────────────────
log "Installing Lotsa"
if [ ! -d "$VENV" ]; then
  "$PYBIN" -m venv "$VENV"
fi
# Install source precedence: an explicit local wheel (dev / `lotsa deploy
# --wheel`), then an explicit git url, then PyPI (the default — what a
# `pip install lotsa` user gets). LOTSA_VERSION pins the PyPI release.
"$VENV/bin/pip" install --quiet --upgrade pip
if [ -n "${LOTSA_WHEEL:-}" ]; then
  [ -f "$LOTSA_WHEEL" ] || die "LOTSA_WHEEL not found: $LOTSA_WHEEL"
  "$VENV/bin/pip" install --quiet --force-reinstall "$LOTSA_WHEEL"
  ok "installed from wheel ($(basename "$LOTSA_WHEEL"))"
elif [ -n "${LOTSA_GIT:-}" ]; then
  "$VENV/bin/pip" install --quiet --force-reinstall "git+$LOTSA_GIT"
  ok "installed from git ($LOTSA_GIT)"
else
  spec="lotsa${LOTSA_VERSION:+==$LOTSA_VERSION}"
  "$VENV/bin/pip" install --quiet --force-reinstall "$spec"
  ok "installed from PyPI ($spec)"
fi
chown -R "$LOTSA_USER:$LOTSA_USER" "$APP_DIR"

# Build the agent container image (the sandbox the agent runs in). Built as root
# so it lands in the shared docker daemon, usable by the lotsa user at runtime.
log "Building the agent image (lotsa-agent:latest) — first build pulls a base image"
( cd /tmp && "$VENV/bin/lotsa" build >/dev/null ) || die "lotsa build failed — check: docker info"
ok "agent image built"

# ── 4. Secrets env file (root-only readable by the service) ───────────────────
log "Writing $ETC_DIR/lotsa.env"
umask 077
{
  echo "# Managed by deploy/install.sh — secrets for the lotsa.service unit."
  [ -n "${ANTHROPIC_API_KEY:-}" ]        && echo "ANTHROPIC_API_KEY=$ANTHROPIC_API_KEY"
  [ -n "${CLAUDE_CODE_OAUTH_TOKEN:-}" ]  && echo "CLAUDE_CODE_OAUTH_TOKEN=$CLAUDE_CODE_OAUTH_TOKEN"
  [ -n "${GITHUB_TOKEN:-}" ]             && echo "GITHUB_TOKEN=$GITHUB_TOKEN"
  echo "LOTSA_ASSUME_YES=1"
} > "$ETC_DIR/lotsa.env"
chown root:"$LOTSA_USER" "$ETC_DIR/lotsa.env"
chmod 640 "$ETC_DIR/lotsa.env"
umask 022
ok "secrets written (640, root:$LOTSA_USER)"

# ── 5. Init data dir + project ───────────────────────────────────────────────
log "Initialising data dir + project '$PROJECT_ID'"
# Run as the lotsa user with ITS home (-H, so `git config --global` writes
# $DATA_DIR/.gitconfig) and from a dir lotsa can read — the script's cwd is
# /root/deploy, which lotsa cannot stat, so git would die with "failed to stat".
cd "$DATA_DIR"
RUNAS=(sudo -u "$LOTSA_USER" -H)

"${RUNAS[@]}" "$VENV/bin/lotsa" init "$DATA_DIR" >/dev/null
"${RUNAS[@]}" git config --global --get user.email >/dev/null 2>&1 || \
  "${RUNAS[@]}" git config --global user.email "lotsa@$LOTSA_DOMAIN"
"${RUNAS[@]}" git config --global --get user.name >/dev/null 2>&1 || \
  "${RUNAS[@]}" git config --global user.name "Lotsa"

PROJ_IDS=()
add_project() {  # <id> [git-url]
  local id="$1" url="${2:-}" dest="$DATA_DIR/projects/$1"
  if [ ! -d "$dest/.git" ]; then
    if [ -n "$url" ]; then
      if [ -n "${GITHUB_TOKEN:-}" ]; then
        # Inline credential helper so the token authenticates the clone without
        # being written into the repo's .git/config (private repos need this).
        "${RUNAS[@]}" git -c "credential.helper=!f(){ echo username=x-access-token; echo password=$GITHUB_TOKEN; };f" clone "$url" "$dest"
      else
        "${RUNAS[@]}" git clone "$url" "$dest"
      fi
    else
      "${RUNAS[@]}" git init -q "$dest"
      "${RUNAS[@]}" bash -c "cd '$dest' && printf '# %s\n' '$id' > README.md && git add README.md && git commit -q -m init"
    fi
  fi
  PROJ_IDS+=("$id")
}

# Project list: PROJECT_REPOS ("id=url id=url …") wins; else single
# PROJECT_REPO/PROJECT_ID; else an empty seeded demo repo so doctor passes.
if [ -n "${PROJECT_REPOS:-}" ]; then
  for pair in $PROJECT_REPOS; do add_project "${pair%%=*}" "${pair#*=}"; done
elif [ -n "${PROJECT_REPO:-}" ]; then
  add_project "$PROJECT_ID" "$PROJECT_REPO"
else
  add_project "$PROJECT_ID" ""
fi

# Reconcile the projects: block every deploy — PROJECT_REPOS is the source of
# truth, so changing it (adding/removing repos) is picked up on the next deploy.
# (The deploy owns this block; manage repos via deploy.env, not by hand-editing.)
YAML="$DATA_DIR/lotsa.yaml"
if grep -q "^projects:" "$YAML"; then
  # Strip the old block (the ``projects:`` key + its indented children), keep the rest.
  tmp="$(mktemp)"
  awk '/^projects:/{skip=1;next} skip&&/^[[:space:]]/{next} skip&&/^[^[:space:]]/{skip=0} {print}' "$YAML" > "$tmp"
  mv "$tmp" "$YAML"
fi
{
  echo ""
  echo "projects:"
  for id in "${PROJ_IDS[@]}"; do
    printf '  %s:\n    name: %s\n    path: %s/projects/%s\n' "$id" "$id" "$DATA_DIR" "$id"
  done
} >> "$YAML"
chown "$LOTSA_USER:$LOTSA_USER" "$YAML"
# Apply the configured model (lotsa init writes a default; honour LOTSA_MODEL).
sed -i "s/^model:.*/model: $LOTSA_MODEL/" "$YAML"
# Docker mode: the agent runs in a container (the isolation boundary on Linux).
grep -q "^docker:" "$YAML" || echo "docker: true" >> "$YAML"
ok "projects: ${PROJ_IDS[*]} (model=$LOTSA_MODEL, docker mode; under $DATA_DIR/projects/)"

# ── 6. systemd daemon ────────────────────────────────────────────────────────
log "Installing systemd service"
install -m 644 "$HERE/lotsa.service" /etc/systemd/system/lotsa.service
systemctl daemon-reload
systemctl enable lotsa >/dev/null
# `enable --now` only *starts* a stopped unit — on an already-running service it
# is a no-op, so a freshly installed wheel or unit file never gets loaded.
# Always restart so every deploy actually picks up the new code.
systemctl restart lotsa
ok "lotsa.service enabled + restarted"

# ── 7. Caddy (TLS + basic auth) ──────────────────────────────────────────────
log "Configuring Caddy for $LOTSA_DOMAIN"
HASH="$(caddy hash-password --plaintext "$LOTSA_BASIC_PASS")"
{
  [ -n "$LOTSA_ADMIN_EMAIL" ] && printf '{\n\temail %s\n}\n\n' "$LOTSA_ADMIN_EMAIL"
  cat <<EOF
$LOTSA_DOMAIN {
	encode zstd gzip
	basic_auth {
		$LOTSA_BASIC_USER $HASH
	}
	reverse_proxy 127.0.0.1:8420 {
		flush_interval -1
	}
}
EOF
} > /etc/caddy/Caddyfile
systemctl reload caddy 2>/dev/null || systemctl restart caddy
ok "Caddy reloaded (auto-HTTPS for $LOTSA_DOMAIN)"

# ── 8. Firewall ──────────────────────────────────────────────────────────────
log "Firewall (ufw)"
ufw allow OpenSSH >/dev/null
ufw allow 80/tcp  >/dev/null
ufw allow 443/tcp >/dev/null
ufw --force enable >/dev/null
ok "ufw: 22/80/443 open, 8420 stays loopback"

# ── 9. Health check ──────────────────────────────────────────────────────────
log "Health check"
sleep 2
systemctl is-active --quiet lotsa || die "lotsa.service is not active — check: journalctl -u lotsa"
systemctl is-active --quiet caddy || die "caddy is not active — check: journalctl -u caddy"
# Source the same secrets the service gets (EnvironmentFile) so the health-check
# doctor reflects the daemon's real auth state, not a credential-less false FATAL.
sudo -u "$LOTSA_USER" -H bash -lc "set -a; . '$ETC_DIR/lotsa.env'; set +a; '$VENV/bin/lotsa' doctor --data-dir '$DATA_DIR'" || true

printf '\n\033[1;32mDone.\033[0m  Dashboard: https://%s  (basic auth user: %s)\n' "$LOTSA_DOMAIN" "$LOTSA_BASIC_USER"
cat <<EOF
  Logs:    journalctl -u lotsa -f   |   journalctl -u caddy -f
  Restart: systemctl restart lotsa
  Update:  re-run ./install.sh (or make deploy)
  Verify:  docker ps   (a lotsa-agent container appears while a task runs)

The agent runs inside a Docker container (ADR-038), isolated from the host.
Still scope GITHUB_TOKEN to the repos you registered.
EOF
