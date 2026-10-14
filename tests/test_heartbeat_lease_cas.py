"""Heartbeat authority is bounded by an active, owned, unexpired lease."""
from __future__ import annotations

from datetime import UTC, datetime, timedelta

import pytest

from auto_assign.models import HeartbeatRequest
from test_api import create_assignment, make_client

from test_atomic_claim_completion import claim, complete


def read_lease(client, assignment):
    return client.app.state.assignment_service.cache.get_assignment(
        assignment["assignment_id"]
    )["lease_expires_at"]


def heartbeat(client, assignment, worker="synthetic-worker-a"):
    service=client.app.state.assignment_service
    return service.record_heartbeat_with_lease_renewal(
        HeartbeatRequest(
            node_id="synthetic-node",
            worker_id=worker,
            assignment_id=assignment["assignment_id"],
        )
    )


def test_current_owner_renews_and_reports_persisted_lease(tmp_path):
    client=make_client(tmp_path)
    assignment=create_assignment(client, "synthetic-heartbeat")
    assert claim(client, assignment).status_code==200
    event=heartbeat(client, assignment)
    assert event.payload["lease_renewed"] is True
    stored=read_lease(client, assignment)
    assert stored is not None
    assert abs(event.payload["lease_expires_at"]-datetime.fromisoformat(stored).timestamp())<1.0


def test_heartbeat_never_shortens_longer_current_lease(tmp_path):
    client=make_client(tmp_path)
    assignment=create_assignment(client,"synthetic-long-lease")
    assert claim(client,assignment).status_code==200
    future=(datetime.now(UTC)+timedelta(hours=3)).isoformat()
    with client.app.state.assignment_service.cache.connect() as db:
        import json
        row=db.execute("SELECT payload_json FROM assignments WHERE assignment_id=?", (assignment["assignment_id"],)).fetchone()
        payload=json.loads(row["payload_json"]);payload["lease_expires_at"]=future
        db.execute(
            "UPDATE assignments SET lease_expires_at=?,payload_json=? WHERE assignment_id=?",
            (future,json.dumps(payload),assignment["assignment_id"]),
        )
    event=heartbeat(client,assignment)
    assert event.payload["lease_renewed"] is True
    actual=datetime.fromisoformat(read_lease(client,assignment))
    assert actual==datetime.fromisoformat(future)


@pytest.mark.parametrize("kind", ["wrong_worker", "expired", "missing", "terminal"])
def test_heartbeat_lease_refusal_never_reopens_or_overwrites(tmp_path,kind):
    client=make_client(tmp_path)
    assignment=create_assignment(client,"synthetic-lease-denial")
    assert claim(client,assignment).status_code==200
    cache=client.app.state.assignment_service.cache
    worker="synthetic-worker-a"
    if kind=="wrong_worker":
        worker="synthetic-impostor"
    elif kind=="terminal":
        assert complete(client,assignment).status_code==200
    else:
        raw=None if kind=="missing" else (datetime.now(UTC)-timedelta(seconds=1)).isoformat()
        with cache.connect() as db:
            db.execute(
                "UPDATE assignments SET lease_expires_at=? WHERE assignment_id=?",
                (raw,assignment["assignment_id"]),
            )
    before=read_lease(client,assignment)
    event=heartbeat(client,assignment,worker=worker)
    assert event.payload["lease_renewed"] is False
    assert read_lease(client,assignment)==before


@pytest.mark.parametrize(("field","value"),[
    ("task_id",""),("worker_id",""),("correlation_id","")
])
def test_claim_rejects_blank_identity_before_effects(tmp_path,field,value):
    client=make_client(tmp_path)
    assignment=create_assignment(client,"synthetic-claim-identity")
    data={
        "task_id": assignment["task_id"], "worker_id":"synthetic-worker",
        "correlation_id":"synthetic-correlation","dry_run":False,
    }
    data[field]=value
    assert client.post(
        f"/api/assignments/{assignment['assignment_id']}/claim",json=data
    ).status_code==422
    assert client.app.state.assignment_service.cache.get_assignment(
        assignment["assignment_id"]
    )["status"]!="running"


@pytest.mark.parametrize(("field","value"),[
    ("task_id",""),("worker_id",""),("correlation_id",""),("assignment_id","")
])
def test_completion_rejects_blank_identity_before_effects(tmp_path,field,value):
    client=make_client(tmp_path)
    assignment=create_assignment(client,"synthetic-complete-identity")
    assert claim(client,assignment).status_code==200
    data={
        "task_id":assignment["task_id"],
        "assignment_id":assignment["assignment_id"],
        "worker_id":"synthetic-worker-a",
        "correlation_id":"synthetic-correlation",
        "dry_run":False,
    }
    data[field]=value
    assert client.post(
        f"/api/assignments/{assignment['assignment_id']}/complete",json=data
    ).status_code==422
    assert client.app.state.assignment_service.cache.get_assignment(
        assignment["assignment_id"]
    )["status"]=="running"
