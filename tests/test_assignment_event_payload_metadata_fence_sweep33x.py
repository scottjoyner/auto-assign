"""Sweep33x: caller metadata must not overwrite authoritative claim/completion context.

The local cache is updated from trusted service state, but the canonical
``assign.claimed`` / ``assign.completed`` event is what AssistX/Neo4j applies.
``**request.metadata`` is currently spread last into that event payload, so a
caller can forge status, worker, assignment/task identity and lease fields while
the local mirror keeps the truth. These tests pin the boundary with an in-process
SQLite cache and stub clients only -- no HTTP, DB service, model or live outbox.
"""

from __future__ import annotations

from auto_assign.cache import CacheStore
from auto_assign.clients import AssistXClient, RouterClient
from auto_assign.models import (
    AssignmentCandidate,
    AssignmentClaimRequest,
    AssignmentCompletionRequest,
    RouterSnapshot,
    correlation_for_task,
)
from auto_assign.scorer import AssignmentScorer
from auto_assign.service import AssignmentService
from auto_assign.settings import Settings

TASK_ID = "ASS-sweep33x-real"
WORKER = "worker-real"
NODE = "x1-370"
FORGED_ASSIGNMENT = "assign-forged"


class _StubAssistX(AssistXClient):
    def __init__(self, settings: Settings):
        super().__init__(settings)

    async def close(self) -> None:
        return None

    async def health(self):
        return {"reachable": True, "brain": "neo4j_via_assistx"}

    async def event_status(self, idempotency_key: str):
        return {"known": False}


class _StubRouter(RouterClient):
    async def close(self) -> None:
        return None

    async def health(self):
        return {"reachable": True}

    async def snapshot(self):
        return RouterSnapshot(reachable=True, context_revision="sweep33x-rev")


def _service(tmp_path):
    settings = Settings(database_url=f"sqlite:///{tmp_path / 'sweep33x.sqlite3'}")
    return AssignmentService(
        settings=settings,
        cache=CacheStore(settings.sqlite_path),
        assistx=_StubAssistX(settings),
        router=_StubRouter(settings),
        scorer=AssignmentScorer(settings),
    )


def _seed_recommended(svc):
    decision = svc.scorer.score(
        AssignmentCandidate(task_id=TASK_ID), RouterSnapshot(reachable=True)
    )
    svc.cache.upsert_assignment(decision)
    return decision.assignment_id


def _claim_request(metadata: dict | None = None) -> AssignmentClaimRequest:
    return AssignmentClaimRequest(
        correlation_id=correlation_for_task(TASK_ID),
        task_id=TASK_ID,
        worker_id=WORKER,
        node_id=NODE,
        metadata=metadata or {},
    )


def _complete_request(metadata: dict | None = None) -> AssignmentCompletionRequest:
    return AssignmentCompletionRequest(
        correlation_id=correlation_for_task(TASK_ID),
        assignment_id="placeholder",
        task_id=TASK_ID,
        worker_id=WORKER,
        metadata=metadata or {},
    )


def _last_event_payload(svc, event_type: str | None = None) -> dict:
    rows = svc.cache.list_outbox_events(limit=10)
    if event_type:
        rows = [row for row in rows if row["event_type"] == event_type]
    assert rows, rows
    return rows[0]["payload"]["payload"]


class TestClaimMetadataFence:
    async def test_metadata_cannot_forge_status(self, tmp_path):
        svc = _service(tmp_path)
        aid = _seed_recommended(svc)
        await svc.claim_assignment(aid, _claim_request({"status": "done"}))
        assert svc.cache.get_assignment(aid)["status"] == "running"
        assert _last_event_payload(svc)["status"] == "running"

    async def test_metadata_cannot_forge_worker(self, tmp_path):
        svc = _service(tmp_path)
        aid = _seed_recommended(svc)
        await svc.claim_assignment(aid, _claim_request({"worker_id": "worker-attacker"}))
        assert _last_event_payload(svc)["worker_id"] == WORKER

    async def test_metadata_cannot_forge_assignment_identity(self, tmp_path):
        svc = _service(tmp_path)
        aid = _seed_recommended(svc)
        req = _claim_request({"assignment_id": FORGED_ASSIGNMENT})
        req.correlation_id = correlation_for_task(TASK_ID)
        await svc.claim_assignment(aid, req)
        payload = _last_event_payload(svc)
        assert payload["assignment_id"] == aid
        assert svc.cache.get_assignment(aid)["assignment_id"] == aid

    async def test_metadata_cannot_forge_lease(self, tmp_path):
        svc = _service(tmp_path)
        aid = _seed_recommended(svc)
        await svc.claim_assignment(
            aid, _claim_request({"lease_seconds": 1, "lease_expires_at": 0.0})
        )
        payload = _last_event_payload(svc)
        assert payload["lease_seconds"] == 900
        assert isinstance(payload["lease_expires_at"], float)
        assert payload["lease_expires_at"] > 0

    async def test_unknown_metadata_keys_are_preserved(self, tmp_path):
        svc = _service(tmp_path)
        aid = _seed_recommended(svc)
        await svc.claim_assignment(aid, _claim_request({"note": "keep-me", "custom_count": 7}))
        payload = _last_event_payload(svc)
        assert payload["note"] == "keep-me"
        assert payload["custom_count"] == 7
        assert payload["status"] == "running"
        assert payload["worker_id"] == WORKER

    async def test_no_metadata_claim_is_unchanged(self, tmp_path):
        svc = _service(tmp_path)
        aid = _seed_recommended(svc)
        await svc.claim_assignment(aid, _claim_request())
        payload = _last_event_payload(svc)
        assert payload["assignment_id"] == aid
        assert payload["worker_id"] == WORKER
        assert payload["task_id"] == TASK_ID
        assert payload["status"] == "running"


class TestCompletionMetadataFence:
    async def _claimed(self, tmp_path):
        svc = _service(tmp_path)
        aid = _seed_recommended(svc)
        await svc.claim_assignment(aid, _claim_request())
        assert len(svc.cache.list_outbox_events(limit=10)) == 1
        return svc, aid

    async def test_metadata_cannot_forge_completion_status(self, tmp_path):
        svc, aid = await self._claimed(tmp_path)
        req = _complete_request({"status": "running"})
        req.assignment_id = aid
        await svc.complete_assignment(aid, req)
        assert _last_event_payload(svc, "assignment.completed")["status"] == "success"

    async def test_metadata_cannot_forge_completion_worker(self, tmp_path):
        svc, aid = await self._claimed(tmp_path)
        req = _complete_request({"worker_id": "worker-attacker"})
        req.assignment_id = aid
        await svc.complete_assignment(aid, req)
        payload = _last_event_payload(svc, "assignment.completed")
        assert payload["worker_id"] == WORKER
        assert payload["assignment_id"] == aid

    async def test_no_metadata_completion_is_unchanged(self, tmp_path):
        svc, aid = await self._claimed(tmp_path)
        req = _complete_request()
        req.assignment_id = aid
        await svc.complete_assignment(aid, req)
        payload = _last_event_payload(svc, "assignment.completed")
        assert payload["status"] == "success"
        assert payload["worker_id"] == WORKER
        assert payload["task_id"] == TASK_ID
