"""Hermetic regression for the router quota-preserve free-API fence (sweep22x).

Pure in-process pydantic objects only. No SQLite, Redis, Neo4j, HTTP, socket,
credential, service, timer, backup or live outbox is read or written.
"""

from __future__ import annotations

import pytest

from auto_assign.models import AssignmentCandidate, Lane, RouterSnapshot
from auto_assign.scorer import AssignmentScorer
from auto_assign.settings import Settings

# Make FREE_API the highest-scoring eligible lane so that a missing
# quota-preserve fence is observable as a real RECOMMENDED cloud decision.
_FREE_API_WINS = "free_api=0.99,local_only=0.10,router_model=0.10,direct_worker=0.10"


def _scorer() -> AssignmentScorer:
    return AssignmentScorer(Settings(lane_base_scores=_FREE_API_WINS))


@pytest.mark.parametrize(
    "spelling",
    [
        "preserve",
        "PRESERVE",
        "Preserve",
        "pReSeRvE",
        " preserve ",
        "\tpreserve\n",
    ],
)
def test_quota_preserve_mode_is_detected_regardless_of_case_or_padding(spelling: str) -> None:
    router = RouterSnapshot(reachable=True, quota={"metadata": {"mode": spelling}})
    assert _scorer()._quota_preserve_mode(router) is True


@pytest.mark.parametrize(
    "quota",
    [
        {"metadata": {"mode": "PRESERVE"}},
        {"metadata": {"mode": "Preserve"}},
        {"metadata": {"mode": " preserve "}},
        {"mode": "PRESERVE"},
    ],
)
def test_free_api_lane_is_skipped_for_noncanonical_preserve_spellings(quota: dict) -> None:
    router = RouterSnapshot(reachable=True, quota=quota)
    candidate = AssignmentCandidate(
        task_id="t-quota-preserve",
        allowed_lanes=[Lane.FREE_API, Lane.LOCAL_ONLY],
        allow_cloud=True,
        sensitive=False,
        local_only=False,
    )
    decision = _scorer().score(candidate, router)
    assert decision.selected_lane is not Lane.FREE_API
    assert any(skip.reason_code == "quota_preserve_mode" for skip in decision.skipped_lanes)


def test_lowercase_preserve_still_skips_free_api() -> None:
    router = RouterSnapshot(reachable=True, quota={"metadata": {"mode": "preserve"}})
    candidate = AssignmentCandidate(
        task_id="t-quota-lowercase",
        allowed_lanes=[Lane.FREE_API, Lane.LOCAL_ONLY],
    )
    decision = _scorer().score(candidate, router)
    assert decision.selected_lane is Lane.LOCAL_ONLY
    assert any(skip.reason_code == "quota_preserve_mode" for skip in decision.skipped_lanes)


@pytest.mark.parametrize("quota", [{}, {"metadata": {}}, {"metadata": {"mode": "balanced"}}, {"mode": "normal"}])
def test_non_preserve_quota_does_not_block_free_api(quota: dict) -> None:
    router = RouterSnapshot(reachable=True, quota=quota)
    candidate = AssignmentCandidate(
        task_id="t-quota-open",
        allowed_lanes=[Lane.FREE_API, Lane.LOCAL_ONLY],
    )
    decision = _scorer().score(candidate, router)
    assert decision.selected_lane is Lane.FREE_API
