.PHONY: setup dev build deploy deploy-wheel check-vps frontend test lint format typecheck frontend-install frontend-dev frontend-build

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
# Deploy — build the wheel and ship it + deploy/ to a host (see deploy/README.md)
#   make deploy-wheel VPS=root@your-server
# Excludes deploy/deploy.env so local secrets are never copied.
# ---------------------------------------------------------------------------

# check-vps runs first so a missing VPS= fails before the (slow) build.
# Ships your local deploy/deploy.env too (it's gitignored) so the config travels
# with the wheel — fill it in once locally instead of editing on the box.
deploy-wheel: check-vps build
	@test -f deploy/deploy.env || echo "note: no deploy/deploy.env yet — cp deploy/deploy.env.example deploy/deploy.env, fill it in (chmod 600), then re-run"
	rsync -avz dist/lotsa-*.whl "$(VPS):/root/"
	rsync -avz deploy/ "$(VPS):/root/deploy/"
	@echo ""
	@echo "Copied wheel + deploy/ (incl. deploy.env if present) to $(VPS):/root/"
	@echo "Next: make deploy VPS=$(VPS)   (or: ssh $(VPS) ; cd deploy ; ./install.sh)"

# One-shot: build, ship everything (incl. your local deploy.env), run the installer.
deploy: deploy-wheel
	ssh "$(VPS)" 'cd /root/deploy && ./install.sh'

check-vps:
	@test -n "$(VPS)" || { echo "set VPS=user@host, e.g. make deploy VPS=root@1.2.3.4"; exit 1; }

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
