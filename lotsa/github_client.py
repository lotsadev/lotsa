"""Thin httpx wrapper for GitHub API operations.

Covers the calls needed by the push step and PR monitor:
- Repository metadata (default branch)
- Pull request lifecycle (create, read, reviews, comments)
- CI check status aggregation
"""

from __future__ import annotations

import re
from dataclasses import dataclass, field

import httpx

_HTTPS_RE = re.compile(r"https://github\.com/(?P<owner>[^/]+)/(?P<repo>[^/]+?)(?:\.git)?$")
_SSH_RE = re.compile(r"git@github\.com:(?P<owner>[^/]+)/(?P<repo>[^/]+?)(?:\.git)?$")


def parse_github_remote(url: str) -> tuple[str, str]:
    """Extract (owner, repo) from an HTTPS or SSH GitHub remote URL.

    Args:
        url: Remote URL, e.g. ``https://github.com/acme/my-repo.git``
             or ``git@github.com:acme/my-repo.git``.

    Returns:
        ``(owner, repo)`` tuple with the ``.git`` suffix stripped.

    Raises:
        ValueError: If the URL does not point to github.com.
    """
    for pattern in (_HTTPS_RE, _SSH_RE):
        m = pattern.match(url.strip())
        if m:
            return m.group("owner"), m.group("repo")
    # Sanitize credentials from URL before including in error message
    safe_url = re.sub(r"://[^@]+@", "://***@", url)
    raise ValueError(f"Not a GitHub URL: {safe_url!r}")


@dataclass
class PrInfo:
    """Minimal pull-request metadata returned by the GitHub API."""

    number: int
    state: str  # "open" or "closed"
    merged: bool
    review_decision: str | None  # e.g. "APPROVED", "CHANGES_REQUESTED", None
    head_sha: str
    base_branch: str
    mergeable: bool | None = None  # None = GitHub hasn't computed yet; False = CONFLICTING


@dataclass
class ReviewComment:
    """A single review comment or issue comment on a pull request."""

    id: int
    author: str
    body: str
    path: str | None  # None for issue-level comments
    line: int | None  # None for issue-level comments
    created_at: str  # ISO 8601 string as returned by the API
    # ``updated_at`` advances when a comment is edited in place. The claude
    # PR-review bot edits its single comment across rounds, so the
    # ``pr_feedback`` audit trail (one row per comment-version) keys off
    # this. Defaults to ``created_at`` so callers constructing instances
    # directly remain back-compat; ``__post_init__`` fills the blank.
    updated_at: str = ""
    # ``html_url`` is the public GitHub URL of the comment. Captured so
    # ``pr_feedback`` metadata can link back to the source.
    html_url: str = ""

    def __post_init__(self) -> None:
        # GitHub returns ``updated_at`` on every payload, but constructors
        # in tests sometimes omit it — fall back to ``created_at`` so the
        # field is never an empty string downstream.
        if not self.updated_at:
            self.updated_at = self.created_at


@dataclass
class CheckStatus:
    """Aggregated CI check-run status for a commit SHA."""

    total: int
    passing: int
    failing: int
    pending: int
    failing_names: list[str] = field(default_factory=list)


def compute_review_decision(reviews: list[dict]) -> str | None:
    """Derive an aggregate review decision from individual review objects.

    The GitHub REST API does not return ``review_decision`` on the PR
    object (that field is GraphQL-only).  This function replicates the
    logic: walk reviews in chronological order, tracking each reviewer's
    most recent actionable review (APPROVED / CHANGES_REQUESTED).  A
    later DISMISSED submission for the same reviewer clears their entry
    — without this, a stale APPROVED would survive a maintainer's
    dismissal and produce a false-positive completion signal.  COMMENTED
    and PENDING are skipped (non-actionable, non-clearing).

    Returns:
    - ``"CHANGES_REQUESTED"`` if any reviewer's latest actionable review is CHANGES_REQUESTED.
    - ``"APPROVED"`` if at least one reviewer approved and none requested changes.
    - ``None`` otherwise.
    """
    _ACTIONABLE = {"APPROVED", "CHANGES_REQUESTED"}
    latest_by_reviewer: dict[str, str] = {}
    for review in reviews:
        login = (review.get("user") or {}).get("login", "")
        if not login:
            continue
        state = review.get("state", "")
        if state in _ACTIONABLE:
            latest_by_reviewer[login] = state
        elif state == "DISMISSED":
            # Reviewer's prior review was dismissed — drop it so a stale
            # APPROVED/CHANGES_REQUESTED doesn't influence the aggregate.
            latest_by_reviewer.pop(login, None)
    if not latest_by_reviewer:
        return None
    if any(s == "CHANGES_REQUESTED" for s in latest_by_reviewer.values()):
        return "CHANGES_REQUESTED"
    if all(s == "APPROVED" for s in latest_by_reviewer.values()):
        return "APPROVED"
    return None


