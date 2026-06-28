"""Smoke tests for SPA serving — ``GET /`` returns the built React index."""

from __future__ import annotations

import os
from pathlib import Path

import pytest
from httpx import ASGITransport, AsyncClient

from lotsa.server import app as appmod


def _touch(p: Path, mtime: float | None = None) -> Path:
    p.parent.mkdir(parents=True, exist_ok=True)
    p.write_text("x")
    if mtime is not None:
        os.utime(p, (mtime, mtime))
    return p


class TestSPARoutes:
    def test_root_serves_spa_index(self, app_with_service, run):
        """GET / returns 200 with the SPA's index.html body."""
        app, _ = app_with_service

        async def _test():
            transport = ASGITransport(app=app)
            async with AsyncClient(transport=transport, base_url="http://test") as client:
                resp = await client.get("/")
                assert resp.status_code == 200
                # index.html ships a root mount point and the Vite-bundled script.
                assert '<div id="root">' in resp.text
                assert "/assets/" in resp.text

        run(_test())

    def test_api_router_still_mounted(self, app_with_service, run):
        """The SPA-only refactor must not have unmounted /api/*."""
        app, _ = app_with_service

        async def _test():
            transport = ASGITransport(app=app)
            async with AsyncClient(transport=transport, base_url="http://test") as client:
                resp = await client.get("/api/tasks")
                assert resp.status_code == 200
                assert isinstance(resp.json(), list)

        run(_test())


# ADR-036 — dashboard bundle freshness (auto-build / serve-stale / fatal-missing)


class TestBundleStaleness:
    def test_missing_bundle_needs_rebuild(self, tmp_path):
        frontend = tmp_path / "frontend"
        _touch(frontend / "package.json")
        assert appmod._bundle_needs_rebuild(tmp_path / "static/dist/index.html", frontend) is True

    def test_packaged_install_with_no_source_is_never_stale(self, tmp_path):
        # Bundle present, no frontend/ source → trust the shipped wheel bundle.
        spa = _touch(tmp_path / "static/dist/index.html")
        assert appmod._bundle_needs_rebuild(spa, tmp_path / "frontend") is False

    def test_fresh_bundle_is_not_stale(self, tmp_path):
        frontend = tmp_path / "frontend"
        _touch(frontend / "package.json", mtime=1000)
        _touch(frontend / "src/main.tsx", mtime=1000)
        spa = _touch(tmp_path / "static/dist/index.html", mtime=2000)
        assert appmod._bundle_needs_rebuild(spa, frontend) is False

    def test_stale_bundle_needs_rebuild(self, tmp_path):
        frontend = tmp_path / "frontend"
        _touch(frontend / "package.json", mtime=1000)
        spa = _touch(tmp_path / "static/dist/index.html", mtime=1000)
        _touch(frontend / "src/main.tsx", mtime=2000)  # edited after the last build
        assert appmod._bundle_needs_rebuild(spa, frontend) is True

    def test_newest_mtime_ignores_node_modules(self, tmp_path):
        frontend = tmp_path / "frontend"
        _touch(frontend / "package.json", mtime=1000)
        _touch(frontend / "src/a.tsx", mtime=1500)
        _touch(frontend / "node_modules/huge/x.js", mtime=9999)  # must not count
        assert appmod._frontend_newest_mtime(frontend) == 1500


class TestEnsureSpaBuilt:
    def _wire(self, monkeypatch, static, frontend):
        monkeypatch.setattr(appmod, "_STATIC_DIR", static)
        monkeypatch.setattr(appmod, "_FRONTEND_DIR", frontend)

    def test_fresh_bundle_serves_without_building(self, tmp_path, monkeypatch):
        static, frontend = tmp_path / "static", tmp_path / "frontend"
        _touch(static / "dist/index.html", mtime=2000)
        _touch(frontend / "package.json", mtime=1000)
        self._wire(monkeypatch, static, frontend)
        calls: list = []
        monkeypatch.setattr(appmod.subprocess, "run", lambda *a, **k: calls.append(a))
        assert appmod._ensure_spa_built() == static / "dist/index.html"
        assert calls == [], "a fresh bundle must not trigger a build"

    def test_stale_bundle_without_npm_serves_stale(self, tmp_path, monkeypatch):
        static, frontend = tmp_path / "static", tmp_path / "frontend"
        _touch(static / "dist/index.html", mtime=1000)
        _touch(frontend / "package.json", mtime=1000)
        _touch(frontend / "src/a.tsx", mtime=2000)  # stale
        (frontend / "node_modules").mkdir()
        self._wire(monkeypatch, static, frontend)

        def _no_npm(*a, **k):
            raise FileNotFoundError("npm")

        monkeypatch.setattr(appmod.subprocess, "run", _no_npm)
        # Stale + can't rebuild → serve the existing bundle, do NOT raise.
        assert appmod._ensure_spa_built() == static / "dist/index.html"

    def test_missing_bundle_without_npm_is_fatal(self, tmp_path, monkeypatch):
        static, frontend = tmp_path / "static", tmp_path / "frontend"
        _touch(frontend / "package.json")
        (frontend / "node_modules").mkdir()
        self._wire(monkeypatch, static, frontend)

        def _no_npm(*a, **k):
            raise FileNotFoundError("npm")

        monkeypatch.setattr(appmod.subprocess, "run", _no_npm)
        with pytest.raises(RuntimeError, match="npm"):
            appmod._ensure_spa_built()
