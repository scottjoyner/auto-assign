from __future__ import annotations

from datetime import UTC, datetime
from enum import StrEnum
from typing import Any
from uuid import uuid4

from pydantic import BaseModel, ConfigDict, Field


def utc_now() -> datetime:
    return datetime.now(UTC)


class AssignmentStatus(StrEnum):
    RECOMMENDED = "recommended"
    APPROVAL_REQUIRED = "approval_required"
    RESERVED = "reserved"
    DISPATCHED = "dispatched"
    RUNNING = "running"
    DONE = "done"
    FAILED = "failed"
    BLOCKED = "blocked"
    RELEASED = "released"


class AssignmentControlMode(StrEnum):
    ENABLED = "enabled"
    PAUSED = "paused"
    DRAINING = "draining"
    MAINTENANCE = "maintenance"


class Lane(StrEnum):
    PAPERCLIP = "paperclip"
    ROUTER_MODEL = "router_model"
    LOCAL_ONLY = "local_only"
    FREE_API = "free_api"
    DIRECT_WORKER = "direct_worker"
    BLOCKED = "blocked"


class EventEnvelope(BaseModel):
    event_id: str = Field(default_factory=lambda: f"evt_{uuid4().hex}")
    event_type: str
    source_service: str = "auto-assign"
    occurred_at: datetime = Field(default_factory=utc_now)
    idempotency_key: str
    schema_version: str = "assign.v1"
    subject: str
    payload: dict[str, Any] = Field(default_factory=dict)
    privacy: list[str] = Field(default_factory=list)


class AssignmentCandidate(BaseModel):
    model_config = ConfigDict(extra="ignore")

    task_id: str
    title: str | None = None
    prompt: str | None = None
    status: str = "ready"
    priority: str = "background"
    queue: str = "backlog"
    risk_level: str = "low"
    approval_required: bool = False
    privacy: str | None = None
    privacy_labels: list[str] = Field(default_factory=list)
    local_only: bool = False
    sensitive: bool = False
    allow_cloud: bool = True
    required_capabilities: list[str] = Field(default_factory=list)
    allowed_lanes: list[Lane] = Field(default_factory=lambda: [Lane.PAPERCLIP, Lane.ROUTER_MODEL])
    retry_count: int = 0
    created_at: datetime = Field(default_factory=utc_now)
    summary: str | None = None
    metadata: dict[str, Any] = Field(default_factory=dict)


class RouterSnapshot(BaseModel):
    reachable: bool = False
    context_revision: str | None = None
    nodes: list[dict[str, Any]] = Field(default_factory=list)
    providers: list[dict[str, Any]] = Field(default_factory=list)
    services: list[dict[str, Any]] = Field(default_factory=list)
    quota: dict[str, Any] = Field(default_factory=dict)
    circuits: dict[str, Any] = Field(default_factory=dict)
    agent_clis: list[dict[str, Any]] = Field(default_factory=list)
    ops_summary: dict[str, Any] = Field(default_factory=dict)


class SkipReason(BaseModel):
    lane: Lane
    reason_code: str
    reason: str


class AssignmentDecision(BaseModel):
    assignment_id: str
    task_id: str
    decision_id: str
    status: AssignmentStatus
    selected_lane: Lane
    selected_target: str | None = None
    score: float = 0.0
    approval_required: bool = False
    lease_expires_at: datetime | None = None
    reasons: list[str] = Field(default_factory=list)
    skipped_lanes: list[SkipReason] = Field(default_factory=list)
    context_revision: str | None = None
    idempotency_key: str
    canonical_status: str | None = None
    cache_only: bool = True
    created_at: datetime = Field(default_factory=utc_now)


class SchedulerTickRequest(BaseModel):
    dry_run: bool = True
    limit: int = Field(default=25, ge=1, le=500)
    reason: str = "manual_operator_tick"
    include_blocked: bool = False
    task_ids: list[str] = Field(default_factory=list)


class SchedulerTickResponse(BaseModel):
    scheduler_run_id: str
    dry_run: bool
    evaluated: int
    recommended: int
    approval_required: int
    skipped: int
    released_expired: int
    decisions: list[AssignmentDecision]


class AssignmentEvaluateRequest(BaseModel):
    task_id: str
    dry_run: bool = True
    force_refresh_context: bool = True
    candidate_lanes: list[Lane] = Field(default_factory=lambda: [Lane.PAPERCLIP, Lane.ROUTER_MODEL, Lane.LOCAL_ONLY, Lane.FREE_API])


class AssignmentApprovalRequest(BaseModel):
    approved_by: str = "operator"
    approval_reason: str = "operator approved dry-run assignment"
    expires_in_seconds: int = Field(default=900, ge=1, le=86_400)
    dry_run: bool = True


class AssignmentReleaseRequest(BaseModel):
    reason: str = "operator_release"
    retryable: bool = True
    dry_run: bool = True


class AssignmentControlRequest(BaseModel):
    mode: AssignmentControlMode
    reason: str | None = None
    updated_by: str = "operator"
    metadata: dict[str, Any] = Field(default_factory=dict)
    dry_run: bool = True


class HeartbeatRequest(BaseModel):
    node_id: str
    worker_id: str | None = None
    assignment_id: str | None = None
    status: str = "online"
    capabilities: list[str] = Field(default_factory=list)
    services: list[dict[str, Any]] = Field(default_factory=list)
    metrics: dict[str, Any] = Field(default_factory=dict)


class HealthResponse(BaseModel):
    status: str
    service: str = "auto-assign"
    version: str
    assistx: dict[str, Any]
    router: dict[str, Any]
    cache: dict[str, Any]
    scheduler: dict[str, Any]
