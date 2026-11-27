"""Sweep32 — a dry-run scheduler tick must not consume expired leases.

Hermetic: in-process SQLite under pytest tmp_path, stubbed AssistX/Router
clients, no sockets, no services, no live outbox/DB/WAL, no credential reads.
"""

from __future__ import annotations

from datetime import timedelta

from auto_assign.cache import CacheStore
from auto_assign.clients import AssistXClient, RouterClient
from auto_assign.models import (
    AssignmentDecision,
    AssignmentStatus,
    Lane,
    RouterSnapshot,
    SchedulerTickRequest,
    utc_now,
)
from auto_assign.scorer import AssignmentScorer
from auto_assign.service import AssignmentService
from auto_assign.settings import Settings

LEASE_ID = "assign-sweep32-expired"
EXPIRED_EVENT = "assignment.expired"


class StubAssistX(AssistXClient):
    """No network client is constructed; only the scheduler's reads are stubbed."""

    def __init__(self) -> None:
        pass

    async def get_backlog_candidates(self, limit: int = 25):
        return []

    async def event_status(self, idempotency_key: str):
        return {"known": False}


class StubRouter(RouterClient):
    def __init__(self) -> None:
        pass

    async def snapshot(self):
        return RouterSnapshot(reachable=True, context_revision="sweep32-rev")


def make_service(tmp_path):
    settings = Settings(database_url=f"sqlite:///{tmp_path / 'sweep32.sqlite3'}")
    cache = CacheStore(settings.sqlite_path)
    service = AssignmentService(
        settings=settings,
        cache=cache,
        assistx=StubAssistX(),
        router=StubRouter(),
        scorer=AssignmentScorer(settings),
    )
    decision = AssignmentDecision(
        assignment_id=LEASE_ID,
        task_id="TASK-SWEEP32",
        decision_id="dec-sweep32",
        status=AssignmentStatus.RUNNING,
        selected_lane=Lane.LOCAL_ONLY,
        selected_target="worker-sweep32",
        score=0.9,
        idempotency_key="decision-sweep32",
    )
    cache.upsert_assignment(decision)
    # Lease already expired one minute ago; zero configured grace.
    with cache.connect() as conn:
        conn.execute(
            "UPDATE assignments SET lease_expires_at=? WHERE assignment_id=?",
            ((utc_now() - timedelta(seconds=60)).isoformat(), LEASE_ID),
        )
    return service


def column_status(service: AssignmentService) -> str:
    with service.cache.connect() as conn:
        row = conn.execute(
            "SELECT status FROM assignments WHERE assignment_id=?", (LEASE_ID,)
        ).fetchone()
    return row["status"]


def expired_event_count(service: AssignmentService) -> int:
    return sum(
        1 for event in service.cache.list_outbox_events() if event["event_type"] == EXPIRED_EVENT
    )


async def test_dry_run_tick_does_not_release_expired_lease(tmp_path):
    """The default (dry_run=True) tick must be a preview, not a write."""
    service = make_service(tmp_path)
    assert column_status(service) == "running"

    result = await service.scheduler_tick(SchedulerTickRequest(dry_run=True))

    assert column_status(service) == "running"
    assert expired_event_count(service) == 0
    assert result.released_expired == 1  # preview count is still reported


async def test_dry_run_direct_call_is_read_only(tmp_path):
    service = make_service(tmp_path)
    result = service.expire_stale_leases(dry_run=True)
    assert result["expired_count"] == 1
    assert result["events_emitted"] == 0
    assert column_status(service) == "running"
    assert expired_event_count(service) == 0


async def test_dry_run_does_not_consume_the_lease(tmp_path):
    """A live tick after a dry-run preview must still be able to release it."""
    service = make_service(tmp_path)
    await service.scheduler_tick(SchedulerTickRequest(dry_run=True))
    live = await service.scheduler_tick(SchedulerTickRequest(dry_run=False))
    assert live.released_expired == 1
    assert column_status(service) == "released"
    assert expired_event_count(service) == 1


async def test_live_tick_still_releases_and_emits(tmp_path):
    """Control: the non-dry-run path is unchanged by the fence."""
    service = make_service(tmp_path)
    result = await service.scheduler_tick(SchedulerTickRequest(dry_run=False))
    assert result.released_expired == 1
    assert column_status(service) == "released"
    assert expired_event_count(service) == 1
