"""Tests for Rigg PromptRegistry."""

import pytest

from rigg.prompt_registry import PromptNotFound, PromptRegistry


def test_load_from_single_path(tmp_path):
    (tmp_path / "coding.md").write_text("You are a coding agent.")
    reg = PromptRegistry(search_paths=[tmp_path])
    assert reg.load("coding") == "You are a coding agent."


def test_load_not_found(tmp_path):
    reg = PromptRegistry(search_paths=[tmp_path])
    with pytest.raises(PromptNotFound, match="missing"):
        reg.load("missing")


def test_load_optional_returns_none(tmp_path):
    reg = PromptRegistry(search_paths=[tmp_path])
    assert reg.load_optional("missing") is None


def test_load_optional_returns_content(tmp_path):
    (tmp_path / "review.md").write_text("Review this code.")
    reg = PromptRegistry(search_paths=[tmp_path])
    assert reg.load_optional("review") == "Review this code."


def test_search_path_priority(tmp_path):
    """First path wins when both have the same prompt."""
    override = tmp_path / "override"
    base = tmp_path / "base"
    override.mkdir()
    base.mkdir()
    (override / "coding.md").write_text("OVERRIDE")
    (base / "coding.md").write_text("BASE")

    reg = PromptRegistry(search_paths=[override, base])
    assert reg.load("coding") == "OVERRIDE"


def test_fallback_to_later_path(tmp_path):
    """If first path doesn't have the prompt, try the next."""
    override = tmp_path / "override"
    base = tmp_path / "base"
    override.mkdir()
    base.mkdir()
    (base / "review.md").write_text("BASE REVIEW")

    reg = PromptRegistry(search_paths=[override, base])
    assert reg.load("review") == "BASE REVIEW"


def test_empty_search_paths():
    reg = PromptRegistry(search_paths=[])
    with pytest.raises(PromptNotFound):
        reg.load("anything")


def test_nonexistent_search_path(tmp_path):
    """Non-existent directories are silently skipped."""
    missing = tmp_path / "does-not-exist"
    real = tmp_path / "real"
    real.mkdir()
    (real / "coding.md").write_text("content")

    reg = PromptRegistry(search_paths=[missing, real])
    assert reg.load("coding") == "content"
