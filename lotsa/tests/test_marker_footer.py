"""The mandatory-marker footer is derived from a step's stdout rules (ADR-039 stopgap)."""

from __future__ import annotations

from lotsa.flows import OutputRule
from lotsa.orchestrator import _marker_requirement_footer


def test_lists_only_stdout_markers():
    rules = [
        OutputRule(source="stdout", pattern="^VERIFIED:", target="next"),
        OutputRule(source="stdout", pattern="^NEEDS_CODE:", target="code"),
        OutputRule(source="report.md", pattern="^X", target="next"),  # non-stdout → ignored
    ]
    footer = _marker_requirement_footer(rules)
    assert "`VERIFIED:`" in footer
    assert "`NEEDS_CODE:`" in footer
    assert "mandatory" in footer.lower()
    assert "report.md" not in footer and "^X" not in footer  # only stdout markers, ^ stripped


def test_empty_when_no_stdout_rules():
    assert _marker_requirement_footer([]) == ""
    assert _marker_requirement_footer([OutputRule(source="out.txt", pattern="^Y", target="next")]) == ""
