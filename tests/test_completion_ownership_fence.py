"""Hermetic fence: completion must come from the worker that owns a running lease.

No live AssistX/Router/SQLite state is read: the cache is a fresh SQLite file
under ``tmp_path`` and the two clients are in-process stubs.
"""

from __future__ import annotations

import asyncio
from pathlib import Path

from auto_assign.cache import CacheStore
from auto_assign.events import EventType
from auto_assign.models import (
    AssignmentCompletionRequest,
    AssignmentDecision,
    AssignmentStatus,
    Lane,
    RouterSnapshot,
)
from auto_assign.scorer import AssignmentScorer
from auto_assign.service import AssignmentService
from auto_assign.settings import Settings

ASSIGNMENT_ID = "ASS-completion-fence"
TASK_ID = "T-completion-fence"
OWNER = "worker-owner"
INTRUDER = "worker-intruder"
CORRELATION_ID = "2f1c5e64-9b0a-4d1f-8f7e-3a6b1c2d9e01"


class _StubClient:
    """No network: the completion path never calls these methods."""

    async def get_task(self, task_id: str):
        return None

    async def event_status(self, idempotency_key: str):
        return {"known": False}

    async def snapshot(self):
        return RouterSnapshot(reachable=True)


def asyncio_run(coro):  # tiny local helper; no event-loop policy changes
    return asyncio.run(coro)


def _service(tmp_path: Path) -> AssignmentService:
    settings = Settings(database_url=f"sqlite:///{tmp_path / 'completion.sqlite3'}")
    return AssignmentService(
        settings=settings,
        cache=CacheStore(settings.sqlite_path),
        assistx=_StubClient(),  # type: ignore[arg-type]
        router=_StubClient(),  # type: ignore[arg-type]
        scorer=AssignmentScorer(settings),
    )


def _seed_assignment(service: AssignmentService, status: str, worker_id: str | None) -> None:
    decision = AssignmentDecision(
        assignment_id=ASSIGNMENT_ID,
        task_id=TASK_ID,
        decision_id="decision-completion-fence",
        status=AssignmentStatus(status),
        selected_lane=Lane.LOCAL_ONLY,
        idempotency_key="decision-completion-fence",
    )
    service.cache.upsert_assignment(decision)
    if worker_id is not None:
        service.cache.update_assignment_status(
            ASSIGNMENT_ID, status=status, worker_id=worker_id
        )


def _request(worker_id: str) -> AssignmentCompletionRequest:
    return AssignmentCompletionRequest(
        correlation_id=CORRELATION_ID,
        assignment_id=ASSIGNMENT_ID,
        task_id=TASK_ID,
        worker_id=worker_id,
        status="success",
    )


def _completion_events(service: AssignmentService) -> list[dict]:
    return [
        row
        for row in service.cache.list_outbox_events()
        if row["event_type"] == EventType.ASSIGNMENT_COMPLETED
    ]


def _db_status(service: AssignmentService) -> str:
    with service.cache.connect() as conn:
        return conn.execute(
            "SELECT status FROM assignments WHERE assignment_id = ?", (ASSIGNMENT_ID,)
        ).fetchone()["status"]


def test_unclaimed_assignment_cannot_be_completed(tmp_path: Path) -> None:
    service = _service(tmp_path)
    _seed_assignment(service, "recommended", None)

    result = asyncio_run(service.complete_assignment(ASSIGNMENT_ID, _request(INTRUDER)))

    assert result["accepted"] is False
    assert _db_status(service) == "recommended"
    assert _completion_events(service) == []


def test_other_worker_cannot_complete_a_running_assignment(tmp_path: Path) -> None:
    service = _service(tmp_path)
    _seed_assignment(service, "running", OWNER)

    result = asyncio_run(service.complete_assignment(ASSIGNMENT_ID, _request(INTRUDER)))

    assert result["accepted"] is False
    assert _db_status(service) == "running"
    assert _completion_events(service) == []


def test_owner_can_still_complete_a_running_assignment(tmp_path: Path) -> None:
    service = _service(tmp_path)
    _seed_assignment(service, "running", OWNER)

    result = asyncio_run(service.complete_assignment(ASSIGNMENT_ID, _request(OWNER)))

    assert result["accepted"] is True
    assert _db_status(service) == "done"
    assert len(_completion_events(service)) == 1