class GitHubClient:
    """Async GitHub API client backed by httpx.

    Usage::

        client = GitHubClient(token="ghp_...", owner="acme", repo="my-repo")
        try:
            pr_number = await client.create_pr(title="...", body="...", head="feat", base="main")
        finally:
            await client.close()
    """

    _BASE = "https://api.github.com"

    def __init__(self, token: str, owner: str, repo: str) -> None:
        self._owner = owner
        self._repo = repo
        self._client = httpx.AsyncClient(
            base_url=self._BASE,
            headers={
                "Authorization": f"Bearer {token}",
                "Accept": "application/vnd.github+json",
                "X-GitHub-Api-Version": "2022-11-28",
            },
            timeout=30.0,
        )

    async def close(self) -> None:
        """Close the underlying httpx client."""
        await self._client.aclose()

    # ------------------------------------------------------------------
    # Repository
    # ------------------------------------------------------------------

    async def get_default_branch(self) -> str:
        """Return the repository's default branch name (e.g. ``"main"``)."""
        resp = await self._client.get(f"/repos/{self._owner}/{self._repo}")
        resp.raise_for_status()
        return resp.json()["default_branch"]

    # ------------------------------------------------------------------
    # Pull requests
    # ------------------------------------------------------------------

    async def create_pr(self, title: str, body: str, head: str, base: str) -> int:
        """Open a pull request and return its number.

        Args:
            title: PR title.
            body: PR description / body text.
            head: Head branch name.
            base: Base branch name.

        Returns:
            The PR number assigned by GitHub.
        """
        try:
            resp = await self._client.post(
                f"/repos/{self._owner}/{self._repo}/pulls",
                json={"title": title, "body": body, "head": head, "base": base},
            )
            resp.raise_for_status()
            return resp.json()["number"]
        except httpx.HTTPStatusError as exc:
            if exc.response.status_code == 422:
                # PR already exists for this head branch — recover its number.
                # Only search open PRs; a closed PR would cause the monitor to
                # immediately classify the task as ABANDONED.
                existing = await self._client.get(
                    f"/repos/{self._owner}/{self._repo}/pulls",
                    params={"head": f"{self._owner}:{head}", "state": "open"},
                )
                existing.raise_for_status()
                prs = existing.json()
                if prs:
                    return prs[0]["number"]
            raise

    async def get_pr(self, pr_number: int) -> PrInfo:
        """Fetch metadata for a single pull request.

        Note: ``review_decision`` is always ``None`` here because the
        GitHub REST API does not expose it.  Call
        :func:`compute_review_decision` with the result of
        :meth:`get_reviews` to derive it.
        """
        resp = await self._client.get(f"/repos/{self._owner}/{self._repo}/pulls/{pr_number}")
        resp.raise_for_status()
        data = resp.json()
        return PrInfo(
            number=data["number"],
            state=data["state"],
            merged=bool(data.get("merged")),
            review_decision=None,
            head_sha=data["head"]["sha"],
            base_branch=data["base"]["ref"],
            mergeable=data.get("mergeable"),
        )

    async def get_reviews(self, pr_number: int) -> list[dict]:
        """Return raw review objects for a pull request.

        Note: returns up to 100 reviews. Full pagination out of scope for MVP.
        """
        resp = await self._client.get(
            f"/repos/{self._owner}/{self._repo}/pulls/{pr_number}/reviews",
            params={"per_page": 100},
        )
        resp.raise_for_status()
        return resp.json()

    async def get_review_comments(self, pr_number: int, since: str | None = None) -> list[ReviewComment]:
        """Return inline review comments (diff-level) for a pull request.

        Args:
            pr_number: Pull request number.
            since: Optional ISO 8601 timestamp; only return comments created
                   at or after this time.

        Note: returns up to 100 comments. Full pagination out of scope for MVP.
        """
        params: dict = {"per_page": 100}
        if since:
            params["since"] = since
        resp = await self._client.get(
            f"/repos/{self._owner}/{self._repo}/pulls/{pr_number}/comments",
            params=params,
        )
        resp.raise_for_status()
        return [_parse_review_comment(c) for c in resp.json()]

    async def get_issue_comments(self, pr_number: int, since: str | None = None) -> list[ReviewComment]:
        """Return issue-level comments (conversation thread) for a pull request.

        Args:
            pr_number: Pull request number.
            since: Optional ISO 8601 timestamp filter.

        Note: returns up to 100 comments per page. PRs with >100 comments
        may miss items. Full pagination is out of scope for MVP.
        """
        params: dict = {"per_page": 100}
        if since:
            params["since"] = since
        resp = await self._client.get(
            f"/repos/{self._owner}/{self._repo}/issues/{pr_number}/comments",
            params=params,
        )
        resp.raise_for_status()
        return [_parse_issue_comment(c) for c in resp.json()]

    # ------------------------------------------------------------------
    # Checks
    # ------------------------------------------------------------------

    async def get_check_status(self, sha: str) -> CheckStatus:
        """Aggregate check-run results for a commit SHA.

        Note: returns up to 100 check runs. Full pagination out of scope for MVP.

        Returns a :class:`CheckStatus` with counts for passing, failing, and
        pending runs, plus the names of any failing checks.
        """
        resp = await self._client.get(
            f"/repos/{self._owner}/{self._repo}/commits/{sha}/check-runs",
            params={"per_page": 100},
        )
        resp.raise_for_status()
        runs = resp.json().get("check_runs", [])

        passing = 0
        failing = 0
        pending = 0
        failing_names: list[str] = []

        for run in runs:
            conclusion = run.get("conclusion")
            # "neutral", "skipped", and "stale" are non-actionable terminal
            # outcomes — GitHub returns them for path-filtered or otherwise
            # non-applicable runs. Bucket them with success so the UI's
            # "passing/total" doesn't permanently lag behind on PRs that
            # have skipped jobs.
            if conclusion in ("success", "neutral", "skipped", "stale"):
                passing += 1
            elif conclusion in ("failure", "timed_out", "cancelled", "action_required"):
                failing += 1
                failing_names.append(run["name"])
            else:
                # in_progress, queued, waiting, or unknown conclusion
                pending += 1

        return CheckStatus(
            total=len(runs),
            passing=passing,
            failing=failing,
            pending=pending,
            failing_names=failing_names,
        )


# ---------------------------------------------------------------------------
# Internal helpers
# ---------------------------------------------------------------------------


def _parse_review_comment(data: dict) -> ReviewComment:
    return ReviewComment(
        id=data["id"],
        author=(data.get("user") or {}).get("login", "[deleted]"),
        body=data["body"],
        path=data.get("path"),
        line=data.get("line"),
        created_at=data["created_at"],
        updated_at=data.get("updated_at") or data["created_at"],
        html_url=data.get("html_url", ""),
    )


def _parse_issue_comment(data: dict) -> ReviewComment:
    return ReviewComment(
        id=data["id"],
        author=(data.get("user") or {}).get("login", "[deleted]"),
        body=data["body"],
        path=None,
        line=None,
        created_at=data["created_at"],
        updated_at=data.get("updated_at") or data["created_at"],
        html_url=data.get("html_url", ""),
    )
