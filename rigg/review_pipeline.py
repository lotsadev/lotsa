"""Code review assessment with pluggable parsers.

Extracted from: bot/orchestrator.py review_status() (lines 446-471)
and get_latest_review_comment() (lines 413-433).
"""

from __future__ import annotations

import re
from typing import Protocol

from rigg.models import ReviewStatus


class ReviewParser(Protocol):
    """Pluggable review format parser."""

    def extract_issues(self, body: str, min_severity: str) -> list[str]: ...

    def is_stale(self, review_timestamp: str, head_commit_timestamp: str) -> bool: ...


# Severity levels ordered from lowest to highest
_SEVERITY_RANK = {"Low": 0, "Medium": 1, "High": 2, "Critical": 3}


class MarkdownReviewParser:
    """Parses ## Medium / ## High headings from markdown review output.

    Matches the format used by the Claude Code Review GitHub Action.
    """

    def extract_issues(self, body: str, min_severity: str = "Medium") -> list[str]:
        min_rank = _SEVERITY_RANK.get(min_severity, 1)
        issues: list[str] = []
        current_severity: str | None = None

        for line in body.splitlines():
            # Match severity headings: ## Medium, ## High, etc.
            heading_match = re.match(r"^#{2,}\s+(Low|Medium|High|Critical)\s*$", line)
            if heading_match:
                current_severity = heading_match.group(1)
                continue

            # Match other headings — reset current section
            if re.match(r"^#{1,}\s+", line):
                current_severity = None
                continue

            # Collect bullet items under qualifying severity
            if current_severity and _SEVERITY_RANK.get(current_severity, 0) >= min_rank:
                bullet_match = re.match(r"^[-*]\s+(.+)$", line.strip())
                if bullet_match:
                    issues.append(bullet_match.group(1))

        return issues

    def is_stale(self, review_timestamp: str, head_commit_timestamp: str) -> bool:
        if not review_timestamp or not head_commit_timestamp:
            return False
        return review_timestamp < head_commit_timestamp


class ReviewPipeline:
    """Assess code review status and extract actionable issues."""

    def __init__(self, parser: ReviewParser | None = None) -> None:
        self._parser = parser or MarkdownReviewParser()

    def assess(self, review_body: str, review_ts: str, head_commit_ts: str) -> ReviewStatus:
        """Determine review status: PENDING (stale), CLEAN, or FEEDBACK."""
        if self._parser.is_stale(review_ts, head_commit_ts):
            return ReviewStatus.PENDING

        issues = self._parser.extract_issues(review_body, min_severity="Medium")
        if issues:
            return ReviewStatus.FEEDBACK
        return ReviewStatus.CLEAN

    def get_issues(self, review_body: str, min_severity: str = "Medium") -> list[str]:
        """Extract actionable issues from a review body."""
        return self._parser.extract_issues(review_body, min_severity)
