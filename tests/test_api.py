from fastapi.testclient import TestClient

from auto_assign.cache import CacheStore
from auto_assign.clients import AssistXClient, RouterClient
from auto_assign.main import app
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
        from auto_assign.models import AssignmentCandidate

        return [AssignmentCandidate(task_id=f"ASS-{index}") for index in range(min(limit, 2))]

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
        from auto_assign.models import RouterSnapshot

        return RouterSnapshot(reachable=True, context_revision="test-rev")


def make_service(tmp_path):
    settings = Settings(database_url=f"sqlite:///{tmp_path / 'api.sqlite3'}")
    return AssignmentService(
        settings=settings,
        cache=CacheStore(settings.sqlite_path),
        assistx=FakeAssistXClient(settings),
        router=FakeRouterClient(settings),
        scorer=AssignmentScorer(settings),
    )


def make_client(tmp_path):
    app.state.assignment_service = make_service(tmp_path)
    return TestClient(app)


def create_assignment(client: TestClient, task_id: str = "ASS-api") -> dict:
    response = client.post(
        "/api/assignments/evaluate",
        json={"task_id": task_id, "dry_run": True, "candidate_lanes": ["router_model"]},
    )
    assert response.status_code == 200
    return response.json()


def test_health_reports_brain_cache_and_control(tmp_path):
    client = make_client(tmp_path)

    response = client.get("/health")

    assert response.status_code == 200
    payload = response.json()
    assert payload["deps"]["assistx"]["brain"] == "neo4j_via_assistx"
    assert payload["deps"]["cache"]["role"] == "cache_outbox_only"
    assert payload["scheduler"]["control"]["mode"] == "enabled"
    assert payload["scheduler"]["control"]["new_assignments_allowed"] is True


def test_assignment_control_defaults_and_updates(tmp_path):
    client = make_client(tmp_path)

    default_state = client.get("/api/assignment-control").json()
    assert default_state["mode"] == "enabled"
    assert default_state["new_assignments_allowed"] is True

    updated = client.post(
        "/api/assignment-control",
        json={
            "mode": "draining",
            "reason": "operator drain before interactive work",
            "updated_by": "operator",
            "dry_run": True,
        },
    ).json()

    assert updated["mode"] == "draining"
    assert updated["new_assignments_allowed"] is False
    assert updated["lease_renewals_allowed"] is True
    assert updated["cache_role"] == "assignment_control_cache_and_outbox"
    assert updated["canonical_source"] == "neo4j_via_assistx"
    assert updated["event_id"].startswith("evt_")
    assert client.get("/api/outbox/summary").json()["summary"] == {"pending": 1}

    read_back = client.get("/api/assignment-control").json()
    assert read_back["mode"] == "draining"
    assert read_back["reason"] == "operator drain before interactive work"


def test_inbound_event_intake_records_inbound_cache_only(tmp_path):
    client = make_client(tmp_path)

    response = client.post(
        "/api/events",
        json={
            "event_type": "router.quota_snapshot.recorded",
            "idempotency_key": "router.quota_snapshot.recorded:test",
            "subject": "auto-router",
            "payload": {"mode": "test"},
        },
    )

    assert response.status_code == 200
    payload = response.json()
    assert payload["accepted"] is True
    assert payload["source"] == "sqlite_inbound_event_cache"
    assert payload["cache_role"] == "received_event_mirror"
    assert client.get("/api/outbox/summary").json()["summary"] == {}

    inbound = client.get("/api/events").json()
    assert inbound["source"] == "sqlite_inbound_event_cache"
    assert inbound["canonical_source"] == "neo4j_via_assistx"
    assert inbound["events"][0]["event_type"] == "router.quota_snapshot.recorded"


def test_process_task_candidate_event_triggers_dry_run_assignment(tmp_path):
    client = make_client(tmp_path)
    client.post(
        "/api/events",
        json={
            "event_type": "task.candidate.created",
            "idempotency_key": "task.candidate.created:ASS-trigger",
            "subject": "ASS-trigger",
            "payload": {"task_id": "ASS-trigger"},
        },
    )

    response = client.post("/api/events/process?event_type=task.candidate.created&dry_run=true")

    assert response.status_code == 200
    payload = response.json()
    assert payload["processed"] == 1
    assert payload["actions"][0]["action"] == "assignment_evaluated"
    assert payload["actions"][0]["task_id"] == "ASS-trigger"
    assert client.get("/api/outbox/summary").json()["summary"] == {"pending": 1}

    processing = client.get("/api/events/processing").json()
    assert processing["source"] == "sqlite_inbound_processing_cache"
    assert processing["processing"][0]["status"] == "assignment_evaluated"


