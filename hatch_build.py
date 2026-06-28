"""Hatchling build hook — bundle the dashboard into the package (ADR-036).

So that `pip install lotsa` ships a ready-to-serve dashboard and end users need
no Node toolchain, the React bundle (`lotsa/server/static/dist/`, which is
gitignored) is built here at packaging time and included in the wheel via the
``artifacts`` glob in ``pyproject.toml``.

Building a distributable wheel/sdist therefore requires Node.js + npm. Editable
installs (`pip install -e`) are skipped — the dev's `make setup` / runtime
auto-build handles the bundle there, so an editable install never needs Node.
"""

from __future__ import annotations

import shutil
import subprocess
from pathlib import Path

from hatchling.builders.hooks.plugin.interface import BuildHookInterface


class CustomBuildHook(BuildHookInterface):
    def initialize(self, version: str, build_data: dict) -> None:
        # Editable installs read the bundle from the source tree at runtime and
        # must not depend on Node — leave them to make setup / runtime build.
        if build_data.get("editable"):
            return

        root = Path(self.root)
        index = root / "lotsa" / "server" / "static" / "dist" / "index.html"
        if index.exists():
            return  # already built (e.g. `make frontend` ran first)

        frontend = root / "lotsa" / "frontend"
        if not (frontend / "package.json").exists():
            return
        if shutil.which("npm") is None:
            raise RuntimeError(
                "Building a Lotsa wheel requires Node.js/npm to bundle the dashboard. "
                "Install Node.js, or run `make frontend` before building."
            )
        if not (frontend / "node_modules").exists():
            subprocess.run(["npm", "install"], cwd=frontend, check=True)
        subprocess.run(["npm", "run", "build"], cwd=frontend, check=True)
        if not index.exists():
            raise RuntimeError(f"Dashboard build completed but {index} is missing — check Vite output.")
