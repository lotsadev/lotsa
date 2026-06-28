from lotsa.status import ALL_STATUSES, TaskStatus


def test_all_statuses_present():
    # The model is now eight-valued: ``archived`` is the new terminal status
    # added for the operator Archive action (worktree torn down, DB log kept).
    assert set(ALL_STATUSES) == {
        "working",
        "waiting",
        "waiting_for_pr",
        "needs_input",
        "blocked",
        "complete",
        "abandoned",
        "archived",
    }


def test_taskstatus_constants_match():
    assert TaskStatus.WORKING == "working"
    assert TaskStatus.WAITING == "waiting"
    assert TaskStatus.WAITING_FOR_PR == "waiting_for_pr"
    assert TaskStatus.NEEDS_INPUT == "needs_input"
    assert TaskStatus.BLOCKED == "blocked"
    assert TaskStatus.COMPLETE == "complete"
    assert TaskStatus.ABANDONED == "abandoned"
    assert TaskStatus.ARCHIVED == "archived"
