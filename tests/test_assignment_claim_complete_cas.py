"""Synthetic compare-and-set and worker ownership assignment lifecycle tests.

Uses disposable SQLite and FastAPI TestClient only; no live graph, tokens or fleet.
"""
from __future__ import annotations

import pytest

from test_api import create_assignment, make_client


def witness(client):
    svc = client.app.state.assignment_service
    with svc.cache.connect() as conn:
        return "\n".join(conn.iterdump()), conn.total_changes


def claim(client, aid, tid, worker="worker-a"):
    return client.post(
        f"/api/assignments/{aid}/claim",
        json={
            "correlation_id": "synthetic-claim",
            "task_id": tid,
            "worker_id": worker,
            "node_id": "synthetic-node",
            "lease_seconds": 900,
        },
    )


def complete(client, aid, tid, worker="worker-a", status="success", **kw):
    return client.post(
        f"/api/assignments/{aid}/complete",
        json={
            "assignment_id": kw.get("assignment_id", aid),
            "correlation_id": "synthetic-complete",
            "task_id": tid,
            "worker_id": worker,
            "status": status,
        },
    )


def test_claim_matching_identity_once_with_owner_and_lease(tmp_path):
    client = make_client(tmp_path)
    a = create_assignment(client, "synthetic-claim-task")
    ok = claim(client, a["assignment_id"], a["task_id"])
    assert ok.status_code == 200
    assert ok.json()["accepted"] is True
    value = client.get(f"/api/assignments/{a['assignment_id']}").json()["assignment"]
    assert value["worker_id"] == "worker-a"
    assert value["status"] == "running"
    assert value["lease_expires_at"] is not None
    before = witness(client)

    # Either same-worker duplicate or another-worker takeover must be denied.
    for worker in ["worker-a", "worker-b"]:
        denied = claim(client, a["assignment_id"], a["task_id"], worker)
        assert denied.status_code == 400
        assert denied.json()["detail"]["accepted"] is False
        assert witness(client) == before


def test_mismatched_claim_task_cannot_claim_or_enqueue(tmp_path):
    client = make_client(tmp_path)
    a = create_assignment(client, "synthetic-canonical-task")
    before = witness(client)
    denied = claim(client, a["assignment_id"], "synthetic-forged-task")
    assert denied.status_code == 400
    assert denied.json()["detail"]["reason"] == "assignment_task_identity_mismatch"
    assert witness(client) == before


@pytest.mark.parametrize("mismatch", ["task", "assignment", "worker"])
def test_completion_identity_and_ownership_mismatch_fails_closed(tmp_path, mismatch):
    client = make_client(tmp_path)
    a = create_assignment(client, "synthetic-completion-identity")
    assert claim(client, a["assignment_id"], a["task_id"]).status_code == 200
    before = witness(client)
    kw = {"assignment_id": "synthetic-different-assignment"} if mismatch == "assignment" else {}
    task_id = "synthetic-different-task" if mismatch == "task" else a["task_id"]
    worker = "worker-b" if mismatch == "worker" else "worker-a"
    resp = complete(client, a["assignment_id"], task_id, worker, **kw)
    assert resp.status_code == 400
    assert witness(client) == before


def test_completion_before_claim_denied_no_ghost_event(tmp_path):
    client = make_client(tmp_path)
    a = create_assignment(client, "synthetic-unclaimed")
    before = witness(client)
    resp = complete(client, a["assignment_id"], a["task_id"])
    assert resp.status_code == 400
    assert witness(client) == before


@pytest.mark.parametrize("status,expected", [("success", "done"), ("failure", "failed")])
def test_owner_completion_updates_cache_and_emits_exactly_once(tmp_path, status, expected):
    client = make_client(tmp_path)
    a = create_assignment(client, "synthetic-completion-once")
    assert claim(client, a["assignment_id"], a["task_id"]).status_code == 200
    done = complete(client, a["assignment_id"], a["task_id"], status=status)
    assert done.status_code == 200
    assert done.json()["status"] == expected
    value = client.get(f"/api/assignments/{a['assignment_id']}").json()["assignment"]
    assert value["status"] == expected
    before = witness(client)
    replay = complete(client, a["assignment_id"], a["task_id"], status=status)
    assert replay.status_code == 400
    assert witness(client) == before


def test_expired_lease_blocks_completion_and_outbox(tmp_path):
    client = make_client(tmp_path)
    a = create_assignment(client, "synthetic-expired")
    assert claim(client, a["assignment_id"], a["task_id"]).status_code == 200
    svc = client.app.state.assignment_service
    # Synthetic disposable cache, deliberately expired to reproduce stale effect.
    with svc.cache.connect() as db:
        db.execute(
            "UPDATE assignments SET lease_expires_at='2020-01-01T00:00:00+00:00' WHERE assignment_id=?",
            (a["assignment_id"],)
        )
    before = witness(client)
    resp = complete(client, a["assignment_id"], a["task_id"])
    assert resp.status_code == 400
    assert resp.json()["detail"]["reason"] == "assignment_not_active_or_lease_expired"
    assert witness(client) == before


def test_failed_cas_claim_does_not_emit_claim_event(tmp_path):
    client = make_client(tmp_path)
    a = create_assignment(client, "synthetic-superseded")
    svc = client.app.state.assignment_service
    # Mimic another worker's successful claim atomically beforehand.
    assert svc.cache.claim_assignment_if_unowned(
        a["assignment_id"], a["task_id"], "worker-b", "node-b",
        4070908800.0,
    ) is True
    before = witness(client)
    resp = claim(client, a["assignment_id"], a["task_id"], "worker-a")
    assert resp.status_code == 400
    assert witness(client) == before
