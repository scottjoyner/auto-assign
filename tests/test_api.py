from fastapi.testclient import TestClient

from auto_assign.cache import CacheStore
from auto_assign.clients import AssistXClient, RouterClient
from auto_assign.main import app
from auto_assign.scorer import AssignmentScorer
from auto_assign.service import AssignmentService
from auto_assign.settings import Settings


class FakeAssistXClient(AssistXClient):
    async def health(self):
        return {"reachable": True, "brain": "neo4j_via_assistx"}

    async def get_task(self, task_id: str):
        return None

    async def event_status(self, idempotency_key: str):
        return {"known": False}


class FakeRouterClient(RouterClient):
    async def health(self):
        return {"reachable": True}

    async def snapshot(self):
        from auto_assign.models import RouterSnapshot

        return RouterSnapshot(reachable=True, context_revision="test-rev")


def make_client(tmp_path):
    settings = Settings(database_url=f"sqlite:///{tmp_path / 'api.sqlite3'}")
    service = AssignmentService(
        settings=settings,
        cache=CacheStore(settings.sqlite_path),
        assistx=FakeAssistXClient(settings),
        router=FakeRouterClient(settings),
        scorer=AssignmentScorer(settings),
    )
    app.state.assignment_service = service
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
