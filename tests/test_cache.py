from auto_assign.cache import CacheStore
from auto_assign.models import AssignmentDecision, AssignmentStatus, EventEnvelope, Lane


def test_cache_is_local_mirror_and_outbox(tmp_path):
    cache = CacheStore(tmp_path / "cache.sqlite3")
    decision = AssignmentDecision(
        assignment_id="assign_1",
        task_id="ASS-1",
        decision_id="decision_1",
        status=AssignmentStatus.RECOMMENDED,
        selected_lane=Lane.PAPERCLIP,
        selected_target="hermes_local",
        score=0.82,
        idempotency_key="assign.assignment.recommended:ASS-1:ready:unknown",
        cache_only=True,
    )

    cache.upsert_assignment(decision)
    rows = cache.list_assignments()

    assert rows[0]["cache_only"] is True
    assert rows[0]["task_id"] == "ASS-1"


def test_outbox_idempotency_key_dedupes_events(tmp_path):
    cache = CacheStore(tmp_path / "cache.sqlite3")
    event = EventEnvelope(
        event_type="assign.assignment.recommended",
        idempotency_key="same-key",
        subject="ASS-1",
        payload={"task_id": "ASS-1"},
    )

    cache.enqueue_event(event)
    cache.enqueue_event(event)

    assert cache.outbox_summary() == {"pending": 1}


def test_outbox_refreshes_pending_event_payload_for_same_idempotency_key(tmp_path):
    cache = CacheStore(tmp_path / "cache.sqlite3")
    first = EventEnvelope(
        event_type="assign.assignment.recommended",
        idempotency_key="refresh-key",
        subject="ASS-1",
        payload={"version": 1},
    )
    second = EventEnvelope(
        event_type="assign.assignment.recommended",
        idempotency_key="refresh-key",
        subject="ASS-1",
        payload={"version": 2},
    )

    cache.enqueue_event(first)
    cache.enqueue_event(second)

    events = cache.list_outbox_events()
    assert len(events) == 1
    assert events[0]["payload"]["payload"]["version"] == 2


def test_outbox_does_not_rewrite_delivered_event_payload(tmp_path):
    cache = CacheStore(tmp_path / "cache.sqlite3")
    first = EventEnvelope(
        event_type="assign.assignment.recommended",
        idempotency_key="delivered-key",
        subject="ASS-1",
        payload={"version": 1},
    )
    second = EventEnvelope(
        event_type="assign.assignment.recommended",
        idempotency_key="delivered-key",
        subject="ASS-1",
        payload={"version": 2},
    )

    cache.enqueue_event(first)
    cache.mark_event_delivered("delivered-key")
    cache.enqueue_event(second)

    events = cache.list_outbox_events()
    assert len(events) == 1
    assert events[0]["status"] == "delivered"
    assert events[0]["payload"]["payload"]["version"] == 1


def test_cache_file_deletion_does_not_define_canonical_history(tmp_path):
    path = tmp_path / "cache.sqlite3"
    cache = CacheStore(path)
    cache.enqueue_event(
        EventEnvelope(
            event_type="assign.assignment.recommended",
            idempotency_key="delete-safe",
            subject="ASS-2",
            payload={"cache_role": "outbox_replay_buffer"},
        )
    )
    assert cache.outbox_summary() == {"pending": 1}

    path.unlink()
    rebuilt = CacheStore(path)

    assert rebuilt.outbox_summary() == {}
    assert rebuilt.health()["role"] == "cache_outbox_only"
