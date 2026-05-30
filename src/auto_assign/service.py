from __future__ import annotations

from uuid import uuid4

from .cache import CacheStore
from .clients import AssistXClient, RouterClient
from .models import (
    AssignmentCandidate,
    AssignmentDecision,
    AssignmentEvaluateRequest,
    EventEnvelope,
    HeartbeatRequest,
    Lane,
    SchedulerTickRequest,
    SchedulerTickResponse,
)
from .scorer import AssignmentScorer
from .settings import Settings


class AssignmentService:
    def __init__(
        self,
        settings: Settings,
        cache: CacheStore,
        assistx: AssistXClient,
        router: RouterClient,
        scorer: AssignmentScorer,
    ):
        self.settings = settings
        self.cache = cache
        self.assistx = assistx
        self.router = router
        self.scorer = scorer
        self.last_tick_at: str | None = None

    async def evaluate(self, request: AssignmentEvaluateRequest) -> AssignmentDecision:
        candidate = await self.assistx.get_task(request.task_id)
        if candidate is None:
            candidate = AssignmentCandidate(task_id=request.task_id, allowed_lanes=request.candidate_lanes)
        else:
            candidate.allowed_lanes = request.candidate_lanes

        router_snapshot = await self.router.snapshot()
        status = await self.assistx.event_status(
            f"assign.assignment.recommended:{candidate.task_id}:{candidate.status}:unknown"
        )
        canonical_status = status.get("canonical_status") if status.get("known") else None
        decision = self.scorer.score(candidate, router_snapshot, request.candidate_lanes, canonical_status)
        self.cache.upsert_assignment(decision)
        self.cache.enqueue_event(self._decision_event(decision, dry_run=request.dry_run))
        return decision

    async def scheduler_tick(self, request: SchedulerTickRequest) -> SchedulerTickResponse:
        candidates = []
        if request.task_ids:
            candidates = [AssignmentCandidate(task_id=task_id) for task_id in request.task_ids]
        else:
            candidates = await self.assistx.get_backlog_candidates(limit=request.limit)

        router_snapshot = await self.router.snapshot()
        decisions: list[AssignmentDecision] = []
        for candidate in candidates[: request.limit]:
            status = await self.assistx.event_status(
                f"assign.assignment.recommended:{candidate.task_id}:{candidate.status}:unknown"
            )
            canonical_status = status.get("canonical_status") if status.get("known") else None
            decision = self.scorer.score(candidate, router_snapshot, canonical_status=canonical_status)
            decisions.append(decision)
            self.cache.upsert_assignment(decision)
            self.cache.enqueue_event(self._decision_event(decision, dry_run=request.dry_run))

        self.last_tick_at = decisions[-1].created_at.isoformat() if decisions else None
        return SchedulerTickResponse(
            scheduler_run_id=f"tick_{uuid4().hex[:12]}",
            dry_run=request.dry_run,
            evaluated=len(candidates[: request.limit]),
            recommended=sum(1 for d in decisions if d.status == "recommended"),
            approval_required=sum(1 for d in decisions if d.status == "approval_required"),
            skipped=sum(1 for d in decisions if d.status == "blocked"),
            released_expired=0,
            decisions=decisions,
        )

    def record_heartbeat(self, heartbeat: HeartbeatRequest) -> EventEnvelope:
        heartbeat_id = f"heartbeat_{uuid4().hex}"
        self.cache.record_heartbeat(heartbeat_id, heartbeat)
        event = EventEnvelope(
            event_type="assign.worker.heartbeat.recorded",
            idempotency_key=f"assign.worker.heartbeat.recorded:{heartbeat.node_id}:{heartbeat.worker_id}:{heartbeat.assignment_id}:{heartbeat_id}",
            subject=heartbeat.assignment_id or heartbeat.worker_id or heartbeat.node_id,
            payload={"heartbeat_id": heartbeat_id, **heartbeat.model_dump(mode="json")},
        )
        self.cache.enqueue_event(event)
        return event

    def list_assignments(self, limit: int = 50) -> list[dict]:
        return self.cache.list_assignments(limit=limit)

    def get_assignment(self, assignment_id: str) -> dict | None:
        return self.cache.get_assignment(assignment_id)

    def _decision_event(self, decision: AssignmentDecision, dry_run: bool) -> EventEnvelope:
        event_type = "assign.assignment.skipped" if decision.selected_lane == Lane.BLOCKED else "assign.assignment.recommended"
        return EventEnvelope(
            event_type=event_type,
            idempotency_key=decision.idempotency_key,
            subject=decision.task_id,
            payload={**decision.model_dump(mode="json"), "dry_run": dry_run, "cache_role": "outbox_replay_buffer"},
            privacy=[],
        )
