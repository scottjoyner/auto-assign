"""Hermetic regression: AssistX canonical FAILED is a terminal task state.

`docs/STATE_MACHINE_MAPPING.md` (source of truth: AssistX Neo4j) maps canonical
DONE, FAILED and CANCELLED to local DONE/FAILED/RELEASED. `AssignmentScorer.score`
must block recommendations whenever AssistX reports any of those terminal
states; the local cache is only a mirror and AssistX wins.

Pure in-process test: no network, DB, Redis, Neo4j, filesystem or live state.
"""

from __future__ import annotations

import pytest

from auto_assign.models import AssignmentCandidate, AssignmentStatus, Lane, RouterSnapshot
from auto_assign.scorer import AssignmentScorer
from auto_assign.settings import Settings

TERMINAL_CANONICAL = [
    "done",
    "DONE",
    "complete",
    "completed",
    "cancelled",
    "CANCELLED",
    "terminal",
    "failed_terminal",
    "failed",
    "FAILED",
]

NON_TERMINAL_CANONICAL = ["review", "ready"]
ACTIVE_CANONICAL = ["running", "claimed", "reserved", "dispatched", "claimed_passive"]


def _scorer() -> AssignmentScorer:
    return AssignmentScorer(Settings())


def _candidate() -> AssignmentCandidate:
    # A plain low-risk task with one eligible local lane: on a non-terminal
    # canonical status the scorer would normally RECOMMEND it.
    return AssignmentCandidate(
        task_id="ASS-sweep21x-failed-terminal",
        allowed_lanes=[Lane.LOCAL_ONLY],
    )


def _router() -> RouterSnapshot:
    return RouterSnapshot(reachable=True, context_revision="sweep21x")


@pytest.mark.parametrize("canonical_status", TERMINAL_CANONICAL)
def test_canonical_terminal_status_is_blocked(canonical_status: str) -> None:
    decision = _scorer().score(_candidate(), _router(), canonical_status=canonical_status)

    assert decision.status is AssignmentStatus.BLOCKED
    assert decision.selected_lane is Lane.BLOCKED
    assert decision.canonical_status == canonical_status
    assert any("terminal state" in reason for reason in decision.reasons)
    assert decision.skipped_lanes
    assert {skip.reason_code for skip in decision.skipped_lanes} == {"canonical_terminal_state"}


def test_canonical_failed_is_blocked_for_local_only_lane() -> None:
    candidate = AssignmentCandidate(
        task_id="ASS-sweep21x-failed-local-only",
        local_only=True,
        allow_cloud=False,
        allowed_lanes=[Lane.LOCAL_ONLY],
    )

    decision = _scorer().score(candidate, _router(), canonical_status="failed")

    assert decision.status is AssignmentStatus.BLOCKED
    assert decision.selected_lane is Lane.BLOCKED
    assert "AssistX/Neo4j reports terminal state" in decision.reasons[0]


@pytest.mark.parametrize("canonical_status", NON_TERMINAL_CANONICAL)
def test_non_terminal_status_still_scores_normally(canonical_status: str) -> None:
    # Control: the fix must not widen the terminal set to every canonical state.
    decision = _scorer().score(_candidate(), _router(), canonical_status=canonical_status)

    assert decision.status is AssignmentStatus.RECOMMENDED
    assert decision.selected_lane is Lane.LOCAL_ONLY
    assert decision.canonical_status == canonical_status


@pytest.mark.parametrize("canonical_status", ACTIVE_CANONICAL)
def test_active_assignment_status_still_blocks_duplicate(canonical_status: str) -> None:
    decision = _scorer().score(_candidate(), _router(), canonical_status=canonical_status)

    assert decision.status is AssignmentStatus.BLOCKED
    assert any(skip.reason_code == "duplicate_active_assignment" for skip in decision.skipped_lanes)


def test_missing_canonical_status_still_scores_normally() -> None:
    decision = _scorer().score(_candidate(), _router())

    assert decision.status is AssignmentStatus.RECOMMENDED
    assert decision.selected_lane is Lane.LOCAL_ONLY
