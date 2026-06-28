#!/usr/bin/env bash
# scripts/dev.sh — install Lotsa in editable mode and start the dev server.
#
# Usage:
#   ./scripts/dev.sh                       # default: lotsa serve (no tasks_dir)
#   ./scripts/dev.sh tasks                 # tasks_dir positional arg
#   ./scripts/dev.sh tasks --port 8500     # forwards extra args to lotsa serve
#
# Args after the script name are forwarded verbatim to `lotsa serve`.

set -euo pipefail

# Resolve the repo root from this script's location so the script works
# regardless of the caller's cwd.
SCRIPT_DIR="$(cd -- "$(dirname -- "${BASH_SOURCE[0]}")" && pwd)"
REPO_ROOT="$(cd -- "$SCRIPT_DIR/.." && pwd)"
cd -- "$REPO_ROOT"

pip install -e .

# exec so signals (Ctrl-C) propagate cleanly to the Python process and the
# existing SIGINT handler inside lotsa serve takes over shutdown.
exec lotsa serve "$@"
