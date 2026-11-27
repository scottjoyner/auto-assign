"""A late worker heartbeat must not revive a terminal assignment (sweep24x).

Hermetic: in-process SQLite in pytest's scratch tmp_path, object() stubs for
the two async clients (AssignmentService never calls them on this path).
No socket, HTTP, Redis, Neo4j, live DB/WAL, credential, service or mount.
"""

from __future__ import annotations

import pytest

from auto_assign.cache import CacheStore
from auto_assign.models import (
    AssignmentDecision,
    AssignmentStatus,
    HeartbeatRequest,
    Lane,
)
from auto_assign.service import AssignmentService
from auto_assign.settings import Settings

AID = "assign-terminal-sweep24x"


def _service(cache: CacheStore) -> AssignmentService:
    return AssignmentService(Settings(), cache, object(), object(), object())


def _column(cache: CacheStore, assignment_id: str = AID) -> tuple[str | None, str | None]:
    """Authoritative status column and lease, not the payload_json mirror."""
    with cache.connect() as conn:
        row = conn.execute(
            "SELECT status, lease_expires_at FROM assignments WHERE assignment_id=?",
            (assignment_id,),
        ).fetchone()
    return (row["status"], row["lease_expires_at"]) if row else (None, None)


def _seed(
    cache: CacheStore,
    *,
    authoritative_status: str = "running",
    payload_status: str = "running",
    worker_id: str | None = "worker-1",
    node_id: str | None = "node-1",
    assignment_id: str = AID,
) -> None:
    cache.upsert_assignment(
        AssignmentDecision(
            assignment_id=assignment_id,
            task_id="ASS-TERMINAL-SWEEP24X",
            decision_id="dec-terminal-sweep24x",
            status=AssignmentStatus.RUNNING,
            selected_lane=Lane.LOCAL_ONLY,
            idempotency_key=f"decision:ASS-TERMINAL-SWEEP24X:{assignment_id}",
        )
    )
    # Claim refreshes payload_json with the worker identity and active status.
    cache.update_assignment_status(
        assignment_id, status=payload_status, worker_id=worker_id, node_id=node_id
    )
    # Completion / lease expiry writes ONLY the authoritative column on this
    # base, leaving the payload_json mirror active. Emulate that exactly.
    with cache.connect() as conn:
        conn.execute(
            "UPDATE assignments SET status=? WHERE assignment_id=?",
            (authoritative_status, assignment_id),
        )


@pytest.mark.parametrize("terminal", ["done", "failed", "released"])
def test_late_worker_heartbeat_does_not_revive_terminal_assignment(tmp_path, terminal: str) -> None:
    cache = CacheStore(tmp_path / f"terminal-{terminal}.sqlite3")
    _seed(cache, authoritative_status=terminal, payload_status="running")
    status_before, lease_before = _column(cache)
    assert status_before == terminal
    assert lease_before is None
    assert cache.get_assignment(AID)["status"] == "running"  # stale mirror on base

    event = _service(cache).record_heartbeat_with_lease_renewal(
        HeartbeatRequest(node_id="node-1", worker_id="worker-1", assignment_id=AID, status="online")
    )

    status_after, lease_after = _column(cache)
    assert status_after == terminal, "terminal status column was overwritten by a heartbeat"
    assert event.payload.get("lease_renewed") is False, "terminal lease was renewed"
    assert lease_after is None, "terminal assignment acquired a live lease"
    cache.close()


def test_active_assignment_heartbeat_still_renews_lease(tmp_path) -> None:
    """Control: an active assignment with a matching worker still renews."""
    cache = CacheStore(tmp_path / "active.sqlite3")
    _seed(cache, authoritative_status="in_progress", payload_status="in_progress")
    before_status, before_lease = _column(cache)
    assert before_status == "in_progress" and before_lease is None

    event = _service(cache).record_heartbeat_with_lease_renewal(
        HeartbeatRequest(node_id="node-1", worker_id="worker-1", assignment_id=AID, status="online")
    )

    after_status, after_lease = _column(cache)
    assert event.payload.get("lease_renewed") is True
    assert after_status == "in_progress"  # lease renewal never rewrites status
    assert after_lease is not None
    cache.close()


def test_terminal_heartbeat_from_another_worker_is_ignored(tmp_path) -> None:
    """Control: a different worker cannot renew or revive terminal work."""
    cache = CacheStore(tmp_path / "other-worker.sqlite3")
    _seed(cache, authoritative_status="done", payload_status="running")

    event = _service(cache).record_heartbeat_with_lease_renewal(
        HeartbeatRequest(node_id="node-2", worker_id="worker-2", assignment_id=AID, status="online")
    )

    assert event.payload.get("lease_renewed") is False
    assert _column(cache)[0] == "done"
    cache.close()


def test_terminal_heartbeat_without_worker_id_is_ignored(tmp_path) -> None:
    """Control: an unidentified worker cannot renew or revive terminal work."""
    cache = CacheStore(tmp_path / "no-worker.sqlite3")
    _seed(cache, authoritative_status="done", payload_status="running")

    event = _service(cache).record_heartbeat_with_lease_renewal(
        HeartbeatRequest(node_id="node-1", assignment_id=AID, status="online")
    )

    assert event.payload.get("lease_renewed") is False
    assert _column(cache)[0] == "done"
    cache.close()


def test_heartbeat_for_unknown_assignment_is_ignored(tmp_path) -> None:
    """Control: unknown assignment ids never renew a lease."""
    cache = CacheStore(tmp_path / "unknown.sqlite3")
    event = _service(cache).record_heartbeat_with_lease_renewal(
        HeartbeatRequest(
            node_id="node-1",
            worker_id="worker-1",
            assignment_id="assign-does-not-exist",
            status="online",
        )
    )
    assert event.payload.get("lease_renewed") is False
    cache.close()
