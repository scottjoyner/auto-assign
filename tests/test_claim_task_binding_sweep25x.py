"""Hermetic contract tests: claim/completion must bind body task_id to the assignment.

sweep25x — no network, no live AssistX/outbox, only an in-process SQLite cache
under pytest's scratch tmp_path and object() stubs for the unused clients.
"""

from __future__ import annotations

from uuid import uuid4

from auto_assign.cache import CacheStore
from auto_assign.events import EventType
from auto_assign.models import (
    AssignmentCandidate,
    AssignmentClaimRequest,
    AssignmentCompletionRequest,
    RouterSnapshot,
)
from auto_assign.scorer import AssignmentScorer
from auto_assign.service import AssignmentService
from auto_assign.settings import Settings


def _seeded(tmp_path, task_id: str = "ASS-real"):
    settings = Settings(database_url=f"sqlite:///{tmp_path / 'binding.sqlite3'}")
    cache = CacheStore(settings.sqlite_path)
    scorer = AssignmentScorer(settings)
    decision = scorer.score(AssignmentCandidate(task_id=task_id), RouterSnapshot(reachable=True))
    cache.upsert_assignment(decision)
    svc = AssignmentService(settings, cache, object(), object(), scorer)
    return svc, cache, decision.assignment_id


def _column_status(cache: CacheStore, assignment_id: str) -> str:
    """Authoritative SQL status column (the payload_json mirror is a separate
    pre-existing concern and deliberately not exercised by this suite)."""
    row = (
        cache.connect()
        .execute("SELECT status FROM assignments WHERE assignment_id=?", (assignment_id,))
        .fetchone()
    )
    return row["status"]


def _claim(task_id: str, worker_id: str = "worker-1") -> AssignmentClaimRequest:
    return AssignmentClaimRequest(
        correlation_id=str(uuid4()),
        task_id=task_id,
        worker_id=worker_id,
        node_id="node-1",
    )


def _complete(
    assignment_id: str, task_id: str, worker_id: str = "worker-1"
) -> AssignmentCompletionRequest:
    return AssignmentCompletionRequest(
        correlation_id=str(uuid4()),
        assignment_id=assignment_id,
        task_id=task_id,
        worker_id=worker_id,
        status="success",
    )


async def test_claim_with_foreign_task_id_is_rejected(tmp_path):
    svc, cache, assignment_id = _seeded(tmp_path)

    result = await svc.claim_assignment(assignment_id, _claim("ASS-foreign"))

    assert result["accepted"] is False, "a claim must not bind another task to this assignment"
    assert "task_id" in result.get("reason", "")
    assert _column_status(cache, assignment_id) == "recommended"
    claimed = [
        e for e in cache.list_outbox_events() if e["event_type"] == EventType.ASSIGNMENT_CLAIMED
    ]
    assert claimed == [], "a rejected claim must not emit a canonical claimed event"


async def test_complete_with_foreign_task_id_is_rejected(tmp_path):
    svc, cache, assignment_id = _seeded(tmp_path)
    assert (await svc.claim_assignment(assignment_id, _claim("ASS-real")))["accepted"] is True

    result = await svc.complete_assignment(assignment_id, _complete(assignment_id, "ASS-foreign"))

    assert result["accepted"] is False, "a completion must not bind another task to this assignment"
    assert "task_id" in result.get("reason", "")
    assert _column_status(cache, assignment_id) == "running"
    completed = [
        e for e in cache.list_outbox_events() if e["event_type"] == EventType.ASSIGNMENT_COMPLETED
    ]
    assert completed == [], "a rejected completion must not emit a canonical completed event"


async def test_matching_claim_is_still_accepted(tmp_path):
    svc, cache, assignment_id = _seeded(tmp_path)

    result = await svc.claim_assignment(assignment_id, _claim("ASS-real"))

    assert result["accepted"] is True
    assert _column_status(cache, assignment_id) == "running"


async def test_matching_completion_is_still_accepted(tmp_path):
    svc, cache, assignment_id = _seeded(tmp_path)
    await svc.claim_assignment(assignment_id, _claim("ASS-real"))

    result = await svc.complete_assignment(assignment_id, _complete(assignment_id, "ASS-real"))

    assert result["accepted"] is True
    assert result["status"] == "done"
    assert _column_status(cache, assignment_id) == "done"


async def test_unknown_assignment_is_still_rejected(tmp_path):
    svc, _cache, _assignment_id = _seeded(tmp_path)

    result = await svc.claim_assignment("does-not-exist", _claim("ASS-real"))

    assert result["accepted"] is False
    assert result["reason"] == "assignment_not_found_in_local_cache"
