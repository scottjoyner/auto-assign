"""Regression tests for status mirroring in CacheStore.update_assignment_status.

The assignments `status` column and the `payload_json` mirror must agree after a
status-only update.  auto-assign's claim/completion lifecycle reads the mirror via
`get_assignment()`, so a stale "recommended" status there re-opens a terminal
assignment for claiming.
"""

from __future__ import annotations

from datetime import UTC, datetime

import pytest

from auto_assign.cache import CacheStore
from auto_assign.models import AssignmentDecision, AssignmentStatus, Lane

ASSIGNMENT_ID = "assign_status_mirror_sweep23x"
CLAIMABLE_STATUSES = ("recommended", "reserved")


def make_store(tmp_path):
    store = CacheStore(tmp_path / "status_mirror.sqlite3")
    decision = AssignmentDecision(
        assignment_id=ASSIGNMENT_ID,
        task_id="ASS-status-mirror",
        decision_id="decision_status_mirror_sweep23x",
        status=AssignmentStatus.RECOMMENDED,
        selected_lane=Lane.LOCAL_ONLY,
        idempotency_key="sweep23x:status-mirror",
    )
    store.upsert_assignment(decision)
    return store


def column_status(store):
    return (
        store.connect()
        .execute("SELECT status FROM assignments WHERE assignment_id=?", (ASSIGNMENT_ID,))
        .fetchone()[0]
    )


@pytest.mark.parametrize("terminal_status", ["done", "failed", "released", "blocked"])
def test_status_only_update_mirrors_into_payload(tmp_path, terminal_status):
    store = make_store(tmp_path)

    assert store.update_assignment_status(ASSIGNMENT_ID, status=terminal_status) is True

    payload = store.get_assignment(ASSIGNMENT_ID)
    assert payload["status"] == terminal_status


@pytest.mark.parametrize("terminal_status", ["done", "failed", "released"])
def test_terminal_status_is_no_longer_claimable_via_mirror(tmp_path, terminal_status):
    store = make_store(tmp_path)
    assert store.get_assignment(ASSIGNMENT_ID)["status"] in CLAIMABLE_STATUSES

    store.update_assignment_status(ASSIGNMENT_ID, status=terminal_status)

    assert store.get_assignment(ASSIGNMENT_ID)["status"] not in CLAIMABLE_STATUSES


def test_column_and_payload_status_agree_after_status_only_update(tmp_path):
    store = make_store(tmp_path)

    store.update_assignment_status(ASSIGNMENT_ID, status="done")

    assert column_status(store) == store.get_assignment(ASSIGNMENT_ID)["status"] == "done"


def test_worker_and_node_are_still_mirrored(tmp_path):
    store = make_store(tmp_path)

    store.update_assignment_status(ASSIGNMENT_ID, status="running", worker_id="w-1", node_id="n-1")

    payload = store.get_assignment(ASSIGNMENT_ID)
    assert payload["status"] == "running"
    assert payload["worker_id"] == "w-1"
    assert payload["node_id"] == "n-1"


def test_status_none_leaves_payload_status_untouched(tmp_path):
    store = make_store(tmp_path)
    before = store.get_assignment(ASSIGNMENT_ID)["status"]
    lease = datetime(2030, 1, 1, tzinfo=UTC).timestamp()

    store.update_assignment_status(ASSIGNMENT_ID, lease_expires_at=lease)

    assert store.get_assignment(ASSIGNMENT_ID)["status"] == before


def test_unknown_assignment_returns_false(tmp_path):
    store = make_store(tmp_path)

    assert store.update_assignment_status("assign_missing", status="done") is False
