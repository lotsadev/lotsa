#!/usr/bin/env bash
#
# Local CI-equivalent gate. Run from the repo root before pushing — there is no
# CI on a fresh clone yet, so this mirrors .github/workflows/ci.yml (ruff +
# pytest) and adds an optional wheel build.
#
#   scripts/verify.sh            # lint + format-check + tests
#   scripts/verify.sh --build    # also build the wheel (needs Node/npm)
#
# Exits non-zero if any gate fails. mypy is intentionally NOT a hard gate: the
# tree has pre-existing typing gaps (missing third-party stubs) that CI also
# skips — run `make typecheck` separately if you want the report.

set -uo pipefail

fail=0
step() { printf '\n\033[1m== %s ==\033[0m\n' "$1"; }
run() { if "$@"; then echo "  ok"; else echo "  FAILED"; fail=1; fi; }

step "ruff check ."
run ruff check .

step "ruff format --check ."
run ruff format --check .

step "pytest (lotsa + rigg)"
run python -m pytest -q lotsa/tests rigg/tests

if [ "${1:-}" = "--build" ]; then
  step "python -m build (wheel bundles the dashboard)"
  run python -m build
fi

echo
if [ "$fail" -eq 0 ]; then
  echo "ALL GREEN"
else
  echo "SOME CHECKS FAILED"
  exit 1
fi
