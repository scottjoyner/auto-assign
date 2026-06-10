"""Scheduler, heartbeat, and lease tests for AssignmentService."""

from __future__ import annotations

from auto_assign.cache import CacheStore
from auto_assign.clients import AssistXClient, RouterClient
from auto_assign.models import (
    AssignmentCandidate,
    AssignmentStatus,
    HeartbeatRequest,
    Lane,
    RouterSnapshot,
    SchedulerTickRequest,
)
from auto_assign.scorer import AssignmentScorer
from auto_assign.service import AssignmentService
from auto_assign.settings import Settings


class FakeAssistXClient(AssistXClient):
    def __init__(self, settings: Settings):
        super().__init__(settings)
        self.applied_keys: set[str] = set()
        self.delivered_keys: set[str] = set()

    async def health(self):
        return {"reachable": True, "brain": "neo4j_via_assistx"}

    async def get_task(self, task_id: str):
        return None

    async def get_backlog_candidates(self, limit: int = 25):
        return [AssignmentCandidate(task_id=f"ASS-{i}") for i in range(min(limit, 3))]

    async def event_status(self, idempotency_key: str):
        if idempotency_key in self.applied_keys:
            return {"known": True, "applied": True, "canonical_status": "applied"}
        return {"known": False}

    async def post_event(self, event, dry_run: bool = True):
        if dry_run:
            return {"delivered": False, "dry_run": True}
        self.delivered_keys.add(event.idempotency_key)
        return {"delivered": True, "status_code": 202}


class FakeRouterClient(RouterClient):
    async def health(self):
        return {"reachable": True}

    async def snapshot(self):
        return RouterSnapshot(reachable=True, context_revision="test-rev")


def make_service(tmp_path):
    settings = Settings(database_url=f"sqlite:///{tmp_path / 'scheduler.sqlite3'}")
    return AssignmentService(
        settings=settings,
        cache=CacheStore(settings.sqlite_path),
        assistx=FakeAssistXClient(settings),
        router=FakeRouterClient(settings),
        scorer=AssignmentScorer(settings),
    )


class TestSchedulerTick:
    async def test_scheduler_tick_evaluates_candidates(self, tmp_path):
        svc = make_service(tmp_path)
        response = await svc.scheduler_tick(
            SchedulerTickRequest(dry_run=True, limit=2, reason="test")
        )
        assert response.evaluated == 2
        assert response.dry_run is True
        assert response.scheduler_run_id.startswith("tick_")
        assert response.decisions[0].task_id == "ASS-0"
        assert response.decisions[1].task_id == "ASS-1"

    async def test_scheduler_tick_with_task_ids(self, tmp_path):
        svc = make_service(tmp_path)
        response = await svc.scheduler_tick(
            SchedulerTickRequest(dry_run=True, task_ids=["CUSTOM-1", "CUSTOM-2"], reason="manual", limit=10)
        )
        assert response.evaluated == 2
        assert response.decisions[0].task_id == "CUSTOM-1"

    async def test_scheduler_tick_empty_candidates(self, tmp_path):
        svc = make_service(tmp_path)
        svc.assistx.get_backlog_candidates = lambda limit=25: []  # type: ignore[method-assign]
        response = await svc.scheduler_tick(
            SchedulerTickRequest(dry_run=True, limit=5, reason="test")
        )
        assert response.evaluated == 0
        assert response.recommended == 0

    async def test_scheduler_tick_records_run(self, tmp_path):
        svc = make_service(tmp_path)
        await svc.scheduler_tick(
            SchedulerTickRequest(dry_run=True, limit=1, reason="test")
        )
        runs = svc.list_scheduler_runs()
        assert len(runs) == 1
        assert runs[0]["trigger_reason"] == "test"


