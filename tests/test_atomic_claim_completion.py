"""SQLite compare-and-set lease and outbox security, entirely synthetic."""
from __future__ import annotations

import asyncio
import concurrent.futures
from datetime import UTC, datetime, timedelta
from threading import Barrier

import pytest
from fastapi.testclient import TestClient

from test_api import create_assignment, make_client


def witness(client):
    service = client.app.state.assignment_service
    with service.cache.connect() as conn:
        return "\n".join(conn.iterdump()), conn.total_changes


def claim(client, assignment, worker="synthetic-worker-a", *,
          dry_run=False, correlation="synthetic-claim-1", task=None):
    return client.post(
        f"/api/assignments/{assignment['assignment_id']}/claim",
        json={
            "correlation_id": correlation,
            "task_id": task or assignment["task_id"],
            "route_id": "synthetic-route",
            "worker_id": worker,
            "lease_seconds": 900,
            "dry_run": dry_run,
        },
    )


def complete(client, assignment, worker="synthetic-worker-a", *,
             dry_run=False, status="success", correlation="synthetic-complete-1",
             task=None, body_id=None):
    return client.post(
        f"/api/assignments/{assignment['assignment_id']}/complete",
        json={
            "correlation_id": correlation,
            "assignment_id": body_id or assignment["assignment_id"],
            "task_id": task or assignment["task_id"],
            "worker_id": worker,
            "status": status,
            "dry_run": dry_run,
        },
    )


@pytest.mark.parametrize("route", ["claim", "complete"])
def test_default_preview_never_claims_lease_or_emits_event(tmp_path, route):
    client = make_client(tmp_path)
    assignment = create_assignment(client, "synthetic-no-write")
    before = witness(client)
    for _ in range(2):
        response = (
            claim(client, assignment, dry_run=True)
            if route == "claim"
            else complete(client, assignment, dry_run=True)
        )
        assert response.status_code == 200
        assert response.json()["accepted"] is False
        assert response.json()["preview"] is True
        assert response.json()["dry_run"] is True
        assert "event_id" not in response.json()
        assert witness(client) == before


@pytest.mark.parametrize("kind", ["wrong_task", "missing", "claimed"])
def test_claim_denies_unverified_source_and_repeated_ownership(tmp_path, kind):
    client = make_client(tmp_path)
    assignment = create_assignment(client, "synthetic-task")
    if kind == "claimed":
        assert claim(client, assignment).json()["accepted"] is True
    if kind == "missing":
        assignment = {**assignment, "assignment_id": "synthetic-missing-id"}
    before = witness(client)
    response = claim(
        client, assignment, worker="synthetic-worker-b",
        task="synthetic-different-task" if kind == "wrong_task" else None,
        correlation="synthetic-claim-repeat",
    )
    assert response.status_code == 400
    assert response.json()["detail"]["accepted"] is False
    assert witness(client) == before


def test_claim_and_completion_are_single_lease_lifecycle(tmp_path):
    client = make_client(tmp_path)
    assignment = create_assignment(client, "synthetic-lease-task")
    initial = witness(client)
    r = claim(client, assignment)
    assert r.status_code == 200
    assert r.json()["accepted"] is True
    assert witness(client) != initial
    now = client.get(f"/api/assignments/{assignment['assignment_id']}").json()["assignment"]
    assert now["status"] == "running"
    assert now["worker_id"] == "synthetic-worker-a"
    assert now["lease_expires_at"] is not None
    after_claim = witness(client)

    result = complete(client, assignment)
    assert result.status_code == 200
    assert result.json()["accepted"] is True
    done = client.get(f"/api/assignments/{assignment['assignment_id']}").json()["assignment"]
    assert done["status"] == "done"
    assert done["lease_expires_at"] is None
    assert witness(client) != after_claim
    stable = witness(client)
    repeat = complete(client, assignment)
    assert repeat.status_code == 400
    assert witness(client) == stable


@pytest.mark.parametrize("kind", ["wrong_task", "wrong_worker", "wrong_body", "before_claim"])
def test_completion_rejects_invalid_ownership_and_path_body(tmp_path, kind):
    client = make_client(tmp_path)
    assignment = create_assignment(client, "synthetic-ownership")
    if kind != "before_claim":
        assert claim(client, assignment).status_code == 200
    before = witness(client)
    kw = {}
    if kind == "wrong_task":
        kw["task"] = "different-synthetic-task"
    elif kind == "wrong_worker":
        kw["worker"] = "not-current-worker"
    elif kind == "wrong_body":
        kw["body_id"] = "other-synthetic-assignment"
    response = complete(client, assignment, **kw)
    assert response.status_code == 400
    assert witness(client) == before


