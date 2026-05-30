from auto_assign.models import AssignmentCandidate, AssignmentStatus, Lane, RouterSnapshot
from auto_assign.scorer import AssignmentScorer
from auto_assign.settings import Settings


def test_local_only_task_blocks_free_api_lane():
    scorer = AssignmentScorer(Settings())
    candidate = AssignmentCandidate(
        task_id="ASS-local",
        privacy_labels=["local_only"],
        allowed_lanes=[Lane.FREE_API, Lane.LOCAL_ONLY],
    )
    router = RouterSnapshot(reachable=True, context_revision="rev-1")

    decision = scorer.score(candidate, router)

    assert decision.selected_lane == Lane.LOCAL_ONLY
    assert any(reason.reason_code == "privacy_denied" for reason in decision.skipped_lanes)
    assert decision.cache_only is True


def test_approval_required_blocks_dispatch_recommendation():
    scorer = AssignmentScorer(Settings())
    candidate = AssignmentCandidate(
        task_id="ASS-approval",
        approval_required=True,
        allowed_lanes=[Lane.PAPERCLIP, Lane.ROUTER_MODEL],
    )
    router = RouterSnapshot(reachable=True)

    decision = scorer.score(candidate, router)

    assert decision.status == AssignmentStatus.APPROVAL_REQUIRED
    assert decision.selected_lane == Lane.BLOCKED
    assert decision.approval_required is True


def test_router_unavailable_blocks_router_and_cloud_but_keeps_paperclip():
    scorer = AssignmentScorer(Settings())
    candidate = AssignmentCandidate(
        task_id="ASS-router-down",
        allowed_lanes=[Lane.ROUTER_MODEL, Lane.FREE_API, Lane.PAPERCLIP],
    )
    router = RouterSnapshot(reachable=False)

    decision = scorer.score(candidate, router)

    assert decision.selected_lane == Lane.PAPERCLIP
    assert {reason.reason_code for reason in decision.skipped_lanes} >= {"router_unavailable"}


def test_neo4j_terminal_state_wins_over_local_candidate():
    scorer = AssignmentScorer(Settings())
    candidate = AssignmentCandidate(task_id="ASS-done", allowed_lanes=[Lane.PAPERCLIP])
    router = RouterSnapshot(reachable=True)

    decision = scorer.score(candidate, router, canonical_status="done")

    assert decision.status == AssignmentStatus.BLOCKED
    assert decision.selected_lane == Lane.BLOCKED
    assert decision.canonical_status == "done"
    assert "AssistX/Neo4j reports terminal state" in decision.reasons[0]
