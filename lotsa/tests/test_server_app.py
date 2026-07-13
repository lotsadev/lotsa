"""Tests for the non-API routes served by the dashboard app (lotsa/server/app.py)."""

from __future__ import annotations

from httpx import ASGITransport, AsyncClient


class TestLegalRoutes:
    def test_privacy_page(self, app_with_service, run):
        app, _ = app_with_service

        async def _test():
            transport = ASGITransport(app=app)
            async with AsyncClient(transport=transport, base_url="http://test") as client:
                resp = await client.get("/privacy")
                assert resp.status_code == 200
                assert resp.headers["content-type"].startswith("text/html")
                assert "Privacy Policy" in resp.text

        run(_test())

    def test_terms_page(self, app_with_service, run):
        app, _ = app_with_service

        async def _test():
            transport = ASGITransport(app=app)
            async with AsyncClient(transport=transport, base_url="http://test") as client:
                resp = await client.get("/terms")
                assert resp.status_code == 200
                assert resp.headers["content-type"].startswith("text/html")
                assert "Terms of Use" in resp.text

        run(_test())
