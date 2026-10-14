"""Synthetic compare-and-set and worker ownership assignment lifecycle tests.

Uses disposable SQLite and FastAPI TestClient only; no live graph, tokens or fleet.
"""
from __future__ import annotations

import pytest

from auto_assign.models import HeartbeatRequest
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


@pytest.mark.parametrize("terminal", ["done", "failed", "released"])
def test_terminal_assignment_cannot_renew_worker_lease(tmp_path, terminal):
    client = make_client(tmp_path)
    a = create_assignment(client, f"synthetic-{terminal}")
    assert claim(client, a["assignment_id"], a["task_id"]).status_code == 200
    svc = client.app.state.assignment_service
    svc.cache.update_assignment_status(a["assignment_id"], status=terminal)
    with svc.cache.connect() as db:
        previous = db.execute(
            "SELECT lease_expires_at FROM assignments WHERE assignment_id=?",
            (a["assignment_id"],)
        ).fetchone()[0]
    event = svc.record_heartbeat_with_lease_renewal(
        HeartbeatRequest(node_id="synthetic-node", worker_id="worker-a",
                         assignment_id=a["assignment_id"])
    )
    assert event.payload["lease_renewed"] is False
    with svc.cache.connect() as db:
        assert db.execute(
            "SELECT lease_expires_at FROM assignments WHERE assignment_id=?",
            (a["assignment_id"],)
        ).fetchone()[0] == previous


@pytest.mark.parametrize("worker", ["worker-b", "worker-a"])
def test_expired_or_nonowner_heartbeat_cannot_recreate_live_lease(
    tmp_path, worker
):
    client = make_client(tmp_path)
    a = create_assignment(client, "synthetic-lease-renew")
    assert claim(client, a["assignment_id"], a["task_id"]).status_code == 200
    svc = client.app.state.assignment_service
    with svc.cache.connect() as db:
        db.execute(
            "UPDATE assignments SET lease_expires_at='2020-01-01T00:00:00+00:00' WHERE assignment_id=?",
            (a["assignment_id"],)
        )
    event = svc.record_heartbeat_with_lease_renewal(
        HeartbeatRequest(node_id="synthetic-node", worker_id=worker,
                         assignment_id=a["assignment_id"])
    )
    assert event.payload["lease_renewed"] is False
    with svc.cache.connect() as db:
        assert db.execute(
            "SELECT lease_expires_at FROM assignments WHERE assignment_id=?",
            (a["assignment_id"],)
        ).fetchone()[0] == "2020-01-01T00:00:00+00:00"


def test_valid_owner_heartbeat_renews_active_lease_once(tmp_path):
    client = make_client(tmp_path)
    a = create_assignment(client, "synthetic-active-heartbeat")
    assert claim(client, a["assignment_id"], a["task_id"]).status_code == 200
    svc = client.app.state.assignment_service
    event = svc.record_heartbeat_with_lease_renewal(
        HeartbeatRequest(node_id="synthetic-node", worker_id="worker-a",
                         assignment_id=a["assignment_id"])
    )
    assert event.payload["lease_renewed"] is True


@pytest.mark.parametrize("request_body", [
    {"task_id": "", "worker_id": "worker-a", "correlation_id": "synthetic"},
    {"task_id": "valid", "worker_id": "", "correlation_id": "synthetic"},
    {"task_id": "valid", "worker_id": "worker-a", "correlation_id": ""},
])
def test_claim_empty_identity_is_422_without_writes(tmp_path, request_body):
    client = make_client(tmp_path)
    a = create_assignment(client, "synthetic-invalid-claim")
    before = witness(client)
    r = client.post(f"/api/assignments/{a['assignment_id']}/claim", json=request_body)
    assert r.status_code == 422
    assert witness(client) == before


@pytest.mark.parametrize("invalid_status", ["", "done", "garbage", "SUCCESS"])
def test_invalid_completion_status_rejected_at_schema(tmp_path, invalid_status):
    client = make_client(tmp_path)
    a = create_assignment(client, "synthetic-invalid-completion")
    assert claim(client, a["assignment_id"], a["task_id"]).status_code == 200
    before = witness(client)
    result = complete(client, a["assignment_id"], a["task_id"], status=invalid_status)
    assert result.status_code == 422
    assert witness(client) == before


def test_heartbeat_cas_never_shortens_an_existing_longer_lease(tmp_path):
    client = make_client(tmp_path)
    a = create_assignment(client, "synthetic-long-lease")
    assert claim(client, a["assignment_id"], a["task_id"]).status_code == 200
    svc = client.app.state.assignment_service
    later = "2099-01-01T00:00:00+00:00"
    with svc.cache.connect() as db:
        db.execute(
            "UPDATE assignments SET lease_expires_at=?, "
            "payload_json=json_set(payload_json,'$.lease_expires_at',?) "
            "WHERE assignment_id=?",
            (later, later, a["assignment_id"]),
        )
    event = svc.record_heartbeat_with_lease_renewal(
        HeartbeatRequest(
            node_id="synthetic-node", worker_id="worker-a",
            assignment_id=a["assignment_id"],
        )
    )
    assert event.payload["lease_renewed"] is True
    with svc.cache.connect() as db:
        row = db.execute(
            "SELECT lease_expires_at FROM assignments WHERE assignment_id=?",
            (a["assignment_id"],)
        ).fetchone()
        assert row[0] == later
    assert svc.cache.get_assignment(a["assignment_id"])["lease_expires_at"] == later