def test_processed_inbound_event_is_not_reprocessed_by_default(tmp_path):
    client = make_client(tmp_path)
    client.post(
        "/api/events",
        json={
            "event_type": "task.candidate.created",
            "idempotency_key": "task.candidate.created:ASS-once",
            "subject": "ASS-once",
            "payload": {"task_id": "ASS-once"},
        },
    )

    first = client.post("/api/events/process?event_type=task.candidate.created&dry_run=true").json()
    second = client.post("/api/events/process?event_type=task.candidate.created&dry_run=true").json()

    assert first["processed"] == 1
    assert second["processed"] == 0
    assert second["skipped_already_processed"] == 1
    assert client.get("/api/outbox/summary").json()["summary"] == {"pending": 1}


def test_processed_inbound_event_can_be_replayed_explicitly(tmp_path):
    client = make_client(tmp_path)
    client.post(
        "/api/events",
        json={
            "event_type": "task.candidate.created",
            "idempotency_key": "task.candidate.created:ASS-replay",
            "subject": "ASS-replay",
            "payload": {"task_id": "ASS-replay"},
        },
    )

    client.post("/api/events/process?event_type=task.candidate.created&dry_run=true")
    replay = client.post(
        "/api/events/process?event_type=task.candidate.created&dry_run=true&include_processed=true"
    ).json()

    assert replay["processed"] == 1
    assert replay["actions"][0]["action"] == "assignment_evaluated"


def test_process_router_snapshot_event_triggers_scheduler_tick(tmp_path):
    client = make_client(tmp_path)
    client.post(
        "/api/events",
        json={
            "event_type": "router.service_snapshot.recorded",
            "idempotency_key": "router.service_snapshot.recorded:trigger",
            "subject": "auto-router",
        },
    )

    response = client.post(
        "/api/events/process?event_type=router.service_snapshot.recorded&dry_run=true&limit=2"
    )

    assert response.status_code == 200
    payload = response.json()
    assert payload["processed"] == 1
    assert payload["actions"][0]["action"] == "scheduler_tick"
    assert payload["actions"][0]["evaluated"] == 2
    assert len(client.get("/api/scheduler/runs").json()["runs"]) == 1


def test_evaluate_assignment_writes_cache_outbox(tmp_path):
    client = make_client(tmp_path)

    payload = create_assignment(client)

    assert payload["selected_lane"] == "router_model"
    assert payload["cache_only"] is True

    outbox = client.get("/api/outbox/summary").json()
    assert outbox["canonical_source"] == "neo4j_via_assistx"
    assert outbox["summary"] == {"pending": 1}


def test_stale_heartbeat_watchdog_reports_cache_visibility(tmp_path):
    client = make_client(tmp_path)
    client.post("/api/heartbeats", json={"node_id": "node-watchdog", "status": "online"})

    stale = client.get("/api/heartbeats/stale?stale_after_seconds=1").json()
    assert stale["source"] == "sqlite_cache_mirror"
    assert stale["canonical_source"] == "neo4j_via_assistx"
    assert stale["stale_after_seconds"] == 1
    assert stale["count"] >= 0

    health = client.get("/health").json()
    assert health["deps"]["cache"]["role"] == "cache_outbox_only"


def test_outbox_reconcile_marks_graph_applied_events_delivered(tmp_path):
    service = make_service(tmp_path)
    app.state.assignment_service = service
    client = TestClient(app)

    payload = create_assignment(client, "ASS-reconcile")
    key = payload["idempotency_key"]
    service.assistx.applied_keys.add(key)

    reconcile = client.post("/api/outbox/reconcile").json()

    assert reconcile["reconciled_delivered"] == 1
    assert reconcile["summary"] == {"delivered": 1}


def test_outbox_dispatch_dry_run_does_not_mark_delivered(tmp_path):
    client = make_client(tmp_path)
    create_assignment(client, "ASS-dry")

    dispatch = client.post("/api/outbox/dispatch?dry_run=true").json()

    assert dispatch["dry_run"] is True
    assert dispatch["considered"] == 1
    assert dispatch["summary"] == {"pending": 1}