class TestHeartbeat:
    def test_record_heartbeat_creates_event(self, tmp_path):
        svc = make_service(tmp_path)
        heartbeat = HeartbeatRequest(
            node_id="node-1",
            worker_id="worker-1",
            assignment_id=None,
        )
        event = svc.record_heartbeat(heartbeat)
        assert event.event_type == "worker.heartbeat.recorded"
        assert "heartbeat_" in event.event_id
        hbs = svc.list_heartbeats()
        assert len(hbs) == 1
        assert hbs[0]["worker_id"] == "worker-1"

    def test_record_heartbeat_with_lease_renewal(self, tmp_path):
        svc = make_service(tmp_path)
        # First create an assignment
        from auto_assign.models import AssignmentDecision

        decision = AssignmentDecision(
            assignment_id="assign-1",
            task_id="ASS-1",
            decision_id="dec-1",
            status=AssignmentStatus.IN_PROGRESS,
            selected_lane=Lane.PAPERCLIP,
            selected_target="worker-1",
            score=0.9,
            idempotency_key="key-1",
        )
        svc.cache.upsert_assignment(decision)
        svc.cache.update_assignment_status(
            "assign-1",
            status="in_progress",
            worker_id="worker-1",
        )

        heartbeat = HeartbeatRequest(
            node_id="node-1",
            worker_id="worker-1",
            assignment_id="assign-1",
        )
        event = svc.record_heartbeat_with_lease_renewal(heartbeat)
        assert event.event_type == "assignment.heartbeat"
        assert event.payload.get("lease_renewed") is True

    def test_heartbeat_without_assignment_no_lease_renewal(self, tmp_path):
        svc = make_service(tmp_path)
        heartbeat = HeartbeatRequest(
            node_id="node-1",
            worker_id="worker-1",
        )
        event = svc.record_heartbeat_with_lease_renewal(heartbeat)
        assert event.payload.get("lease_renewed") is False

    def test_list_stale_heartbeats(self, tmp_path):
        svc = make_service(tmp_path)
        import time
        # Record a heartbeat with an old timestamp
        svc.cache.record_heartbeat("hb-1", HeartbeatRequest(
            node_id="node-stale",
            worker_id="worker-stale",
        ))
        svc.cache._upsert(
            "INSERT OR REPLACE INTO heartbeats (heartbeat_id, node_id, worker_id, assignment_id, status, heartbeat_at, created_at) "
            "VALUES (?, ?, ?, ?, ?, ?, ?)",
            ("hb-1", "node-stale", "worker-stale", None, "running",
             (time.time() - 3600), (time.time() - 3600)),
        )
        stale = svc.list_stale_heartbeats(stale_after_seconds=300)
        assert stale["count"] >= 1
        assert stale["stale_after_seconds"] == 300


class TestExpireStaleLeases:
    def test_expire_stale_no_assignments(self, tmp_path):
        svc = make_service(tmp_path)
        result = svc.expire_stale_leases()
        assert result["expired_count"] == 0
        assert result["events_emitted"] == 0

    def test_expire_stale_leases_expired(self, tmp_path):
        import time

        svc = make_service(tmp_path)
        from auto_assign.models import AssignmentDecision

        decision = AssignmentDecision(
            assignment_id="assign-expired",
            task_id="ASS-1",
            decision_id="dec-1",
            status=AssignmentStatus.IN_PROGRESS,
            selected_lane=Lane.PAPERCLIP,
            selected_target="worker-1",
            score=0.9,
            idempotency_key="key-exp",
        )
        svc.cache.upsert_assignment(decision)
        # Set lease expiry far in the past
        svc.cache._upsert(
            "UPDATE assignments SET lease_expires_at = ? WHERE assignment_id = ?",
            ((time.time() - 3600), "assign-expired"),
        )
        result = svc.expire_stale_leases()
        assert result["expired_count"] == 1
        assert result["events_emitted"] == 1

    def test_expire_stale_active_lease_skipped(self, tmp_path):
        import time

        svc = make_service(tmp_path)
        from auto_assign.models import AssignmentDecision

        decision = AssignmentDecision(
            assignment_id="assign-active",
            task_id="ASS-2",
            decision_id="dec-2",
            status=AssignmentStatus.IN_PROGRESS,
            selected_lane=Lane.PAPERCLIP,
            selected_target="worker-2",
            score=0.95,
            idempotency_key="key-active",
        )
        svc.cache.upsert_assignment(decision)
        # Set lease to far in the future
        svc.cache._upsert(
            "UPDATE assignments SET lease_expires_at = ? WHERE assignment_id = ?",
            ((time.time() + 3600), "assign-active"),
        )
        result = svc.expire_stale_leases()
        assert result["expired_count"] == 0
        assert result["events_emitted"] == 0
