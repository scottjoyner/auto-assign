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


def test_health_reports_brain_and_cache(tmp_path):
    client = make_client(tmp_path)

    response = client.get("/health")

    assert response.status_code == 200
    payload = response.json()
    assert payload["assistx"]["brain"] == "neo4j_via_assistx"
    assert payload["cache"]["role"] == "cache_outbox_only"


def test_evaluate_assignment_writes_cache_outbox(tmp_path):
    client = make_client(tmp_path)

    response = client.post(
        "/api/assignments/evaluate",
        json={"task_id": "ASS-api", "dry_run": True, "candidate_lanes": ["paperclip"]},
    )

    assert response.status_code == 200
    payload = response.json()
    assert payload["selected_lane"] == "paperclip"
    assert payload["cache_only"] is True

    outbox = client.get("/api/outbox/summary").json()
    assert outbox["canonical_source"] == "neo4j_via_assistx"
    assert outbox["summary"] == {"pending": 1}


def test_outbox_reconcile_marks_graph_applied_events_delivered(tmp_path):
    service = make_service(tmp_path)
    app.state.assignment_service = service
    client = TestClient(app)

    response = client.post(
        "/api/assignments/evaluate",
        json={"task_id": "ASS-reconcile", "dry_run": True, "candidate_lanes": ["paperclip"]},
    )
    key = response.json()["idempotency_key"]
    service.assistx.applied_keys.add(key)

    reconcile = client.post("/api/outbox/reconcile").json()

    assert reconcile["reconciled_delivered"] == 1
    assert reconcile["summary"] == {"delivered": 1}


def test_outbox_dispatch_dry_run_does_not_mark_delivered(tmp_path):
    client = make_client(tmp_path)
    client.post(
        "/api/assignments/evaluate",
        json={"task_id": "ASS-dry", "dry_run": True, "candidate_lanes": ["paperclip"]},
    )

    dispatch = client.post("/api/outbox/dispatch?dry_run=true").json()

    assert dispatch["dry_run"] is True
    assert dispatch["considered"] == 1
    assert dispatch["summary"] == {"pending": 1}
