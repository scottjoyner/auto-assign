"""Direct assignment dry-run has zero local persistence effects.

Synthetic canonical Task and Router responses only; disposable SQLite;
no real AssistX, outbound requests, admission, credentials or graph writes.
"""
from __future__ import annotations

import pytest

from auto_assign.cache import CacheStore
from auto_assign.control import AssignmentControlStore
from auto_assign.models import (
    AssignmentCandidate,
    AssignmentControlMode,
    AssignmentControlRequest,
    AssignmentEvaluateRequest,
    RouterSnapshot,
)
from auto_assign.scorer import AssignmentScorer
from auto_assign.service import AssignmentService
from auto_assign.settings import Settings


class VerifiedSyntheticTask:
    async def get_task(self, task_id):
        return AssignmentCandidate(task_id=task_id, status="READY")

    async def event_status(self, _key):
        return {"known": False}


class SyntheticRouter:
    async def snapshot(self):
        return RouterSnapshot(reachable=True, context_revision="synthetic")


def service_at(tmp_path):
    settings = Settings(database_url=f"sqlite:///{tmp_path / 'evaluate-preview.sqlite3'}")
    svc = AssignmentService(
        settings=settings,
        cache=CacheStore(settings.sqlite_path),
        assistx=VerifiedSyntheticTask(),
        router=SyntheticRouter(),
        scorer=AssignmentScorer(settings),
    )
    return svc


def database_witness(svc):
    with svc.cache.connect() as conn:
        schema_version = conn.execute("PRAGMA schema_version").fetchone()[0]
        return (
            "\n".join(conn.iterdump()),
            conn.total_changes,
            schema_version,
        )


@pytest.mark.parametrize("task_id", ["synthetic-task-one", "synthetic-task-two"])
async def test_direct_preview_is_ephemeral_and_repeated_without_writes(
    tmp_path, task_id
):
    svc = service_at(tmp_path)
    before = database_witness(svc)
    first = await svc.evaluate(AssignmentEvaluateRequest(
        task_id=task_id, dry_run=True
    ))
    second = await svc.evaluate(AssignmentEvaluateRequest(
        task_id=task_id, dry_run=True
    ))
    assert first.task_id == second.task_id == task_id
    assert first.cache_only is True
    assert database_witness(svc) == before


@pytest.mark.parametrize("mode", [AssignmentControlMode.PAUSED, AssignmentControlMode.MAINTENANCE])
async def test_blocked_preview_does_not_enqueue_or_upsert(tmp_path, mode):
    svc = service_at(tmp_path)
    AssignmentControlStore(svc.settings.sqlite_path).set(
        AssignmentControlRequest(mode=mode)
    )
    before = database_witness(svc)
    decision = await svc.evaluate(AssignmentEvaluateRequest(
        task_id="synthetic-paused", dry_run=True
    ))
    assert decision.status == "blocked"
    assert database_witness(svc) == before


async def test_first_preview_does_not_create_control_table(tmp_path):
    svc = service_at(tmp_path)
    with svc.cache.connect() as conn:
        assert conn.execute(
            "SELECT count(*) FROM sqlite_master WHERE name='assignment_control'"
        ).fetchone()[0] == 0
    before = database_witness(svc)
    decision = await svc.evaluate(AssignmentEvaluateRequest(
        task_id="synthetic-first-preview", dry_run=True
    ))
    assert decision.task_id == "synthetic-first-preview"
    assert database_witness(svc) == before


async def test_non_dry_run_explicitly_preserves_write_contract(tmp_path):
    svc = service_at(tmp_path)
    before = database_witness(svc)
    decision = await svc.evaluate(AssignmentEvaluateRequest(
        task_id="synthetic-committed", dry_run=False
    ))
    assert decision.task_id == "synthetic-committed"
    assert database_witness(svc) != before
    with svc.cache.connect() as conn:
        assert conn.execute("SELECT count(*) FROM assignments").fetchone()[0] == 1
        assert conn.execute("SELECT count(*) FROM outbox_events").fetchone()[0] == 1


async def test_blocked_non_dry_run_preserves_auditable_blocked_record(tmp_path):
    svc = service_at(tmp_path)
    AssignmentControlStore(svc.settings.sqlite_path).set(
        AssignmentControlRequest(mode=AssignmentControlMode.PAUSED)
    )
    decision = await svc.evaluate(AssignmentEvaluateRequest(
        task_id="synthetic-blocked-persist", dry_run=False
    ))
    assert decision.status == "blocked"
    with svc.cache.connect() as conn:
        assert conn.execute("SELECT count(*) FROM assignments").fetchone()[0] == 1
        assert conn.execute("SELECT count(*) FROM outbox_events").fetchone()[0] == 1
