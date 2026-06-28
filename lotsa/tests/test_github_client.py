"""Tests for GitHubClient — mocked httpx responses."""

from __future__ import annotations

from unittest.mock import AsyncMock, MagicMock, patch

import pytest

from lotsa.github_client import (
    CheckStatus,
    GitHubClient,
    PrInfo,
    ReviewComment,
    compute_review_decision,
    parse_github_remote,
)

# ---------------------------------------------------------------------------
# parse_github_remote
# ---------------------------------------------------------------------------


def test_parse_https_with_git_suffix():
    owner, repo = parse_github_remote("https://github.com/acme/my-repo.git")
    assert owner == "acme"
    assert repo == "my-repo"


def test_parse_https_without_git_suffix():
    owner, repo = parse_github_remote("https://github.com/acme/my-repo")
    assert owner == "acme"
    assert repo == "my-repo"


def test_parse_ssh_format():
    owner, repo = parse_github_remote("git@github.com:acme/my-repo.git")
    assert owner == "acme"
    assert repo == "my-repo"


def test_parse_ssh_format_without_git_suffix():
    owner, repo = parse_github_remote("git@github.com:acme/my-repo")
    assert owner == "acme"
    assert repo == "my-repo"


def test_parse_invalid_url_raises():
    with pytest.raises(ValueError, match="GitHub"):
        parse_github_remote("https://gitlab.com/acme/repo.git")


def test_parse_empty_url_raises():
    with pytest.raises(ValueError):
        parse_github_remote("")


def test_parse_non_github_https_raises():
    with pytest.raises(ValueError, match="GitHub"):
        parse_github_remote("https://bitbucket.org/acme/repo")


def test_parse_credential_url_does_not_leak_token():
    """Error message must not contain embedded credentials."""
    with pytest.raises(ValueError) as exc_info:
        parse_github_remote("https://ghp_secret123@gitlab.com/acme/repo.git")
    assert "ghp_secret123" not in str(exc_info.value)
    assert "***" in str(exc_info.value)


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def _make_response(json_data, status_code=200):
    """Return a mock httpx.Response with .json() and .raise_for_status()."""
    resp = MagicMock()
    resp.status_code = status_code
    resp.json.return_value = json_data
    resp.raise_for_status = MagicMock()
    return resp


def _make_client(token="tok-test", owner="acme", repo="my-repo"):
    return GitHubClient(token=token, owner=owner, repo=repo)


# ---------------------------------------------------------------------------
# get_default_branch
# ---------------------------------------------------------------------------


async def test_get_default_branch():
    client = _make_client()
    mock_resp = _make_response({"default_branch": "main"})

    with patch.object(client._client, "get", new=AsyncMock(return_value=mock_resp)) as mock_get:
        branch = await client.get_default_branch()

    assert branch == "main"
    mock_get.assert_called_once()
    assert "/repos/acme/my-repo" in str(mock_get.call_args)

    await client.close()


# ---------------------------------------------------------------------------
# create_pr
# ---------------------------------------------------------------------------


async def test_create_pr_returns_pr_number():
    client = _make_client()
    mock_resp = _make_response({"number": 42, "html_url": "https://github.com/acme/my-repo/pull/42"})

    with patch.object(client._client, "post", new=AsyncMock(return_value=mock_resp)):
        pr_number = await client.create_pr(
            title="My PR",
            body="Description",
            head="feature/thing",
            base="main",
        )

    assert pr_number == 42

    await client.close()


async def test_create_pr_sends_correct_payload():
    client = _make_client()
    mock_resp = _make_response({"number": 7})

    with patch.object(client._client, "post", new=AsyncMock(return_value=mock_resp)) as mock_post:
        await client.create_pr(title="T", body="B", head="feat", base="main")

    payload = mock_post.call_args.kwargs.get("json") or mock_post.call_args[1].get("json")
    assert payload["title"] == "T"
    assert payload["head"] == "feat"
    assert payload["base"] == "main"

    await client.close()


# ---------------------------------------------------------------------------
# get_pr
# ---------------------------------------------------------------------------


async def test_get_pr_open():
    client = _make_client()
    mock_resp = _make_response(
        {
            "number": 5,
            "state": "open",
            "merged": False,
            "review_decision": None,
            "head": {"sha": "abc123"},
            "base": {"ref": "main"},
        }
    )

    with patch.object(client._client, "get", new=AsyncMock(return_value=mock_resp)):
        pr = await client.get_pr(5)

    assert isinstance(pr, PrInfo)
    assert pr.number == 5
    assert pr.state == "open"
    assert pr.merged is False
    assert pr.review_decision is None
    assert pr.head_sha == "abc123"
    assert pr.base_branch == "main"

    await client.close()


