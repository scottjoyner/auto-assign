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

    TASK_CANDIDATE_CREATED = "task.candidate.created"
    ROUTER_QUOTA_SNAPSHOT_RECORDED = "router.quota_snapshot.recorded"
    ROUTER_SERVICE_SNAPSHOT_RECORDED = "router.service_snapshot.recorded"


ROUTER_SNAPSHOT_EVENTS = {
    EventType.ROUTER_QUOTA_SNAPSHOT_RECORDED,
    EventType.ROUTER_SERVICE_SNAPSHOT_RECORDED,
}
