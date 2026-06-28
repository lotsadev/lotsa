"""FastAPI web dashboard for Lotsa Community Edition.

Serves the React SPA at ``/`` and the JSON API under ``/api/*``. The SPA is
built into ``lotsa/server/static/dist/`` by ``make frontend`` (or auto-built
at startup when the bundle is missing or stale — ADR-036).
"""

from __future__ import annotations

import logging
import os
import subprocess
from contextlib import asynccontextmanager
from pathlib import Path

from fastapi import FastAPI
from fastapi.responses import FileResponse
from fastapi.staticfiles import StaticFiles

from lotsa.config import LotsaConfig
from lotsa.db import TaskDB
from lotsa.orchestrator import OrchestratorService

logger = logging.getLogger(__name__)

_SERVER_DIR = Path(__file__).parent
_STATIC_DIR = _SERVER_DIR / "static"
_FRONTEND_DIR = _SERVER_DIR.parent / "frontend"


_NPM_MISSING_FATAL = (
    "Dashboard bundle missing and `npm` not found on PATH. Install Node.js and run `make frontend` (or `make setup`)."
)
_NPM_MISSING_STALE = (
    "Dashboard bundle is stale and `npm` is unavailable — serving the existing build. "
    "Install Node.js and run `make frontend` to refresh."
)


def _frontend_newest_mtime(frontend_dir: Path) -> float | None:
    """Newest mtime under the frontend source, or ``None`` if there is no source.

    Walks ``frontend_dir`` (skipping ``node_modules`` and dot-dirs). Returns
    ``None`` when no ``package.json`` is present — a packaged install ships only
    the prebuilt bundle, so there is nothing to be "stale" against.
    """
    if not (frontend_dir / "package.json").exists():
        return None
    newest = 0.0
    for root, dirs, files in os.walk(frontend_dir):
        dirs[:] = [d for d in dirs if d != "node_modules" and not d.startswith(".")]
        for name in files:
            try:
                m = os.path.getmtime(os.path.join(root, name))
            except OSError:
                continue
            newest = max(newest, m)
    return newest or None


def _bundle_needs_rebuild(spa_index: Path, frontend_dir: Path) -> bool:
    """True if the dashboard bundle is missing, or stale vs the frontend source.

    A packaged install (no ``frontend/`` source) is never stale — only a
    *missing* bundle counts (ADR-036).
    """
    if not spa_index.exists():
        return True
    newest_src = _frontend_newest_mtime(frontend_dir)
    if newest_src is None:
        return False  # packaged install — trust the shipped bundle
    return newest_src > spa_index.stat().st_mtime


def _ensure_spa_built() -> Path:
    """Ensure the dashboard bundle is present and current; return dist/index.html.

    Builds the SPA when the bundle is missing, and rebuilds it when it is stale
    relative to ``lotsa/frontend`` source (ADR-036). A packaged install ships a
    prebuilt bundle and no source, so it serves as-is. When a rebuild is needed
    but ``npm`` is unavailable, a *present* (stale) bundle is served with a
    warning; only a *missing* bundle is fatal.
    """
    spa_index = _STATIC_DIR / "dist" / "index.html"
    if not _bundle_needs_rebuild(spa_index, _FRONTEND_DIR):
        return spa_index

    bundle_present = spa_index.exists()

    if not (_FRONTEND_DIR / "package.json").exists():
        # Only reachable with a *missing* bundle (a present-but-no-source bundle
        # returns above): a packaged install that somehow lost its dashboard.
        raise RuntimeError(
            f"Dashboard bundle missing and no frontend source at {_FRONTEND_DIR}. "
            "A packaged install should ship the bundle — reinstall, or build from source."
        )

    if not (_FRONTEND_DIR / "node_modules").exists():
        logger.info("Dashboard deps not installed — running `npm install` in %s", _FRONTEND_DIR)
        try:
            subprocess.run(["npm", "install"], cwd=_FRONTEND_DIR, check=True)
        except FileNotFoundError as exc:
            if bundle_present:
                logger.warning(_NPM_MISSING_STALE)
                return spa_index
            raise RuntimeError(_NPM_MISSING_FATAL) from exc

    logger.info(
        "%s the dashboard via `npm run build` in %s", "Rebuilding" if bundle_present else "Building", _FRONTEND_DIR
    )
    try:
        subprocess.run(["npm", "run", "build"], cwd=_FRONTEND_DIR, check=True)
    except FileNotFoundError as exc:
        if bundle_present:
            logger.warning(_NPM_MISSING_STALE)
            return spa_index
        raise RuntimeError(_NPM_MISSING_FATAL) from exc

    if not spa_index.exists():
        raise RuntimeError(f"Dashboard build completed but {spa_index} is still missing — check Vite output.")
    return spa_index


@asynccontextmanager
async def _lifespan(app: FastAPI):
    config: LotsaConfig = app.state.config

    config.data_dir.mkdir(parents=True, exist_ok=True)
    db = TaskDB(config.data_dir / "lotsa.db")
    await db.initialize()

    service = OrchestratorService(config, db)
    await service.start()

    app.state.service = service
    app.state.db = db
    yield

    await service.shutdown()
    await db.close()


def create_app(config: LotsaConfig) -> FastAPI:
    """Create and configure the FastAPI application."""
    # Ensure the SPA is built before the mount guards below evaluate.  Running
    # this inside the (async) lifespan would be too late: ``create_app`` has
    # already returned and the conditional ``app.mount`` calls have already
    # decided whether ``/assets`` and ``/fonts`` exist, so a fresh-checkout
    # startup would serve ``index.html`` with 404s on every JS/CSS asset until
    # restart.  Doing it here is also constitution-compliant (no blocking I/O
    # inside ``async def``).
    _ensure_spa_built()

    app = FastAPI(title="Lotsa Dashboard", lifespan=_lifespan)
    app.state.config = config

    # ── JSON API ─────────────────────────────────────────────────────
    from lotsa.server.api_routes import router as api_router

    app.include_router(api_router)

    # ── React SPA ────────────────────────────────────────────────────
    dist_dir = _STATIC_DIR / "dist"
    spa_index = dist_dir / "index.html"

    @app.get("/", include_in_schema=False)
    async def dashboard():
        return FileResponse(str(spa_index))

    if (dist_dir / "assets").exists():
        app.mount("/assets", StaticFiles(directory=str(dist_dir / "assets")), name="spa-assets")
    if (dist_dir / "fonts").exists():
        app.mount("/fonts", StaticFiles(directory=str(dist_dir / "fonts")), name="spa-fonts")

    favicon_path = dist_dir / "favicon.svg"
    if favicon_path.exists():

        @app.get("/favicon.svg", include_in_schema=False)
        async def favicon():
            return FileResponse(str(favicon_path), media_type="image/svg+xml")

    return app
