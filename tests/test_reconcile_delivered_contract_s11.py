"""Hermetic regression tests for the reconcile_delivered() reconciliation count.

The count returned by CacheStore.reconcile_delivered() feeds
AssignmentService.reconcile_outbox()'s ``reconciled_delivered`` API field. It
must report rows actually transitioned to delivered, not the number of keys
handed to it. All state is a tmp_path SQLite file: no network, no live DB.
"""

from auto_assign.cache import CacheStore
from auto_assign.models import EventEnvelope


def _event(key: str, version: int = 1) -> EventEnvelope:
    return EventEnvelope(
        event_type="assign.assignment.recommended",
        idempotency_key=key,
        subject="ASS-1",
        payload={"version": version},
    )


def test_ghost_key_is_not_counted_as_reconciled(tmp_path):
    cache = CacheStore(tmp_path / "cache.sqlite3")
    cache.enqueue_event(_event("real-key"))

    assert cache.reconcile_delivered(["ghost-key"]) == 0
    assert cache.outbox_summary() == {"pending": 1}

    assert cache.reconcile_delivered(["real-key"]) == 1
    assert cache.outbox_summary() == {"delivered": 1}

    # A second reconciliation of a row already delivered is a no-op.
    assert cache.reconcile_delivered(["real-key"]) == 0
    assert cache.outbox_summary() == {"delivered": 1}


def test_repeated_keys_are_deduped_before_counting(tmp_path):
    cache = CacheStore(tmp_path / "cache.sqlite3")
    cache.enqueue_event(_event("dup-key"))

    assert cache.reconcile_delivered(["dup-key", "dup-key", "dup-key"]) == 1
    assert cache.outbox_summary() == {"delivered": 1}


def test_mixed_keys_count_only_real_transitions(tmp_path):
    cache = CacheStore(tmp_path / "cache.sqlite3")
    cache.enqueue_event(_event("key-1"))
    cache.enqueue_event(_event("key-2"))

    assert cache.reconcile_delivered(["key-1", "missing", "key-2", "key-1"]) == 2
    assert cache.outbox_summary() == {"delivered": 2}