def test_approve_assignment_enqueues_graph_event(tmp_path):
    client = make_client(tmp_path)
    assignment = create_assignment(client, "ASS-approve")

    response = client.post(
        f"/api/assignments/{assignment['assignment_id']}/approve",
        json={"approved_by": "operator", "approval_reason": "test approval", "dry_run": True},
    )

    assert response.status_code == 200
    payload = response.json()
    assert payload["accepted"] is True
    assert payload["canonical_source"] == "neo4j_via_assistx"
    assert payload["cache_role"] == "outbox_replay_buffer"
    assert client.get("/api/outbox/summary").json()["summary"] == {"pending": 2}


def test_release_assignment_enqueues_graph_event(tmp_path):
    client = make_client(tmp_path)
    assignment = create_assignment(client, "ASS-release")

    response = client.post(
        f"/api/assignments/{assignment['assignment_id']}/release",
        json={"reason": "test_release", "retryable": True, "dry_run": True},
    )

    assert response.status_code == 200
    payload = response.json()
    assert payload["accepted"] is True
    assert payload["canonical_source"] == "neo4j_via_assistx"
    assert payload["cache_role"] == "outbox_replay_buffer"
    assert client.get("/api/outbox/summary").json()["summary"] == {"pending": 2}


def test_claim_assignment_transitions_to_running(tmp_path):
    client = make_client(tmp_path)
    assignment = create_assignment(client, "ASS-claim")

    response = client.post(
        f"/api/assignments/{assignment['assignment_id']}/claim",
        json={
            "correlation_id": "corr-claim",
            "task_id": assignment["task_id"],
            "route_id": "route-claim",
            "worker_id": "worker-claim",
            "node_id": "node-claim",
            "capabilities": ["python"],
            "lease_seconds": 1200,
        },
    )

    assert response.status_code == 200
    payload = response.json()
    assert payload["accepted"] is True
    assert payload["assignment_id"] == assignment["assignment_id"]
    assert payload["worker_id"] == "worker-claim"

    fetched = client.get(f"/api/assignments/{assignment['assignment_id']}").json()
    assert fetched["assignment"]["status"] == "running"
    assert fetched["assignment"]["worker_id"] == "worker-claim"

    events = client.get("/api/outbox/events").json()["events"]
    assert any(e["event_type"] == "assignment.claimed" for e in events)


def test_approve_missing_assignment_returns_404(tmp_path):

    client = make_client(tmp_path)

    response = client.post(
        "/api/assignments/missing/approve",
        json={"approved_by": "operator", "approval_reason": "missing"},
    )

    assert response.status_code == 404


def test_scheduler_tick_records_local_run_summary(tmp_path):
    client = make_client(tmp_path)

    response = client.post("/api/scheduler/tick", json={"dry_run": True, "limit": 2})
    runs = client.get("/api/scheduler/runs").json()

    assert response.status_code == 200
    assert response.json()["evaluated"] == 2
    assert runs["source"] == "sqlite_cache_mirror"
    assert runs["canonical_source"] == "neo4j_via_assistx"
    assert len(runs["runs"]) == 1
    assert runs["runs"][0]["evaluated_count"] == 2


def test_heartbeat_listing_exposes_local_mirror(tmp_path):
    client = make_client(tmp_path)

    response = client.post(
        "/api/heartbeats",
        json={"node_id": "node-1", "worker_id": "worker-1", "status": "online"},
    )
    heartbeats = client.get("/api/heartbeats").json()

    assert response.status_code == 200
    assert heartbeats["source"] == "sqlite_cache_mirror"
    assert heartbeats["canonical_source"] == "neo4j_via_assistx"
    assert heartbeats["heartbeats"][0]["node_id"] == "node-1"


def test_stale_heartbeat_listing_is_read_only_cache_visibility(tmp_path):
    client = make_client(tmp_path)
    client.post("/api/heartbeats", json={"node_id": "node-stale", "status": "online"})

    stale = client.get("/api/heartbeats/stale?stale_after_seconds=1").json()

    assert stale["source"] == "sqlite_cache_mirror"
    assert stale["canonical_source"] == "neo4j_via_assistx"
    assert stale["stale_after_seconds"] == 1
    assert stale["count"] == 0


def test_outbox_events_listing_exposes_payload_and_status(tmp_path):
    client = make_client(tmp_path)
    create_assignment(client, "ASS-outbox-list")

    events = client.get("/api/outbox/events").json()

    assert events["source"] == "sqlite_cache_mirror"
    assert events["canonical_source"] == "neo4j_via_assistx"
    assert events["events"][0]["status"] == "pending"
    assert events["events"][0]["payload"]["event_type"] == "assign.assignment.recommended"


