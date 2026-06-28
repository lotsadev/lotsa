"""Tests for Rigg ReviewPipeline."""

from rigg.models import ReviewStatus
from rigg.review_pipeline import MarkdownReviewParser, ReviewPipeline

CLEAN_REVIEW = """### PR Review

## Summary
Code looks good.

## Low
- Minor style issue on line 42.
"""

FEEDBACK_REVIEW = """### PR Review

## Summary
Several issues found.

## Medium
- Missing error handling in `process()`.
- SQL injection risk in query builder.

## Low
- Unused import on line 5.
"""

HIGH_REVIEW = """### PR Review

## High
- Authentication bypass possible.
"""


class TestMarkdownReviewParser:
    def test_extract_no_issues(self):
        parser = MarkdownReviewParser()
        issues = parser.extract_issues(CLEAN_REVIEW, min_severity="Medium")
        assert issues == []

    def test_extract_medium_issues(self):
        parser = MarkdownReviewParser()
        issues = parser.extract_issues(FEEDBACK_REVIEW, min_severity="Medium")
        assert len(issues) == 2
        assert "Missing error handling" in issues[0]

    def test_extract_high_issues(self):
        parser = MarkdownReviewParser()
        issues = parser.extract_issues(HIGH_REVIEW, min_severity="Medium")
        assert len(issues) == 1
        assert "Authentication bypass" in issues[0]

    def test_extract_high_only(self):
        parser = MarkdownReviewParser()
        issues = parser.extract_issues(FEEDBACK_REVIEW, min_severity="High")
        assert issues == []

    def test_stale_review(self):
        parser = MarkdownReviewParser()
        assert parser.is_stale("2026-03-15T10:00:00Z", "2026-03-15T11:00:00Z") is True

    def test_fresh_review(self):
        parser = MarkdownReviewParser()
        assert parser.is_stale("2026-03-15T12:00:00Z", "2026-03-15T11:00:00Z") is False

    def test_stale_empty_timestamps(self):
        parser = MarkdownReviewParser()
        assert parser.is_stale("", "") is False


class TestReviewPipeline:
    def test_assess_clean(self):
        pipeline = ReviewPipeline()
        status = pipeline.assess(CLEAN_REVIEW, "2026-03-15T12:00:00Z", "2026-03-15T11:00:00Z")
        assert status == ReviewStatus.CLEAN

    def test_assess_feedback(self):
        pipeline = ReviewPipeline()
        status = pipeline.assess(FEEDBACK_REVIEW, "2026-03-15T12:00:00Z", "2026-03-15T11:00:00Z")
        assert status == ReviewStatus.FEEDBACK

    def test_assess_stale(self):
        pipeline = ReviewPipeline()
        status = pipeline.assess(CLEAN_REVIEW, "2026-03-15T10:00:00Z", "2026-03-15T11:00:00Z")
        assert status == ReviewStatus.PENDING

    def test_get_issues(self):
        pipeline = ReviewPipeline()
        issues = pipeline.get_issues(FEEDBACK_REVIEW)
        assert len(issues) == 2

    def test_assess_empty_body(self):
        pipeline = ReviewPipeline()
        status = pipeline.assess("", "2026-03-15T12:00:00Z", "2026-03-15T11:00:00Z")
        assert status == ReviewStatus.CLEAN
