"""Sweep33: inbound-event admission must reject malformed correlation IDs.

Hermetic offline tests — no HTTP, no network, no containers, no live services,
no credentials. A synthetic in-process SQLite cache and stub collaborators are
used. ``EventEnvelope`` (auto_assign.models) documents correlation_id as a
required valid UUID mirroring assistx.contracts, but does not enforce it, so
``AssignmentService.ingest_event``/``POST /api/events`` silently admitted
non-UUID correlation IDs into the durable inbound mirror.
"""

from __future__ import annotations

from types import SimpleNamespace
from uuid import uuid4

import pytest

from auto_assign.cache import CacheStore
from auto_assign.models import EventEnvelope
from auto_assign.service import AssignmentService


def _service(tmp_path):
    db_path = tmp_path / "inbound.sqlite3"
    cache = CacheStore(db_path)
    service = AssignmentService(
        settings=SimpleNamespace(
            sqlite_path=db_path,
            default_lease_seconds=900,
            stale_assignment_seconds=0,
        ),
        cache=cache,
        assistx=SimpleNamespace(),
        router=SimpleNamespace(),
        scorer=SimpleNamespace(),
    )
    return service, cache


def _event(correlation_id: str, key: str) -> EventEnvelope:
    return EventEnvelope(
        event_type="task.candidate.created",
        idempotency_key=key,
        subject="ASS-sweep33",
        correlation_id=correlation_id,
        payload={"task_id": "ASS-sweep33"},
    )


@pytest.mark.parametrize(
    "bad_correlation_id",
    [
        "not-a-uuid",
        "",
        "12345",
        "00000000-0000-0000-0000-00000000000",  # one hex digit short
        "corr-claim",
    ],
)
def test_ingest_rejects_non_uuid_correlation_id(tmp_path, bad_correlation_id):
    service, cache = _service(tmp_path)
    event = _event(bad_correlation_id, f"sweep33-bad-{bad_correlation_id!r}")

    result = service.ingest_event(event)

    assert result["accepted"] is False
    assert result["reason"] == "invalid_correlation_id"
    assert cache.list_inbound_events() == [], "malformed event must not enter the inbound mirror"


def test_ingest_accepts_valid_uuid_correlation_id(tmp_path):
    service, cache = _service(tmp_path)
    cid = str(uuid4())
    event = _event(cid, "sweep33-good-1")

    result = service.ingest_event(event)

    assert result["accepted"] is True
    rows = cache.list_inbound_events()
    assert len(rows) == 1
    assert rows[0]["payload"]["correlation_id"] == cid


def test_ingest_accepts_model_default_correlation_id(tmp_path):
    """Events that omit correlation_id get a valid UUID default and must pass."""
    service, cache = _service(tmp_path)
    event = EventEnvelope(
        event_type="task.candidate.created",
        idempotency_key="sweep33-good-default",
        subject="ASS-sweep33",
        payload={"task_id": "ASS-sweep33"},
    )

    result = service.ingest_event(event)

    assert result["accepted"] is True
    assert len(cache.list_inbound_events()) == 1
