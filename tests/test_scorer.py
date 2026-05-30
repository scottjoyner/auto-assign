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
    assert any(reason.reason_code in {"privacy_denied", "privacy_cloud_denied"} for reason in decision.skipped_lanes)
    assert decision.cache_only is True


def test_explicit_local_only_blocks_router_model_and_free_api():
    scorer = AssignmentScorer(Settings())
    candidate = AssignmentCandidate(
        task_id="ASS-explicit-local",
        local_only=True,
        allow_cloud=False,
        allowed_lanes=[Lane.ROUTER_MODEL, Lane.FREE_API, Lane.LOCAL_ONLY],
    )
    router = RouterSnapshot(reachable=True, context_revision="rev-1")

    decision = scorer.score(candidate, router)

    assert decision.selected_lane == Lane.LOCAL_ONLY
    assert {reason.reason_code for reason in decision.skipped_lanes} >= {
        "router_cloud_denied",
        "privacy_cloud_denied",
    }


def test_sensitive_task_blocks_hosted_free_api():
    scorer = AssignmentScorer(Settings())
    candidate = AssignmentCandidate(
        task_id="ASS-sensitive",
        sensitive=True,
        allowed_lanes=[Lane.FREE_API, Lane.PAPERCLIP],
    )
    router = RouterSnapshot(reachable=True, context_revision="rev-1")

    decision = scorer.score(candidate, router)

    assert decision.selected_lane == Lane.PAPERCLIP
    assert any(reason.reason_code == "privacy_cloud_denied" for reason in decision.skipped_lanes)


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


def test_neo4j_active_assignment_blocks_duplicate():
    scorer = AssignmentScorer(Settings())
    candidate = AssignmentCandidate(task_id="ASS-running", allowed_lanes=[Lane.PAPERCLIP])
    router = RouterSnapshot(reachable=True)

    decision = scorer.score(candidate, router, canonical_status="running")

    assert decision.status == AssignmentStatus.BLOCKED
    assert decision.selected_lane == Lane.BLOCKED
    assert any(reason.reason_code == "duplicate_active_assignment" for reason in decision.skipped_lanes)


def test_assignment_and_decision_ids_are_deterministic_for_same_inputs():
    scorer = AssignmentScorer(Settings())
    candidate = AssignmentCandidate(task_id="ASS-deterministic", allowed_lanes=[Lane.PAPERCLIP])
    router = RouterSnapshot(reachable=True, context_revision="rev-stable")

    first = scorer.score(candidate, router)
    second = scorer.score(candidate, router)

    assert first.assignment_id == second.assignment_id
    assert first.decision_id == second.decision_id
    assert first.idempotency_key == second.idempotency_key


def test_decision_id_changes_when_router_context_revision_changes():
    scorer = AssignmentScorer(Settings())
    candidate = AssignmentCandidate(task_id="ASS-context", allowed_lanes=[Lane.PAPERCLIP])

    first = scorer.score(candidate, RouterSnapshot(reachable=True, context_revision="rev-a"))
    second = scorer.score(candidate, RouterSnapshot(reachable=True, context_revision="rev-b"))

    assert first.assignment_id == second.assignment_id
    assert first.decision_id != second.decision_id
    assert first.idempotency_key == second.idempotency_key


def test_decision_idempotency_key_is_status_neutral_for_recommended_decision():
    scorer = AssignmentScorer(Settings())
    candidate = AssignmentCandidate(task_id="ASS-key", allowed_lanes=[Lane.PAPERCLIP])

    decision = scorer.score(candidate, RouterSnapshot(reachable=True))

    assert decision.status == AssignmentStatus.RECOMMENDED
    assert decision.idempotency_key == "assign.assignment.decision:ASS-key:ready:unknown"


def test_decision_idempotency_key_is_status_neutral_for_blocked_decision():
    scorer = AssignmentScorer(Settings())
    candidate = AssignmentCandidate(task_id="ASS-block-key", allowed_lanes=[Lane.FREE_API])

    decision = scorer.score(candidate, RouterSnapshot(reachable=False))

    assert decision.status == AssignmentStatus.BLOCKED
    assert decision.idempotency_key == "assign.assignment.decision:ASS-block-key:ready:unknown"
    assert "recommended" not in decision.idempotency_key
