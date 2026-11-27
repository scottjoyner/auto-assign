"""Sensitive work must never be recommended to the cloud router_model lane.

Hermetic: scorer/models/settings only. No DB, socket, HTTP, credential, service,
Redis, Neo4j or live outbox is touched.
"""

import pytest

from auto_assign.models import AssignmentCandidate, AssignmentStatus, Lane, RouterSnapshot
from auto_assign.scorer import AssignmentScorer
from auto_assign.settings import Settings


def _router() -> RouterSnapshot:
    return RouterSnapshot(reachable=True, context_revision="rev-sensitive")


def _score(candidate: AssignmentCandidate, lanes: list[Lane]):
    return AssignmentScorer(Settings()).score(candidate, _router(), candidate_lanes=lanes)


def test_sensitive_flag_blocks_cloud_router_model() -> None:
    candidate = AssignmentCandidate(
        task_id="ASS-sensitive-flag",
        sensitive=True,
        allow_cloud=True,
        allowed_lanes=[Lane.ROUTER_MODEL],
    )

    decision = _score(candidate, [Lane.ROUTER_MODEL])

    assert decision.status == AssignmentStatus.BLOCKED
    assert decision.selected_lane == Lane.BLOCKED
    assert "router_cloud_denied" in [r.reason_code for r in decision.skipped_lanes]


@pytest.mark.parametrize(
    "privacy,labels",
    [
        ("voice_auth", []),
        ("enrollment_sample", []),
        (None, ["enrollment"]),
    ],
)
def test_sensitive_privacy_label_blocks_cloud_router_model(privacy, labels) -> None:
    candidate = AssignmentCandidate(
        task_id="ASS-sensitive-label",
        privacy=privacy,
        privacy_labels=labels,
        allow_cloud=True,
        allowed_lanes=[Lane.ROUTER_MODEL],
    )

    decision = _score(candidate, [Lane.ROUTER_MODEL])

    assert decision.status == AssignmentStatus.BLOCKED
    assert decision.selected_lane == Lane.BLOCKED
    assert "router_cloud_denied" in [r.reason_code for r in decision.skipped_lanes]


def test_sensitive_task_with_local_lane_prefers_local_only() -> None:
    candidate = AssignmentCandidate(
        task_id="ASS-sensitive-both",
        sensitive=True,
        allow_cloud=True,
        allowed_lanes=[Lane.ROUTER_MODEL, Lane.LOCAL_ONLY],
    )

    decision = _score(candidate, [Lane.ROUTER_MODEL, Lane.LOCAL_ONLY])

    assert decision.status == AssignmentStatus.RECOMMENDED
    assert decision.selected_lane == Lane.LOCAL_ONLY
    assert "router_cloud_denied" in [r.reason_code for r in decision.skipped_lanes]


def test_nonsensitive_task_can_still_use_router_model() -> None:
    candidate = AssignmentCandidate(
        task_id="ASS-plain",
        allow_cloud=True,
        allowed_lanes=[Lane.ROUTER_MODEL],
    )

    decision = _score(candidate, [Lane.ROUTER_MODEL])

    assert decision.status == AssignmentStatus.RECOMMENDED
    assert decision.selected_lane == Lane.ROUTER_MODEL


def test_sensitive_free_api_still_blocked() -> None:
    candidate = AssignmentCandidate(
        task_id="ASS-sensitive-free",
        sensitive=True,
        allow_cloud=True,
        allowed_lanes=[Lane.FREE_API],
    )

    decision = _score(candidate, [Lane.FREE_API])

    assert decision.status == AssignmentStatus.BLOCKED
    assert "privacy_cloud_denied" in [r.reason_code for r in decision.skipped_lanes]


def test_cloud_disallowed_still_blocks_router_model() -> None:
    candidate = AssignmentCandidate(
        task_id="ASS-no-cloud",
        allow_cloud=False,
        allowed_lanes=[Lane.ROUTER_MODEL],
    )

    decision = _score(candidate, [Lane.ROUTER_MODEL])

    assert decision.status == AssignmentStatus.BLOCKED
    assert "router_cloud_denied" in [r.reason_code for r in decision.skipped_lanes]
