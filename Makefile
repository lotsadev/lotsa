.PHONY: setup dev build deploy frontend test lint format typecheck frontend-install frontend-dev frontend-build

# ---------------------------------------------------------------------------
# Setup — run once after cloning
# ---------------------------------------------------------------------------

setup:
	pip install -e .
	$(MAKE) frontend

# ---------------------------------------------------------------------------
# Package — build a wheel/sdist with the dashboard bundled (needs Node)
# ---------------------------------------------------------------------------

build:
	rm -rf dist build
	python -m build

# ---------------------------------------------------------------------------
# Deploy — `lotsa deploy` (ADR-042) is the supported path; see deploy/README.md.
#   Pip users:     pip install lotsa && lotsa deploy --init && lotsa deploy
#   Contributors:  make deploy   (builds a local wheel and ships THAT)
# Config lives in ./deploy.yaml (run `lotsa deploy --init` to scaffold it).
# ---------------------------------------------------------------------------

# Contributor convenience: build the dashboard-bundled wheel and deploy it via
# the CLI's --wheel override (so the box runs your local build, not PyPI).
# Reads ./deploy.yaml for the host + config, same as a pip user.
deploy: build
	lotsa deploy --wheel $$(ls dist/lotsa-*.whl)

# ---------------------------------------------------------------------------
# CLI dev server
# ---------------------------------------------------------------------------

dev:
	@./scripts/dev.sh

# ---------------------------------------------------------------------------
# Testing
# ---------------------------------------------------------------------------

test:
	python -m pytest

# ---------------------------------------------------------------------------
# Code quality
# ---------------------------------------------------------------------------

lint:
	ruff check .
	ruff format --check .

format:
	ruff format .

typecheck:
	mypy lotsa/ rigg/

# ---------------------------------------------------------------------------
# Frontend / dashboard
# ---------------------------------------------------------------------------

# Install deps and build the dashboard bundle in one step.
frontend:
	cd lotsa/frontend && npm install && npm run build

frontend-install:
	cd lotsa/frontend && npm install

frontend-dev:
	cd lotsa/frontend && npm run dev

frontend-build:
	cd lotsa/frontend && npm run build
