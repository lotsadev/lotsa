"""Prompt-name traversal is rejected (audit finding #6)."""

from __future__ import annotations

import pytest

from rigg.prompt_registry import PromptNotFound, PromptRegistry


class TestPromptNameTraversal:
    def test_parent_traversal_rejected(self, tmp_path):
        reg = PromptRegistry([tmp_path])
        with pytest.raises(PromptNotFound):
            reg.load("../../../../etc/passwd")

    def test_absolute_path_rejected(self, tmp_path):
        reg = PromptRegistry([tmp_path])
        with pytest.raises(PromptNotFound):
            reg.load("/etc/passwd")

    def test_separator_rejected(self, tmp_path):
        reg = PromptRegistry([tmp_path])
        with pytest.raises(PromptNotFound):
            reg.load("sub/coding")

    def test_plain_basename_still_loads(self, tmp_path):
        (tmp_path / "coding.md").write_text("hello")
        assert PromptRegistry([tmp_path]).load("coding") == "hello"
