from lotsa.status import ALL_STATUSES, TaskStatus


def test_all_statuses_present():
    # The model is now nine-valued: ``awaiting_operator`` is the parked
    # "awaiting you" state (ADR-043) for the operator mark-complete escape hatch,
    # alongside ``archived`` (the operator Archive action).
    assert set(ALL_STATUSES) == {
        "working",
        "waiting",
        "waiting_for_pr",
        "awaiting_operator",
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
    assert TaskStatus.AWAITING_OPERATOR == "awaiting_operator"
    assert TaskStatus.NEEDS_INPUT == "needs_input"
    assert TaskStatus.BLOCKED == "blocked"
    assert TaskStatus.COMPLETE == "complete"
    assert TaskStatus.ABANDONED == "abandoned"
    assert TaskStatus.ARCHIVED == "archived"
