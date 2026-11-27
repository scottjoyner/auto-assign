"""Hermetic regression tests for inbound-event dedup status handling.

`AssignmentControlStore`-style local caches are mirrors; an inbound event whose
processor produced a non-success action (`skipped`, `ignored`) must not become a
permanent dedup tombstone.  Otherwise a corrected redelivery carrying the same
idempotency key is silently discarded (fail-open dedup gap).

All I/O is local SQLite under ``tmp_path`` with fake AssistX/Router clients --
no live HTTP, service, DB, credential or network-mount access.
"""

from fastapi.testclient import TestClient

from auto_assign.cache import CacheStore
from auto_assign.clients import AssistXClient, RouterClient
from auto_assign.main import app
from auto_assign.models import AssignmentCandidate, RouterSnapshot
from auto_assign.scorer import AssignmentScorer
from auto_assign.service import AssignmentService
from auto_assign.settings import Settings

KEY = "task.candidate.created:ASS-recovered"


class FakeAssistXClient(AssistXClient):
    async def health(self):
        return {"reachable": True, "brain": "neo4j_via_assistx"}

    async def get_task(self, task_id: str):
        return None

    async def get_backlog_candidates(self, limit: int = 25):
        return [AssignmentCandidate(task_id=f"ASS-{i}") for i in range(min(limit, 2))]

    async def event_status(self, idempotency_key: str):
        return {"known": False}

    async def post_event(self, event, dry_run: bool = True):
        return {"delivered": False, "dry_run": True}


class FakeRouterClient(RouterClient):
    async def health(self):
        return {"reachable": True}

    async def snapshot(self):
        return RouterSnapshot(reachable=True, context_revision="test-rev")


def make_client(tmp_path):
    # Synthetic in-process TestClient bypass only (mirrors the repo's own tests).
    settings = Settings(
        database_url=f"sqlite:///{tmp_path / 'inbound.sqlite3'}",
        allow_unauthenticated_local_dev=True,
    )
    app.state.assignment_service = AssignmentService(
        settings=settings,
        cache=CacheStore(settings.sqlite_path),
        assistx=FakeAssistXClient(settings),
        router=FakeRouterClient(settings),
        scorer=AssignmentScorer(settings),
    )
    return TestClient(app)


def ingest(client: TestClient, event_type: str, key: str, payload: dict, subject: str = "") -> None:
    response = client.post(
        "/api/events",
        json={
            "event_type": event_type,
            "idempotency_key": key,
            "subject": subject,
            "payload": payload,
        },
    )
    assert response.status_code == 200


def process(client: TestClient, event_type: str | None = None) -> dict:
    url = "/api/events/process?dry_run=true"
    if event_type:
        url += f"&event_type={event_type}"
    response = client.post(url)
    assert response.status_code == 200
    return response.json()


def test_skipped_event_is_reprocessed_after_corrected_redelivery(tmp_path):
    client = make_client(tmp_path)

    # First delivery carries no task_id -> processor records "skipped".
    ingest(client, "task.candidate.created", KEY, {})
    first = process(client, "task.candidate.created")
    assert first["processed"] == 1
    assert first["actions"][0]["action"] == "skipped"
    assert first["actions"][0]["reason"] == "missing_task_id"

    # Corrected redelivery: same stable idempotency key, now with task_id.
    ingest(client, "task.candidate.created", KEY, {"task_id": "ASS-recovered"})
    second = process(client, "task.candidate.created")

    # Pre-fix: 0 processed / 1 skipped_already_processed -- the skip tombstone
    # permanently suppressed the corrected event (the fail-open defect).
    assert second["processed"] == 1
    assert second["skipped_already_processed"] == 0
    assert second["actions"][0]["action"] == "assignment_evaluated"
    assert second["actions"][0]["task_id"] == "ASS-recovered"
    assert client.get("/api/outbox/summary").json()["summary"] == {"pending": 1}


def test_ignored_event_is_not_treated_as_processed(tmp_path):
    client = make_client(tmp_path)
    key = "unknown.kind:ASS-no-processor"
    ingest(client, "unknown.kind", key, {"task_id": "ASS-recovered"})

    first = process(client)
    assert first["processed"] == 1
    assert first["actions"][0]["action"] == "ignored"
    assert first["actions"][0]["reason"] == "no_processor_registered"

    service = app.state.assignment_service
    # Pre-fix: returns True even though the status is "ignored".
    assert service._inbound_event_was_processed(key) is False


def test_success_statuses_still_dedupe(tmp_path):
    client = make_client(tmp_path)
    key = "task.candidate.created:ASS-dedupe"
    ingest(client, "task.candidate.created", key, {"task_id": "ASS-dedupe"})

    assert process(client, "task.candidate.created")["processed"] == 1
    second = process(client, "task.candidate.created")
    assert second["processed"] == 0
    assert second["skipped_already_processed"] == 1