async def test_get_pr_merged():
    client = _make_client()
    mock_resp = _make_response(
        {
            "number": 10,
            "state": "closed",
            "merged": True,
            "head": {"sha": "def456"},
            "base": {"ref": "develop"},
        }
    )

    with patch.object(client._client, "get", new=AsyncMock(return_value=mock_resp)):
        pr = await client.get_pr(10)

    assert pr.state == "closed"
    assert pr.merged is True
    # review_decision is always None from REST API — computed separately
    assert pr.review_decision is None

    await client.close()


# ---------------------------------------------------------------------------
# get_check_status
# ---------------------------------------------------------------------------


async def test_get_check_status_all_passing():
    client = _make_client()
    mock_resp = _make_response(
        {
            "check_runs": [
                {"name": "ci/lint", "conclusion": "success", "status": "completed"},
                {"name": "ci/test", "conclusion": "success", "status": "completed"},
            ]
        }
    )

    with patch.object(client._client, "get", new=AsyncMock(return_value=mock_resp)):
        status = await client.get_check_status("abc123")

    assert isinstance(status, CheckStatus)
    assert status.total == 2
    assert status.passing == 2
    assert status.failing == 0
    assert status.pending == 0
    assert status.failing_names == []

    await client.close()


async def test_get_check_status_with_failures():
    client = _make_client()
    mock_resp = _make_response(
        {
            "check_runs": [
                {"name": "ci/lint", "conclusion": "failure", "status": "completed"},
                {"name": "ci/test", "conclusion": "success", "status": "completed"},
                {"name": "ci/build", "conclusion": None, "status": "in_progress"},
            ]
        }
    )

    with patch.object(client._client, "get", new=AsyncMock(return_value=mock_resp)):
        status = await client.get_check_status("sha999")

    assert status.total == 3
    assert status.passing == 1
    assert status.failing == 1
    assert status.pending == 1
    assert "ci/lint" in status.failing_names

    await client.close()


async def test_get_check_status_buckets_neutral_skipped_stale_as_passing():
    """neutral/skipped/stale are non-actionable terminal outcomes — bucket
    them with success so the UI's checks N/M counter doesn't lag behind
    permanently on PRs that have path-filtered or skipped jobs."""
    client = _make_client()
    mock_resp = _make_response(
        {
            "check_runs": [
                {"name": "ci/lint", "conclusion": "success", "status": "completed"},
                {"name": "ci/neutral", "conclusion": "neutral", "status": "completed"},
                {"name": "ci/skipped", "conclusion": "skipped", "status": "completed"},
                {"name": "ci/stale", "conclusion": "stale", "status": "completed"},
                {"name": "ci/test", "conclusion": "failure", "status": "completed"},
            ]
        }
    )

    with patch.object(client._client, "get", new=AsyncMock(return_value=mock_resp)):
        status = await client.get_check_status("sha_mixed")

    assert status.total == 5
    assert status.passing == 4
    assert status.failing == 1
    assert status.pending == 0
    assert status.failing_names == ["ci/test"]

    await client.close()


async def test_get_check_status_empty():
    client = _make_client()
    mock_resp = _make_response({"check_runs": []})

    with patch.object(client._client, "get", new=AsyncMock(return_value=mock_resp)):
        status = await client.get_check_status("sha000")

    assert status.total == 0
    assert status.passing == 0
    assert status.failing == 0
    assert status.pending == 0

    await client.close()


# ---------------------------------------------------------------------------
# get_review_comments
# ---------------------------------------------------------------------------


async def test_get_review_comments():
    client = _make_client()
    mock_resp = _make_response(
        [
            {
                "id": 1,
                "user": {"login": "reviewer"},
                "body": "Fix this",
                "path": "src/main.py",
                "line": 42,
                "created_at": "2024-01-15T10:00:00Z",
            }
        ]
    )

    with patch.object(client._client, "get", new=AsyncMock(return_value=mock_resp)):
        comments = await client.get_review_comments(5)

    assert len(comments) == 1
    c = comments[0]
    assert isinstance(c, ReviewComment)
    assert c.id == 1
    assert c.author == "reviewer"
    assert c.body == "Fix this"
    assert c.path == "src/main.py"
    assert c.line == 42

    await client.close()


# ---------------------------------------------------------------------------
# get_issue_comments
# ---------------------------------------------------------------------------


async def test_get_issue_comments():
    client = _make_client()
    mock_resp = _make_response(
        [
            {
                "id": 99,
                "user": {"login": "bob"},
                "body": "LGTM",
                "created_at": "2024-02-01T12:00:00Z",
            }
        ]
    )

    with patch.object(client._client, "get", new=AsyncMock(return_value=mock_resp)):
        comments = await client.get_issue_comments(3)

    assert len(comments) == 1
    c = comments[0]
    assert c.id == 99
    assert c.author == "bob"
    assert c.body == "LGTM"
    assert c.path is None
    assert c.line is None

    await client.close()


# ---------------------------------------------------------------------------
# get_reviews
# ---------------------------------------------------------------------------


