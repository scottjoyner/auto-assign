"""Hermetic contract tests for the AssistX event-status delivery fence.

A status of ``{"known": True}`` means AssistX has *seen* the event; it does not
mean the event was applied to the canonical graph.  Only an explicit boolean
``applied: True`` may move a local outbox row to ``delivered``.

All state is an in-memory stub plus a SQLite cache under pytest ``tmp_path``.
No network, service, credential, live outbox, or model is used.
"""

from __future__ import annotations

import asyncio

from auto_assign.cache import CacheStore
from auto_assign.events import EventType
from auto_assign.models import EventEnvelope
from auto_assign.service import AssignmentService
from auto_assign.settings import Settings


class StubAssistX:
    """Minimal event-status / post-event stub; no httpx client is created."""

    def __init__(self, statuses: dict[str, dict]):
        self.statuses = statuses
        self.posted: list[str] = []

    async def event_status(self, idempotency_key: str) -> dict:
        return self.statuses.get(idempotency_key, {"known": False})

    async def post_event(self, event, dry_run: bool = True) -> dict:
        self.posted.append(event.idempotency_key)
        return {"delivered": True, "status_code": 202}

    async def close(self) -> None:
        return None


def _service(tmp_path, status: dict):
    settings = Settings(database_url=f"sqlite:///{tmp_path / 'fence.sqlite3'}")
    cache = CacheStore(settings.sqlite_path)
    assistx = StubAssistX({"key-1": status})
    service = AssignmentService(settings, cache, assistx, None, None)  # type: ignore[arg-type]
    cache.enqueue_event(
        EventEnvelope(
            event_type=EventType.ASSIGNMENT_RECOMMENDED,
            idempotency_key="key-1",
            subject={"kind": "task", "id": "T-fence"},
            payload={"decision": "pin"},
        )
    )
    return service


def test_dispatch_known_without_applied_stays_pending(tmp_path):
    service = _service(tmp_path, {"known": True})

    result = asyncio.run(service.dispatch_outbox(dry_run=True))

    assert result["delivered"] == 0
    assert result["summary"] == {"pending": 1}
    assert [event["idempotency_key"] for event in result["dry_run_events"]] == ["key-1"]


def test_reconcile_known_without_applied_is_not_delivered(tmp_path):
    service = _service(tmp_path, {"known": True})

    result = asyncio.run(service.reconcile_outbox())

    assert result["reconciled_delivered"] == 0
    assert result["summary"] == {"pending": 1}


def test_reconcile_string_applied_is_not_confirmation(tmp_path):
    service = _service(tmp_path, {"known": True, "applied": "true"})

    result = asyncio.run(service.reconcile_outbox())

    assert result["reconciled_delivered"] == 0
    assert result["summary"] == {"pending": 1}


def test_reconcile_conflict_without_applied_is_dead_lettered(tmp_path):
    service = _service(tmp_path, {"known": True, "conflict": True})

    result = asyncio.run(service.reconcile_outbox())

    assert result["reconciled_delivered"] == 0
    assert result["conflicts_dead_lettered"] == 1
    assert result["summary"] == {"dead_letter": 1}


def test_dispatch_explicit_applied_true_is_delivered(tmp_path):
    service = _service(tmp_path, {"known": True, "applied": True})

    result = asyncio.run(service.dispatch_outbox(dry_run=True))

    assert result["delivered"] == 1
    assert result["summary"] == {"delivered": 1}


def test_reconcile_explicit_applied_true_is_delivered(tmp_path):
    service = _service(tmp_path, {"known": True, "applied": True})

    result = asyncio.run(service.reconcile_outbox())

    assert result["reconciled_delivered"] == 1
    assert result["summary"] == {"delivered": 1}


def test_reconcile_unknown_event_stays_pending(tmp_path):
    service = _service(tmp_path, {"known": False})

    result = asyncio.run(service.reconcile_outbox())

    assert result["reconciled_delivered"] == 0
    assert result["summary"] == {"pending": 1}


def test_reconcile_explicit_applied_false_stays_pending(tmp_path):
    service = _service(tmp_path, {"known": True, "applied": False})

    result = asyncio.run(service.reconcile_outbox())

    assert result["reconciled_delivered"] == 0
    assert result["summary"] == {"pending": 1}
