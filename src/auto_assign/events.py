from __future__ import annotations

from enum import StrEnum


class EventType(StrEnum):
    ASSIGNMENT_DECISION = "assign.assignment.decision"
    ASSIGNMENT_RECOMMENDED = "assign.assignment.recommended"
    ASSIGNMENT_APPROVAL_REQUIRED = "assign.assignment.approval_required"
    ASSIGNMENT_SKIPPED = "assign.assignment.skipped"
    ASSIGNMENT_APPROVED = "assign.assignment.approved"
    ASSIGNMENT_RELEASED = "assign.assignment.released"
    WORKER_HEARTBEAT_RECORDED = "assign.worker.heartbeat.recorded"

    # Orchestration plan lifecycle events (Section 2.1)
    ASSIGNMENT_REQUESTED = "assignment.requested"
    ASSIGNMENT_CLAIMED = "assignment.claimed"
    ASSIGNMENT_HEARTBEAT = "assignment.heartbeat"
    ASSIGNMENT_COMPLETED = "assignment.completed"
    ASSIGNMENT_FAILED = "assignment.failed"
    ASSIGNMENT_EXPIRED = "assignment.expired"
    ASSIGNMENT_RELEASED_CANONICAL = "assignment.released"

    TASK_CANDIDATE_CREATED = "task.candidate.created"
    ROUTER_QUOTA_SNAPSHOT_RECORDED = "router.quota_snapshot.recorded"
    ROUTER_SERVICE_SNAPSHOT_RECORDED = "router.service_snapshot.recorded"


ROUTER_SNAPSHOT_EVENTS = {
    EventType.ROUTER_QUOTA_SNAPSHOT_RECORDED,
    EventType.ROUTER_SERVICE_SNAPSHOT_RECORDED,
}