@pytest.mark.parametrize("lease", ["absent", "expired", "naive"])
def test_completion_rejects_invalid_or_expired_lease_without_effects(tmp_path, lease):
    client = make_client(tmp_path)
    assignment = create_assignment(client, "synthetic-lease-invalid")
    assert claim(client, assignment).status_code == 200
    aid = assignment["assignment_id"]
    with client.app.state.assignment_service.cache.connect() as conn:
        value = (
            None if lease == "absent"
            else ((datetime.now(UTC) - timedelta(minutes=5)).isoformat()
                  if lease == "expired" else datetime.now().isoformat())
        )
        conn.execute(
            "UPDATE assignments SET lease_expires_at=? WHERE assignment_id=?",
            (value, aid),
        )
    before = witness(client)
    response = complete(client, assignment)
    assert response.status_code == 400
    assert "lease_not_active" in str(response.json())
    assert witness(client) == before


@pytest.mark.parametrize("result", ["success", "failure"])
def test_completion_status_emitted_only_after_atomic_transition(tmp_path, result):
    client = make_client(tmp_path)
    assignment = create_assignment(client, "synthetic-result")
    assert claim(client, assignment).status_code == 200
    response = complete(client, assignment, status=result)
    assert response.status_code == 200
    assert response.json()["status"] == ("done" if result == "success" else "failed")
    events = client.get("/api/outbox/events").json()["events"]
    matching = [e for e in events if e["event_type"] == (
        "assignment.completed" if result == "success" else "assignment.failed"
    )]
    assert len(matching) == 1


def test_invalid_completion_result_is_rejected_by_contract(tmp_path):
    client = make_client(tmp_path)
    assignment = create_assignment(client, "synthetic-invalid-result")
    assert claim(client, assignment).status_code == 200
    before = witness(client)
    assert complete(client, assignment, status="arbitrary").status_code == 422
    assert witness(client) == before


def test_event_serialization_failure_rolls_back_claim(tmp_path, monkeypatch):
    client = make_client(tmp_path)
    assignment = create_assignment(client, "synthetic-atomic-failure")
    cache = client.app.state.assignment_service.cache
    before = witness(client)

    def fail_before_outbox_insert(_subject):
        raise ValueError("synthetic event serialization fault")

    monkeypatch.setattr(cache, "_subject_storage_value", fail_before_outbox_insert)
    # Service errors are allowed to escape TestClient. A half-claim is not.
    with pytest.raises(ValueError, match="synthetic event serialization"):
        claim(client, assignment)
    assert witness(client) == before


def test_duplicate_event_key_causes_transaction_rollback(tmp_path):
    client = make_client(tmp_path)
    assignment = create_assignment(client, "synthetic-event-collision")
    svc = client.app.state.assignment_service
    first = claim(client, assignment, correlation="synthetic-idempotency")
    assert first.status_code == 200
    aid = assignment["assignment_id"]
    # Artificially restore claimable state while keeping outbox idempotency
    # witness. This simulates a potentially unsafe stale-state restoration.
    with svc.cache.connect() as conn:
        row = conn.execute(
            "SELECT payload_json FROM assignments WHERE assignment_id=?", (aid,)
        ).fetchone()
        import json
        payload = json.loads(row["payload_json"])
        payload["status"] = "recommended"
        conn.execute(
            "UPDATE assignments SET status='recommended', payload_json=? "
            "WHERE assignment_id=?",
            (json.dumps(payload), aid),
        )
    before = witness(client)
    replay = claim(client, assignment, correlation="synthetic-idempotency")
    assert replay.status_code == 400
    assert "idempotency_conflict" in str(replay.json())
    assert witness(client) == before


def test_competing_claims_have_exactly_one_winner(tmp_path):
    client = make_client(tmp_path)
    assignment = create_assignment(client, "synthetic-racing-workers")
    service = client.app.state.assignment_service
    from auto_assign.models import AssignmentClaimRequest
    barrier = Barrier(2)

    def attempt(worker):
        barrier.wait(timeout=4)
        return asyncio.run(service.claim_assignment(
            assignment["assignment_id"],
            AssignmentClaimRequest(
                correlation_id="synthetic-" + worker,
                task_id=assignment["task_id"],
                worker_id=worker, dry_run=False
            ),
        ))

    with concurrent.futures.ThreadPoolExecutor(max_workers=2) as pool:
        futures = [pool.submit(attempt, "worker-one"), pool.submit(attempt, "worker-two")]
        results = [f.result(timeout=12) for f in futures]
    assert sum(x["accepted"] for x in results) == 1
    final = service.cache.get_assignment(assignment["assignment_id"])
    assert final["status"] == "running"
    assert final["worker_id"] in {"worker-one", "worker-two"}
    with service.cache.connect() as conn:
        count = conn.execute(
            "SELECT count(*) FROM outbox_events WHERE event_type='assignment.claimed'"
        ).fetchone()[0]
        assert count == 1
