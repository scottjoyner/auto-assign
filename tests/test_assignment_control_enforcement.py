from fastapi.testclient import TestClient

from auto_assign.cache import CacheStore
from auto_assign.clients import AssistXClient, RouterClient
from auto_assign.main import app
from auto_assign.models import AssignmentCandidate, RouterSnapshot
from auto_assign.scorer import AssignmentScorer
from auto_assign.service import AssignmentService
from auto_assign.settings import Settings


class FakeAssistXClient(AssistXClient):
    async def health(self):
        return {"reachable": True, "brain": "neo4j_via_assistx"}

    async def get_task(self, task_id: str):
        return None

    async def get_backlog_candidates(self, limit: int = 25):
        return [AssignmentCandidate(task_id=f"ASS-{index}") for index in range(min(limit, 2))]

    async def event_status(self, idempotency_key: str):
        return {"known": False}


class FakeRouterClient(RouterClient):
    async def health(self):
        return {"reachable": True}

    async def snapshot(self):
        return RouterSnapshot(reachable=True, context_revision="test-rev")


def make_client(tmp_path):
    settings = Settings(database_url=f"sqlite:///{tmp_path / 'control.sqlite3'}")
    app.state.assignment_service = AssignmentService(
        settings=settings,
        cache=CacheStore(settings.sqlite_path),
        assistx=FakeAssistXClient(settings),
        router=FakeRouterClient(settings),
        scorer=AssignmentScorer(settings),
    )
    return TestClient(app)


def set_control(client: TestClient, mode: str) -> None:
    response = client.post(
        "/api/assignment-control",
        json={"mode": mode, "reason": f"test {mode}", "updated_by": "test"},
    )
    assert response.status_code == 200


def test_paused_control_blocks_new_assignment_evaluation(tmp_path):
    client = make_client(tmp_path)
    set_control(client, "paused")

    response = client.post(
        "/api/assignments/evaluate",
        json={"task_id": "ASS-paused", "dry_run": True, "candidate_lanes": ["paperclip"]},
    )

    assert response.status_code == 200
    payload = response.json()
    assert payload["status"] == "blocked"
    assert payload["selected_lane"] == "blocked"
    assert payload["canonical_status"] == "paused"
    assert any("assignment control mode is paused" in reason for reason in payload["reasons"])


def test_maintenance_control_blocks_new_assignment_evaluation(tmp_path):
    client = make_client(tmp_path)
    set_control(client, "maintenance")

    response = client.post(
        "/api/assignments/evaluate",
        json={"task_id": "ASS-maintenance", "dry_run": True, "candidate_lanes": ["paperclip"]},
    )

    assert response.status_code == 200
    payload = response.json()
    assert payload["status"] == "blocked"
    assert payload["selected_lane"] == "blocked"
    assert payload["canonical_status"] == "maintenance"


def test_draining_control_blocks_scheduler_tick_without_evaluating_candidates(tmp_path):
    client = make_client(tmp_path)
    set_control(client, "draining")

    response = client.post("/api/scheduler/tick", json={"dry_run": True, "limit": 2})

    assert response.status_code == 200
    payload = response.json()
    assert payload["evaluated"] == 0
    assert payload["recommended"] == 0
    assert payload["approval_required"] == 0
    assert payload["skipped"] == 0
    assert payload["decisions"] == []

    runs = client.get("/api/scheduler/runs").json()["runs"]
    assert len(runs) == 1
    assert "blocked_by_assignment_control:draining" in runs[0]["trigger_reason"]
    assert "assignment control mode draining blocks scheduler ticks" in runs[0]["error_summary"]


def test_enabled_control_allows_scheduler_tick(tmp_path):
    client = make_client(tmp_path)
    set_control(client, "enabled")

    response = client.post("/api/scheduler/tick", json={"dry_run": True, "limit": 2})

    assert response.status_code == 200
    payload = response.json()
    assert payload["evaluated"] == 2
    assert payload["recommended"] == 2
