"""Load prompt files from disk with layered override support.

Extracted from: bot/orchestrator.py prompt loading pattern (lines 755-756).
"""

from __future__ import annotations

from pathlib import Path


class PromptNotFound(Exception):
    """Raised when a prompt file cannot be found in any search path."""


class PromptRegistry:
    """Load prompt markdown files from disk with search path priority.

    Given search_paths=[override_dir, base_dir], a call to load("coding")
    searches for "coding.md" in override_dir first, then base_dir.
    """

    def __init__(self, search_paths: list[Path]) -> None:
        self._search_paths = search_paths

    def load(self, name: str) -> str:
        """Search paths in order, return first {name}.md match.

        Raises PromptNotFound if not found in any path.
        """
        # ``name`` comes from a process YAML ``prompt:`` field — reject any value
        # that could escape the search-path roots (``../../etc/passwd``, an
        # absolute path). Prompt names are plain basenames (finding #6).
        if not name or "/" in name or "\\" in name or ".." in name or Path(name).is_absolute():
            raise PromptNotFound(f"Invalid prompt name {name!r} — must be a plain basename")
        for path in self._search_paths:
            candidate = path / f"{name}.md"
            if candidate.is_file():
                return candidate.read_text()
        raise PromptNotFound(f"Prompt '{name}' not found in search paths: {[str(p) for p in self._search_paths]}")

    def load_optional(self, name: str) -> str | None:
        """Like load() but returns None if not found."""
        try:
            return self.load(name)
        except PromptNotFound:
            return None
