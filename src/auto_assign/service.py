from __future__ import annotations

import json
from uuid import uuid4

from .cache import CacheStore
from .clients import AssistXClient, RouterClient
from .control import AssignmentControlStore
from .events import EventType, ROUTER_SNAPSHOT_EVENTS
from .models import (
    AssignmentApprovalRequest,
    AssignmentCandidate,
    AssignmentClaimRequest,
    AssignmentCompletionRequest,
    AssignmentDecision,
    AssignmentEvaluateRequest,
    AssignmentReleaseRequest,
    AssignmentStatus,
    EventEnvelope,
    HeartbeatRequest,
    Lane,
    RouterSnapshot,
    SchedulerTickRequest,
    SchedulerTickResponse,
    utc_now,
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

    def _control(self) -> dict:
        return AssignmentControlStore(self.settings.sqlite_path).get()

    def _assignment_control_blocks_new_work(self) -> dict | None:
        control = self._control()
        if control.get("new_assignments_allowed"):
            return None
        return control

    def _control_blocked_decision(
        self,
        candidate: AssignmentCandidate,
        control: dict,
        dry_run: bool,
    ) -> AssignmentDecision:
        reason_code = f"assignment_control_{control.get('mode', 'paused')}"
        reason = f"assignment control mode is {control.get('mode')}; new assignment decisions are paused"
        decision = self.scorer.blocked(
            candidate,
            RouterSnapshot(reachable=False, context_revision="assignment-control"),
            canonical_status=control.get("mode"),
            reason_code=reason_code,
            reason=reason,
        )
        self.cache.upsert_assignment(decision)
        self.cache.enqueue_event(self._decision_event(decision, dry_run=dry_run))
        return decision

    async def _inbound_event_was_processed(self, idempotency_key: str) -> bool:
        async with await self.cache.aconnect() as db:
            cursor = await db.execute(
                "SELECT status FROM inbound_event_processing WHERE idempotency_key = ?",
                (idempotency_key,),
            )
            row = await cursor.fetchone()
        return row is not None

    async def _mark_inbound_event_processed(self, event: dict, action: dict) -> None:
        now = utc_now().isoformat()
        status = action.get("action", "processed")
        async with await self.cache.aconnect() as db:
            await db.execute(
                """
                INSERT INTO inbound_event_processing (
                    idempotency_key, event_id, event_type, status, action_json,
                    processed_at, updated_at
                ) VALUES (?, ?, ?, ?, ?, ?, ?)
                ON CONFLICT(idempotency_key) DO UPDATE SET
                    event_id=excluded.event_id,
                    event_type=excluded.event_type,
                    status=excluded.status,
                    action_json=excluded.action_json,
                    processed_at=excluded.processed_at,
                    updated_at=excluded.updated_at
                """,
                (
                    event.get("idempotency_key"),
                    event.get("event_id"),
                    event.get("event_type"),
                    status,
                    json.dumps(action, sort_keys=True),
                    now,
                    now,
                ),
            )

    def list_inbound_processing(self, limit: int = 50) -> list[dict]:
        with self.cache.connect() as conn:
            rows = conn.execute(
                """
                SELECT idempotency_key, event_id, event_type, status, action_json,
                       processed_at, updated_at
                FROM inbound_event_processing
                ORDER BY processed_at DESC
                LIMIT ?
                """,
                (limit,),
            ).fetchall()
        return [{**dict(row), "action": json.loads(row["action_json"])} for row in rows]

    async def evaluate(self, request: AssignmentEvaluateRequest) -> AssignmentDecision:
        candidate = await self.assistx.get_task(request.task_id)
        if candidate is None:
            candidate = AssignmentCandidate(task_id=request.task_id, allowed_lanes=request.candidate_lanes)
        else:
            candidate.allowed_lanes = request.candidate_lanes

        blocked_control = self._assignment_control_blocks_new_work()
        if blocked_control:
            return self._control_blocked_decision(candidate, blocked_control, dry_run=request.dry_run)

        router_snapshot = await self.router.snapshot()
        status = await self.assistx.event_status(
            self.scorer.decision_idempotency_key(candidate.task_id, candidate.status, None)
        )
        canonical_status = status.get("canonical_status") if status.get("known") else None
        decision = self.scorer.score(candidate, router_snapshot, request.candidate_lanes, canonical_status)
        self.cache.upsert_assignment(decision)
        self.cache.enqueue_event(self._decision_event(decision, dry_run=request.dry_run))
        return decision

    async def scheduler_tick(self, request: SchedulerTickRequest) -> SchedulerTickResponse:
        blocked_control = self._assignment_control_blocks_new_work()
        if blocked_control:
            scheduler_run_id = f"tick_{uuid4().hex[:12]}"
            self.last_tick_at = utc_now().isoformat()
            self.cache.record_scheduler_run(
                scheduler_run_id=scheduler_run_id,
                trigger_reason=f"{request.reason}:blocked_by_assignment_control:{blocked_control.get('mode')}",
                dry_run=request.dry_run,
                evaluated_count=0,
                recommended_count=0,
                skipped_count=0,
                error_summary=f"assignment control mode {blocked_control.get('mode')} blocks scheduler ticks",
            )
            return SchedulerTickResponse(
                scheduler_run_id=scheduler_run_id,
                dry_run=request.dry_run,
                evaluated=0,
                recommended=0,
                approval_required=0,
                skipped=0,
                released_expired=0,
                decisions=[],
            )

        candidates = []
        if request.task_ids:
            candidates = [AssignmentCandidate(task_id=task_id) for task_id in request.task_ids]
        else:
            candidates = await self.assistx.get_backlog_candidates(limit=request.limit)

        router_snapshot = await self.router.snapshot()
        decisions: list[AssignmentDecision] = []
        for candidate in candidates[: request.limit]:
            status = await self.assistx.event_status(
                self.scorer.decision_idempotency_key(candidate.task_id, candidate.status, None)
            )
            canonical_status = status.get("canonical_status") if status.get("known") else None
            decision = self.scorer.score(candidate, router_snapshot, canonical_status=canonical_status)
            decisions.append(decision)
            self.cache.upsert_assignment(decision)
            self.cache.enqueue_event(self._decision_event(decision, dry_run=request.dry_run))

        scheduler_run_id = f"tick_{uuid4().hex[:12]}"
        self.last_tick_at = decisions[-1].created_at.isoformat() if decisions else None
        response = SchedulerTickResponse(
            scheduler_run_id=scheduler_run_id,
            dry_run=request.dry_run,
            evaluated=len(candidates[: request.limit]),
            recommended=sum(1 for d in decisions if d.status == AssignmentStatus.RECOMMENDED),
            approval_required=sum(1 for d in decisions if d.status == AssignmentStatus.APPROVAL_REQUIRED),
            skipped=sum(1 for d in decisions if d.status == AssignmentStatus.BLOCKED),
            released_expired=0,
            decisions=decisions,
        )
        self.cache.record_scheduler_run(
            scheduler_run_id=scheduler_run_id,
            trigger_reason=request.reason,
            dry_run=request.dry_run,
            evaluated_count=response.evaluated,
            recommended_count=response.recommended,
            skipped_count=response.skipped,
        )
        return response

    async def approve_assignment(self, assignment_id: str, request: AssignmentApprovalRequest) -> dict:
        assignment = self.cache.get_assignment(assignment_id)
        if assignment is None:
            return {
                "accepted": False,
                "reason": "assignment_not_found_in_local_cache",
                "canonical_source": "neo4j_via_assistx",
            }
        event = EventEnvelope(
            event_type=EventType.ASSIGNMENT_APPROVED,
            idempotency_key=f"{EventType.ASSIGNMENT_APPROVED}:{assignment_id}:{request.approved_by}:{request.approval_reason}",
            subject=assignment.get("task_id", assignment_id),
            payload={
                "assignment_id": assignment_id,
                "task_id": assignment.get("task_id"),
                "approved_by": request.approved_by,
                "approval_reason": request.approval_reason,
                "expires_in_seconds": request.expires_in_seconds,
                "dry_run": request.dry_run,
                "cache_role": "outbox_replay_buffer",
            },
        )
        self.cache.enqueue_event(event)
        return {
            "accepted": True,
            "event_id": event.event_id,
            "idempotency_key": event.idempotency_key,
            "dry_run": request.dry_run,
            "canonical_source": "neo4j_via_assistx",
            "cache_role": "outbox_replay_buffer",
        }

    def _assignment_event_context(self, assignment_id: str, assignment: dict, **overrides) -> dict:
        context = {
            "assignment_id": assignment_id,
            "task_id": assignment.get("task_id"),
            "route_id": assignment.get("route_id"),
            "worker_id": assignment.get("worker_id"),
            "node_id": assignment.get("node_id"),
            "status": assignment.get("status"),
            "lease_seconds": assignment.get("lease_seconds"),
            "lease_expires_at": assignment.get("lease_expires_at"),
        }
        for key, value in overrides.items():
            if value is not None:
                context[key] = value
        return {key: value for key, value in context.items() if value is not None}

    async def release_assignment(self, assignment_id: str, request: AssignmentReleaseRequest) -> dict:
        assignment = self.cache.get_assignment(assignment_id)
        if assignment is None:
            return {
                "accepted": False,
                "reason": "assignment_not_found_in_local_cache",
                "canonical_source": "neo4j_via_assistx",
            }
        event = EventEnvelope(
            event_type=EventType.ASSIGNMENT_RELEASED,
            idempotency_key=f"{EventType.ASSIGNMENT_RELEASED}:{assignment_id}:{request.reason}:{request.retryable}",
            subject=assignment.get("task_id", assignment_id),
            payload={
                **self._assignment_event_context(assignment_id, assignment),
                "reason": request.reason,
                "retryable": request.retryable,
                "dry_run": request.dry_run,
                "cache_role": "outbox_replay_buffer",
            },
        )
        self.cache.enqueue_event(event)
        return {
            "accepted": True,
            "event_id": event.event_id,
            "idempotency_key": event.idempotency_key,
            "dry_run": request.dry_run,
            "canonical_source": "neo4j_via_assistx",
            "cache_role": "outbox_replay_buffer",
        }

    def record_heartbeat(self, heartbeat: HeartbeatRequest) -> EventEnvelope:
        heartbeat_id = f"heartbeat_{uuid4().hex}"
        self.cache.record_heartbeat(heartbeat_id, heartbeat)
        event = EventEnvelope(
            event_type=EventType.WORKER_HEARTBEAT_RECORDED,
            idempotency_key=f"{EventType.WORKER_HEARTBEAT_RECORDED}:{heartbeat.node_id}:{heartbeat.worker_id}:{heartbeat.assignment_id}:{heartbeat_id}",
            subject=heartbeat.assignment_id or heartbeat.worker_id or heartbeat.node_id,
            payload={"heartbeat_id": heartbeat_id, **heartbeat.model_dump(mode="json")},
        )
        self.cache.enqueue_event(event)
        return event

    def ingest_event(self, event: EventEnvelope) -> dict:
        self.cache.record_inbound_event(event)
        return {
            "accepted": True,
            "event_id": event.event_id,
            "event_type": event.event_type,
            "idempotency_key": event.idempotency_key,
            "subject": event.subject,
            "source": "sqlite_inbound_event_cache",
            "canonical_source": "neo4j_via_assistx",
            "cache_role": "received_event_mirror",
        }

    async def process_inbound_events(
        self,
        event_type: str | None = None,
        dry_run: bool = True,
        limit: int = 25,
        include_processed: bool = False,
    ) -> dict:
        events = self.cache.list_inbound_events(limit=limit, event_type=event_type)
        actions: list[dict] = []
        skipped_already_processed = 0
        router_snapshot_event_types = {str(event_type) for event_type in ROUTER_SNAPSHOT_EVENTS}
        for event_row in events:
            event = event_row["payload"]
            if not include_processed and await self._inbound_event_was_processed(event["idempotency_key"]):
                skipped_already_processed += 1
                continue
            event_name = event.get("event_type")
            payload = event.get("payload", {}) or {}
            subject = event.get("subject")
            if event_name == EventType.TASK_CANDIDATE_CREATED:
                task_id = payload.get("task_id") or subject
                if task_id:
                    decision = await self.evaluate(
                        AssignmentEvaluateRequest(task_id=task_id, dry_run=dry_run)
                    )
                    action = {
                        "event_id": event.get("event_id"),
                        "event_type": event_name,
                        "action": "assignment_evaluated",
                        "task_id": task_id,
                        "assignment_id": decision.assignment_id,
                        "selected_lane": decision.selected_lane.value,
                        "dry_run": dry_run,
                    }
                else:
                    action = {
                        "event_id": event.get("event_id"),
                        "event_type": event_name,
                        "action": "skipped",
                        "reason": "missing_task_id",
                    }
            elif event_name in router_snapshot_event_types:
                tick = await self.scheduler_tick(
                    SchedulerTickRequest(
                        dry_run=dry_run,
                        limit=limit,
                        reason=f"inbound_event:{event_name}",
                    )
                )
                action = {
                    "event_id": event.get("event_id"),
                    "event_type": event_name,
                    "action": "scheduler_tick",
                    "scheduler_run_id": tick.scheduler_run_id,
                    "evaluated": tick.evaluated,
                    "dry_run": dry_run,
                }
            else:
                action = {
                    "event_id": event.get("event_id"),
                    "event_type": event_name,
                    "action": "ignored",
                    "reason": "no_processor_registered",
                }
            await self._mark_inbound_event_processed(event, action)
            actions.append(action)
        return {
            "processed": len(actions),
            "skipped_already_processed": skipped_already_processed,
            "actions": actions,
            "dry_run": dry_run,
            "source": "sqlite_inbound_event_cache",
            "canonical_source": "neo4j_via_assistx",
        }

    async def dispatch_outbox(self, dry_run: bool = True, limit: int = 25) -> dict:
        events = self.cache.pending_events(limit=limit)
        delivered = 0
        failed = 0
        dry_run_events = []
        for event in events:
            status = await self.assistx.event_status(event.idempotency_key)
            if status.get("known") and status.get("applied", True):
                self.cache.mark_event_delivered(event.idempotency_key)
                delivered += 1
                continue
            if dry_run:
                dry_run_events.append(event.model_dump(mode="json"))
                continue
            try:
                result = await self.assistx.post_event(event, dry_run=False)
                if result.get("delivered"):
                    self.cache.mark_event_delivered(event.idempotency_key)
                    delivered += 1
                else:
                    self.cache.mark_event_failed(
                        event.idempotency_key,
                        f"AssistX returned {result.get('status_code', 'unknown')}",
                    )
                    failed += 1
            except Exception as exc:
                self.cache.mark_event_failed(event.idempotency_key, str(exc))
                failed += 1
        return {
            "dry_run": dry_run,
            "considered": len(events),
            "delivered": delivered,
            "failed": failed,
            "dry_run_events": dry_run_events,
            "summary": self.cache.outbox_summary(),
            "canonical_source": "neo4j_via_assistx",
        }

    async def reconcile_outbox(self, limit: int = 100) -> dict:
        events = self.cache.pending_events(limit=limit)
        delivered_keys: list[str] = []
        conflicts: list[dict] = []
        for event in events:
            status = await self.assistx.event_status(event.idempotency_key)
            if status.get("known") and status.get("applied", True):
                delivered_keys.append(event.idempotency_key)
            elif status.get("known") and status.get("conflict"):
                self.cache.mark_event_failed(event.idempotency_key, "canonical graph conflict", dead_letter=True)
                conflicts.append({"idempotency_key": event.idempotency_key, "status": status})
        reconciled = self.cache.reconcile_delivered(delivered_keys)
        return {
            "checked": len(events),
            "reconciled_delivered": reconciled,
            "conflicts_dead_lettered": len(conflicts),
            "conflicts": conflicts,
            "summary": self.cache.outbox_summary(),
            "canonical_source": "neo4j_via_assistx",
        }

    def list_assignments(self, limit: int = 50) -> list[dict]:
        return self.cache.list_assignments(limit=limit)

    def get_assignment(self, assignment_id: str) -> dict | None:
        return self.cache.get_assignment(assignment_id)

    def list_heartbeats(self, limit: int = 50) -> list[dict]:
        return self.cache.list_heartbeats(limit=limit)

    def list_stale_heartbeats(self, stale_after_seconds: int | None = None, limit: int = 50) -> dict:
        threshold = stale_after_seconds or self.settings.stale_heartbeat_seconds
        stale = self.cache.list_stale_heartbeats(stale_after_seconds=threshold, limit=limit)
        return {
            "source": "sqlite_cache_mirror",
            "canonical_source": "neo4j_via_assistx",
            "stale_after_seconds": threshold,
            "count": len(stale),
            "heartbeats": stale,
        }

    def list_inbound_events(self, limit: int = 50, event_type: str | None = None) -> list[dict]:
        return self.cache.list_inbound_events(limit=limit, event_type=event_type)

    def list_outbox_events(self, limit: int = 50, status: str | None = None) -> list[dict]:
        return self.cache.list_outbox_events(limit=limit, status=status)

    def list_scheduler_runs(self, limit: int = 50) -> list[dict]:
        return self.cache.list_scheduler_runs(limit=limit)

    def ops_summary(self) -> dict:
        summary = self.cache.ops_summary()
        summary["scheduler"]["enabled"] = self.settings.scheduler_enabled
        summary["scheduler"]["last_tick_at"] = self.last_tick_at
        summary["dispatch_enabled"] = self.settings.dispatch_enabled
        summary["direct_workers_enabled"] = self.settings.direct_workers_enabled
        summary["stale_heartbeat_seconds"] = self.settings.stale_heartbeat_seconds
        summary["inbound_processing"] = self.list_inbound_processing(limit=10)
        return summary

    def _decision_event(self, decision: AssignmentDecision, dry_run: bool) -> EventEnvelope:
        if decision.status == AssignmentStatus.APPROVAL_REQUIRED:
            event_type = EventType.ASSIGNMENT_APPROVAL_REQUIRED
        elif decision.status == AssignmentStatus.RECOMMENDED:
            event_type = EventType.ASSIGNMENT_RECOMMENDED
        else:
            event_type = EventType.ASSIGNMENT_SKIPPED
        return EventEnvelope(
            event_type=event_type,
            idempotency_key=decision.idempotency_key,
            subject=decision.task_id,
            payload={**decision.model_dump(mode="json"), "dry_run": dry_run, "cache_role": "outbox_replay_buffer"},
            privacy=[],
        )

    # ------------------------------------------------------------------
    # Orchestration plan claim / lease / completion lifecycle
    # ------------------------------------------------------------------

    async def claim_assignment(self, assignment_id: str, request: AssignmentClaimRequest) -> dict:
        assignment = self.cache.get_assignment(assignment_id)
        if assignment is None:
            return {"accepted": False, "reason": "assignment_not_found_in_local_cache"}
        if assignment.get("status") not in ("recommended", "reserved"):
            return {"accepted": False, "reason": f"assignment status '{assignment.get('status')}' is not claimable"}

        now = utc_now()
        lease_expires = now.timestamp() + request.lease_seconds
        self.cache.update_assignment_status(
            assignment_id,
            status="running",
            worker_id=request.worker_id,
            node_id=request.node_id,
            lease_expires_at=lease_expires,
        )

        event = EventEnvelope(
            event_type=EventType.ASSIGNMENT_CLAIMED,
            idempotency_key=f"{EventType.ASSIGNMENT_CLAIMED}:{assignment_id}:{request.worker_id}:{request.correlation_id}",
            subject=request.task_id,
            correlation_id=request.correlation_id,
            payload={
                **self._assignment_event_context(
                    assignment_id,
                    assignment,
                    task_id=request.task_id,
                    worker_id=request.worker_id,
                    node_id=request.node_id,
                    status="running",
                    lease_seconds=request.lease_seconds,
                    lease_expires_at=lease_expires,
                    route_id=request.route_id,
                ),
                "route_id": request.route_id,
                "capabilities": request.capabilities,
                "correlation_id": request.correlation_id,
                **request.metadata,
            },
        )
        self.cache.enqueue_event(event)
        return {
            "accepted": True,
            "event_id": event.event_id,
            "assignment_id": assignment_id,
            "worker_id": request.worker_id,
            "lease_expires_at": lease_expires,
            "correlation_id": request.correlation_id,
        }

    async def complete_assignment(self, assignment_id: str, request: AssignmentCompletionRequest) -> dict:
        assignment = self.cache.get_assignment(assignment_id)
        if assignment is None:
            return {"accepted": False, "reason": "assignment_not_found_in_local_cache"}

        status = "done" if request.status == "success" else "failed"
        self.cache.update_assignment_status(assignment_id, status=status)

        event_type = EventType.ASSIGNMENT_COMPLETED if status == "done" else EventType.ASSIGNMENT_FAILED
        event = EventEnvelope(
            event_type=event_type,
            idempotency_key=f"{event_type}:{assignment_id}:{request.worker_id}:{request.status}:{utc_now().isoformat()}",
            subject=request.task_id,
            correlation_id=request.correlation_id,
            payload={
                **self._assignment_event_context(
                    assignment_id,
                    assignment,
                    task_id=request.task_id,
                    worker_id=request.worker_id,
                    status=status,
                    route_id=assignment.get("route_id"),
                ),
                "status": request.status,
                "summary": request.summary,
                "artifacts": request.artifacts,
                "correlation_id": request.correlation_id,
                **request.metadata,
            },
        )
        self.cache.enqueue_event(event)
        return {
            "accepted": True,
            "event_id": event.event_id,
            "assignment_id": assignment_id,
            "status": status,
            "correlation_id": request.correlation_id,
        }

    def record_heartbeat_with_lease_renewal(self, heartbeat: HeartbeatRequest) -> EventEnvelope:
        heartbeat_id = f"heartbeat_{uuid4().hex}"
        self.cache.record_heartbeat(heartbeat_id, heartbeat)

        lease_renewed = False
        renewed_lease_expires = None
        assignment = None
        if heartbeat.assignment_id:
            assignment = self.cache.get_assignment(heartbeat.assignment_id)
            if assignment and assignment.get("worker_id") == heartbeat.worker_id:
                renewed_lease_expires = utc_now().timestamp() + self.settings.default_lease_seconds
                self.cache.update_assignment_status(
                    heartbeat.assignment_id,
                    status=assignment.get("status", "running"),
                    lease_expires_at=renewed_lease_expires,
                )
                lease_renewed = True

        event = EventEnvelope(
            event_type=EventType.ASSIGNMENT_HEARTBEAT,
            idempotency_key=f"{EventType.ASSIGNMENT_HEARTBEAT}:{heartbeat.node_id}:{heartbeat.worker_id}:{heartbeat.assignment_id}:{heartbeat_id}",
            subject=heartbeat.assignment_id or heartbeat.worker_id or heartbeat.node_id,
            payload={
                "heartbeat_id": heartbeat_id,
                "lease_renewed": lease_renewed,
                **heartbeat.model_dump(mode="json"),
                **(
                    self._assignment_event_context(
                        heartbeat.assignment_id,
                        assignment,
                        status=assignment.get("status", "running") if assignment else heartbeat.status,
                        lease_expires_at=renewed_lease_expires if lease_renewed else (assignment.get("lease_expires_at") if assignment else None),
                    )
                    if heartbeat.assignment_id and assignment
                    else {}
                ),
            },
        )
        self.cache.enqueue_event(event)
        return event

    def expire_stale_leases(self) -> dict:
        stale_threshold = self.settings.default_lease_seconds
        expired = self.cache.expire_stale_assignments(stale_seconds=stale_threshold)
        expired_events = 0
        for assignment_id in expired:
            assignment = self.cache.get_assignment(assignment_id)
            if assignment:
                event = EventEnvelope(
                    event_type=EventType.ASSIGNMENT_EXPIRED,
                    idempotency_key=f"{EventType.ASSIGNMENT_EXPIRED}:{assignment_id}:{utc_now().isoformat()}",
                    subject=assignment.get("task_id", assignment_id),
                    payload={
                        **self._assignment_event_context(assignment_id, assignment),
                        "reason": "lease_expired",
                    },
                )
                self.cache.enqueue_event(event)
                expired_events += 1
        return {"expired_count": len(expired), "events_emitted": expired_events}