def test_assignment_summary_reports_governor_state(tmp_path):
    client = make_client(tmp_path)
    create_assignment(client, "ASS-summary")
    client.post("/api/heartbeats", json={"node_id": "node-summary", "status": "online"})

    summary = client.get("/api/assignments/summary").json()

    assert summary["cache_role"] == "assignment_governor_read_model"
    assert summary["canonical_source"] == "neo4j_via_assistx"
    assert summary["control"]["mode"] == "enabled"
    assert summary["assignments_by_status"] == {"recommended": 1}
    assert summary["outbox_by_status"] == {"pending": 2}
    assert summary["heartbeats"]["count"] == 1
    assert summary["heartbeats"]["stale_count"] == 0
    assert summary["safety"]["cache_is_canonical"] is False
    assert summary["safety"]["control_mode"] == "enabled"
    assert any(rec["action"] == "dry_run_only" for rec in summary["recommendations"])
    assert any(rec["action"] == "dispatch_or_reconcile_outbox" for rec in summary["recommendations"])


def test_assignment_summary_respects_paused_control(tmp_path):
    client = make_client(tmp_path)
    client.post(
        "/api/assignment-control",
        json={"mode": "paused", "reason": "pause for interactive work", "updated_by": "operator"},
    )

    summary = client.get("/api/assignments/summary").json()

    assert summary["control"]["mode"] == "paused"
    assert summary["safety"]["assignment_allowed"] is False
    assert any(rec["action"] == "keep_assignment_governor_paused" for rec in summary["recommendations"])


def test_assignment_summary_respects_draining_control(tmp_path):
    client = make_client(tmp_path)
    client.post(
        "/api/assignment-control",
        json={"mode": "draining", "reason": "finish safe checkpoints", "updated_by": "operator"},
    )

    summary = client.get("/api/assignments/summary").json()

    assert summary["control"]["mode"] == "draining"
    assert summary["control"]["lease_renewals_allowed"] is True
    assert any(rec["action"] == "drain_assignment_governor" for rec in summary["recommendations"])


def test_ops_summary_reports_cache_state(tmp_path):
    client = make_client(tmp_path)
    create_assignment(client, "ASS-ops")
    client.post("/api/heartbeats", json={"node_id": "node-ops", "status": "online"})
    client.post(
        "/api/events",
        json={
            "event_type": "router.service_snapshot.recorded",
            "idempotency_key": "router.service_snapshot.recorded:test",
            "subject": "auto-router",
        },
    )

    summary = client.get("/api/ops/summary").json()

    assert summary["cache_role"] == "cache_outbox_only"
    assert summary["canonical_source"] == "neo4j_via_assistx"
    assert summary["control"]["mode"] == "enabled"
    assert summary["assignments_by_status"] == {"recommended": 1}
    assert summary["outbox_by_status"] == {"pending": 2}
    assert summary["inbound_by_type"] == {"router.service_snapshot.recorded": 1}
    assert summary["inbound_events"]["count"] == 1
    assert summary["heartbeats"]["count"] == 1
    assert summary["stale_heartbeat_seconds"] > 0
    assert isinstance(summary["inbound_processing"], list)


def test_api_auth_enforced_when_token_set(tmp_path):
    from auto_assign.settings import Settings as _Settings

    settings = _Settings(
        database_url=f"sqlite:///{tmp_path / 'auth.sqlite3'}",
        api_token="secret-token",
    )
    svc = AssignmentService(
        settings=settings,
        cache=CacheStore(settings.sqlite_path),
        assistx=FakeAssistXClient(settings),
        router=FakeRouterClient(settings),
        scorer=AssignmentScorer(settings),
    )
    app.state.assignment_service = svc
    client = TestClient(app)

    # health stays open
    assert client.get("/health").status_code == 200

    # protected route without token -> 401
    assert client.get("/api/assignments").status_code == 401

    # Bearer token accepted
    assert client.get("/api/assignments", headers={"Authorization": "Bearer secret-token"}).status_code == 200

    # Basic auth (password == token) accepted
    import base64 as _b64

    basic = _b64.b64encode(b"anyuser:secret-token").decode()
    assert client.get("/api/assignments", headers={"Authorization": f"Basic {basic}"}).status_code == 200

    # wrong token -> 401
    assert client.get("/api/assignments", headers={"Authorization": "Bearer wrong"}).status_code == 401
