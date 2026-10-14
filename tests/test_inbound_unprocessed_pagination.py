"""Synthetic-only regression for inbound event selection fairness.

All events, receipts, and processing markers are created in a disposable
SQLite database. No live receiver, graph, HTTP requests or admission calls.
"""
from __future__ import annotations

import asyncio
from types import SimpleNamespace

import pytest

from auto_assign.cache import CacheStore
from auto_assign.models import EventEnvelope, Lane
from auto_assign.service import AssignmentService


EVENT = "task.candidate.created"


def _setup(tmp_path):
    cache = CacheStore(tmp_path / "synthetic-inbound-fairness.sqlite")
    # Three older events have no processing record. A hundred newer events
    # are already processed, which fills the old newest-first LIMIT window.
    for idx in range(103):
        key = f"assistx-task-created:synthetic-{idx:03d}"
        event = EventEnvelope(
            event_id=f"evt_synthetic_{idx:03d}",
            event_type=EVENT,
            source_service="assistx",
            idempotency_key=key,
            subject={"task_id": f"synthetic-{idx:03d}"},
            payload={"task_id": f"synthetic-{idx:03d}"},
        )
        cache.record_inbound_event(event)
        with cache.connect() as conn:
            conn.execute(
                "UPDATE inbound_events SET received_at=? WHERE idempotency_key=?",
                (f"2026-08-23T00:{idx // 60:02d}:{idx % 60:02d}+00:00", key),
            )
            if idx >= 3:
                conn.execute(
                    "INSERT INTO inbound_event_processing "
                    "(idempotency_key,event_id,event_type,status,action_json,"
                    "processed_at,updated_at) VALUES(?,?,?,?,?,?,?)",
                    (
                        key, event.event_id, EVENT, "assignment_evaluated",
                        '{"action":"assignment_evaluated","dry_run":true}',
                        "2026-08-24T00:00:00+00:00",
                        "2026-08-24T00:00:00+00:00",
                    ),
                )
    return cache


def test_old_newest_first_limit_is_not_eligible_event_pagination(tmp_path):
    cache = _setup(tmp_path)
    try:
        newest = cache.list_inbound_events(limit=25, event_type=EVENT)
        assert len(newest) == 25
        assert all(
            int(row["payload"]["payload"]["task_id"].rsplit("-", 1)[1]) >= 3
            for row in newest
        )
        # Old behavior would skip all 25 and perform zero useful work.
        eligible = cache.list_unprocessed_inbound_events(limit=25, event_type=EVENT)
        assert [
            row["payload"]["payload"]["task_id"] for row in eligible
        ] == ["synthetic-000", "synthetic-001", "synthetic-002"]
    finally:
        cache.close()


def test_selector_limit_and_event_type_are_parameterized(tmp_path):
    cache = _setup(tmp_path)
    try:
        first = cache.list_unprocessed_inbound_events(limit=2, event_type=EVENT)
        assert len(first) == 2
        assert first[0]["idempotency_key"].endswith("synthetic-000")
        assert first[1]["idempotency_key"].endswith("synthetic-001")
        assert cache.list_unprocessed_inbound_events(event_type="no-match") == []
        assert len(cache.list_inbound_events(limit=25)) == 25
    finally:
        cache.close()


@pytest.mark.parametrize("bad_limit", [True, 0, -1, 501, "10"])
def test_invalid_pagination_limits_are_rejected(tmp_path, bad_limit):
    cache = _setup(tmp_path)
    try:
        with pytest.raises(ValueError, match="1..500"):
            cache.list_unprocessed_inbound_events(limit=bad_limit)
    finally:
        cache.close()


def test_selection_is_read_only_for_disposable_sqlite(tmp_path):
    cache = _setup(tmp_path)
    try:
        before = cache.connect().total_changes
        rows = cache.list_unprocessed_inbound_events(limit=25, event_type=EVENT)
        after = cache.connect().total_changes
        assert len(rows) == 3
        assert after == before
    finally:
        cache.close()


def test_synthetic_dry_run_processor_pages_oldest_eligible_first(tmp_path):
    cache = _setup(tmp_path)
    try:
        processor = object.__new__(AssignmentService)
        processor.cache = cache
        seen = []

        async def synthetic_evaluate(request):
            assert request.dry_run is True
            seen.append(request.task_id)
            return SimpleNamespace(
                assignment_id=f"synthetic-assignment-{request.task_id}",
                selected_lane=Lane.BLOCKED,
            )

        processor.evaluate = synthetic_evaluate
        first = asyncio.run(processor.process_inbound_events(
            event_type=EVENT, dry_run=True, limit=2,
        ))
        second = asyncio.run(processor.process_inbound_events(
            event_type=EVENT, dry_run=True, limit=2,
        ))
        assert first["processed"] == 2
        assert second["processed"] == 1
        assert seen == ["synthetic-000", "synthetic-001", "synthetic-002"]
        assert cache.list_unprocessed_inbound_events(limit=25, event_type=EVENT) == []
        # The dry-run evidence exists only in this disposable SQLite database.
    finally:
        cache.close()