async def test_get_reviews():
    client = _make_client()
    raw = [{"id": 1, "state": "APPROVED", "user": {"login": "alice"}}]
    mock_resp = _make_response(raw)

    with patch.object(client._client, "get", new=AsyncMock(return_value=mock_resp)):
        reviews = await client.get_reviews(5)

    assert reviews == raw

    await client.close()


# ---------------------------------------------------------------------------
# close
# ---------------------------------------------------------------------------


# ---------------------------------------------------------------------------
# compute_review_decision
# ---------------------------------------------------------------------------


def test_compute_review_decision_no_reviews():
    assert compute_review_decision([]) is None


def test_compute_review_decision_all_approved():
    reviews = [
        {"user": {"login": "alice"}, "state": "APPROVED"},
        {"user": {"login": "bob"}, "state": "APPROVED"},
    ]
    assert compute_review_decision(reviews) == "APPROVED"


def test_compute_review_decision_any_changes_requested():
    reviews = [
        {"user": {"login": "alice"}, "state": "APPROVED"},
        {"user": {"login": "bob"}, "state": "CHANGES_REQUESTED"},
    ]
    assert compute_review_decision(reviews) == "CHANGES_REQUESTED"


def test_compute_review_decision_latest_per_reviewer_wins():
    """A reviewer who first requests changes, then approves — latest wins."""
    reviews = [
        {"user": {"login": "alice"}, "state": "CHANGES_REQUESTED"},
        {"user": {"login": "alice"}, "state": "APPROVED"},
    ]
    assert compute_review_decision(reviews) == "APPROVED"


def test_compute_review_decision_ignores_commented():
    """COMMENTED reviews do not count as actionable."""
    reviews = [
        {"user": {"login": "alice"}, "state": "COMMENTED"},
        {"user": {"login": "bob"}, "state": "COMMENTED"},
    ]
    assert compute_review_decision(reviews) is None


def test_compute_review_decision_ignores_dismissed():
    """DISMISSED reviews do not count as actionable."""
    reviews = [
        {"user": {"login": "alice"}, "state": "DISMISSED"},
    ]
    assert compute_review_decision(reviews) is None


def test_compute_review_decision_dismissed_clears_prior_approval():
    """A reviewer's APPROVED followed by DISMISSED yields no decision."""
    reviews = [
        {"user": {"login": "alice"}, "state": "APPROVED"},
        {"user": {"login": "alice"}, "state": "DISMISSED"},
    ]
    assert compute_review_decision(reviews) is None


def test_compute_review_decision_dismissed_clears_prior_changes_requested():
    """Dismissing a CHANGES_REQUESTED unblocks the aggregate when others approved."""
    reviews = [
        {"user": {"login": "alice"}, "state": "APPROVED"},
        {"user": {"login": "bob"}, "state": "CHANGES_REQUESTED"},
        {"user": {"login": "bob"}, "state": "DISMISSED"},
    ]
    assert compute_review_decision(reviews) == "APPROVED"


def test_compute_review_decision_dismissed_then_re_review():
    """A dismissed review followed by a new actionable one for the same reviewer uses the new one."""
    reviews = [
        {"user": {"login": "alice"}, "state": "APPROVED"},
        {"user": {"login": "alice"}, "state": "DISMISSED"},
        {"user": {"login": "alice"}, "state": "CHANGES_REQUESTED"},
    ]
    assert compute_review_decision(reviews) == "CHANGES_REQUESTED"


def test_compute_review_decision_mixed_actionable_and_commented():
    """Only APPROVED/CHANGES_REQUESTED are actionable; COMMENTED is ignored."""
    reviews = [
        {"user": {"login": "alice"}, "state": "COMMENTED"},
        {"user": {"login": "bob"}, "state": "APPROVED"},
    ]
    assert compute_review_decision(reviews) == "APPROVED"


def test_compute_review_decision_reviewer_changes_then_approves_among_others():
    """Complex scenario: one reviewer flip-flops, another approves."""
    reviews = [
        {"user": {"login": "alice"}, "state": "CHANGES_REQUESTED"},
        {"user": {"login": "bob"}, "state": "APPROVED"},
        {"user": {"login": "alice"}, "state": "APPROVED"},
    ]
    assert compute_review_decision(reviews) == "APPROVED"


def test_compute_review_decision_deleted_user():
    """Reviews with no user are safely skipped."""
    reviews = [
        {"user": None, "state": "APPROVED"},
        {"user": {"login": "bob"}, "state": "APPROVED"},
    ]
    assert compute_review_decision(reviews) == "APPROVED"


# ---------------------------------------------------------------------------
# close
# ---------------------------------------------------------------------------


async def test_close_calls_aclose():
    client = _make_client()
    with patch.object(client._client, "aclose", new=AsyncMock()) as mock_close:
        await client.close()
    mock_close.assert_called_once()
