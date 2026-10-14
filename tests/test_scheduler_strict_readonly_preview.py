"""Scheduler dry-run must be a SQL- and side-effect-free preview.

All authority/score inputs use in-process fakes and disposable SQLite.
No real AssistX, Neo4j, Router, credential or production action is used.
"""
from __future__ import annotations

import pytest

from auto_assign.cache import CacheStore
from auto_assign.control import AssignmentControlStore
from auto_assign.models import (
    AssignmentCandidate,
    AssignmentControlMode,
    AssignmentControlRequest,
    RouterSnapshot,
    SchedulerTickRequest,
)
from auto_assign.scorer import AssignmentScorer
from auto_assign.service import AssignmentService
from auto_assign.settings import Settings


class FakeAssistX:
    def __init__(self):
        self.calls = []

    async def get_task(self, task_id):
        self.calls.append(("read_task", task_id))
        return AssignmentCandidate(task_id=task_id, status="READY")

    async def get_backlog_candidates(self, limit=25):
        return [AssignmentCandidate(task_id=f"synthetic-{i}") for i in range(min(limit, 2))]

    async def event_status(self, key):
        self.calls.append(("read_event_status", None))
        return {"known": False}


class FakeRouter:
    async def snapshot(self):
        return RouterSnapshot(reachable=True, context_revision="synthetic-preview")


def make_service(tmp_path):
    settings = Settings(database_url=f"sqlite:///{tmp_path / 'preview.sqlite3'}")
    cache = CacheStore(settings.sqlite_path)
    upstream = FakeAssistX()
    svc = AssignmentService(
        settings=settings, cache=cache, assistx=upstream,
        router=FakeRouter(), scorer=AssignmentScorer(settings),
    )
    return svc, upstream


def snapshot(svc):
    with svc.cache.connect() as connection:
        return "\n".join(connection.iterdump()), connection.total_changes


@pytest.mark.parametrize("task_ids", [[], ["synthetic-a"], ["synthetic-a", "synthetic-b"]])
async def test_ready_preview_keeps_all_local_state_unchanged(tmp_path, task_ids):
    svc, upstream = make_service(tmp_path)
    before = snapshot(svc)
    memory_before = svc.last_tick_at

    def illegal_expiry():
        raise AssertionError("preview must not expire a lease")

    svc.expire_stale_leases = illegal_expiry
    first = await svc.scheduler_tick(
        SchedulerTickRequest(dry_run=True, task_ids=task_ids, limit=2)
    )
    second = await svc.scheduler_tick(
        SchedulerTickRequest(dry_run=True, task_ids=task_ids, limit=2)
    )
    assert first.dry_run is True
    assert second.dry_run is True
    assert first.evaluated == second.evaluated == (2 if not task_ids else len(task_ids))
    assert first.released_expired == second.released_expired == 0
    assert first.decisions and second.decisions
    assert snapshot(svc) == before
    assert svc.last_tick_at == memory_before
    assert any(name == "read_task" for name, _ in upstream.calls)


async def test_empty_backlog_preview_does_not_create_run(tmp_path):
    svc, upstream = make_service(tmp_path)

    async def no_backlog(limit=25):
        return []

    upstream.get_backlog_candidates = no_backlog
    before = snapshot(svc)
    response = await svc.scheduler_tick(SchedulerTickRequest(dry_run=True))
    assert response.evaluated == 0
    assert response.decisions == []
    assert response.released_expired == 0
    assert snapshot(svc) == before


async def test_blocked_control_preview_is_read_only(tmp_path):
    svc, upstream = make_service(tmp_path)
    AssignmentControlStore(svc.settings.sqlite_path).set(
        AssignmentControlRequest(mode=AssignmentControlMode.PAUSED)
    )
    before = snapshot(svc)
    previous = svc.last_tick_at
    response = await svc.scheduler_tick(
        SchedulerTickRequest(dry_run=True, task_ids=["synthetic-a"])
    )
    assert response.evaluated == 0
    assert response.decisions == []
    assert snapshot(svc) == before
    assert svc.last_tick_at == previous
    assert upstream.calls == []


async def test_first_preview_does_not_initialize_control_schema(tmp_path):
    svc, _ = make_service(tmp_path)
    with svc.cache.connect() as conn:
        before_schema = conn.execute("PRAGMA schema_version").fetchone()[0]
        before = conn.execute(
            "SELECT count(*) FROM sqlite_master WHERE name='assignment_control'"
        ).fetchone()[0]
    assert before == 0
    initial = snapshot(svc)
    response = await svc.scheduler_tick(
        SchedulerTickRequest(dry_run=True, task_ids=["synthetic-a"])
    )
    assert response.evaluated == 1
    with svc.cache.connect() as conn:
        after_schema = conn.execute("PRAGMA schema_version").fetchone()[0]
        after = conn.execute(
            "SELECT count(*) FROM sqlite_master WHERE name='assignment_control'"
        ).fetchone()[0]
    assert (after_schema, after) == (before_schema, before)
    assert snapshot(svc) == initial


async def test_non_dry_run_keeps_existing_synthetic_persistence_contract(tmp_path):
    svc, _ = make_service(tmp_path)
    previous = snapshot(svc)
    response = await svc.scheduler_tick(
        SchedulerTickRequest(
            dry_run=False, task_ids=["synthetic-persist"], limit=1
        )
    )
    assert response.dry_run is False
    assert response.evaluated == 1
    assert snapshot(svc) != previous
    with svc.cache.connect() as conn:
        assert conn.execute("SELECT count(*) FROM assignments").fetchone()[0] == 1
        assert conn.execute("SELECT count(*) FROM outbox_events").fetchone()[0] == 1
        assert conn.execute("SELECT count(*) FROM scheduler_runs").fetchone()[0] == 1


@pytest.mark.parametrize("mode", [AssignmentControlMode.DRAINING, AssignmentControlMode.PAUSED])
async def test_controlled_non_dry_run_still_records_blocked_run(tmp_path, mode):
    svc, _ = make_service(tmp_path)
    AssignmentControlStore(svc.settings.sqlite_path).set(
        AssignmentControlRequest(mode=mode)
    )
    response = await svc.scheduler_tick(
        SchedulerTickRequest(dry_run=False, task_ids=["synthetic-not-run"])
    )
    assert response.evaluated == 0
    assert len(svc.list_scheduler_runs()) == 1
