"""Dry-run must not change control, approval or release state.

Uses only FastAPI TestClient and disposable SQLite; no live services.
"""
import pytest

from test_api import create_assignment, make_client


def witness(client):
    svc = client.app.state.assignment_service
    with svc.cache.connect() as db:
        return "\n".join(db.iterdump()), db.total_changes


def test_control_get_and_internal_reads_have_no_implicit_schema_writes(tmp_path):
    client = make_client(tmp_path)
    svc = client.app.state.assignment_service
    before = witness(client)
    with svc.cache.connect() as db:
        schema_before = db.execute("PRAGMA schema_version").fetchone()[0]
        assert db.execute(
            "SELECT count(*) FROM sqlite_master WHERE name='assignment_control'"
        ).fetchone()[0] == 0
    for _ in range(2):
        response = client.get("/api/assignment-control")
        assert response.status_code == 200
        assert response.json()["mode"] == "enabled"
        assert svc._control()["mode"] == "enabled"
        assert witness(client) == before
    with svc.cache.connect() as db:
        assert db.execute("PRAGMA schema_version").fetchone()[0] == schema_before
        assert db.execute(
            "SELECT count(*) FROM sqlite_master WHERE name='assignment_control'"
        ).fetchone()[0] == 0


@pytest.mark.parametrize("mode", ["paused", "draining", "maintenance", "enabled"])
def test_assignment_control_default_dry_run_never_changes_governor(tmp_path, mode):
    client = make_client(tmp_path)
    svc = client.app.state.assignment_service
    before = witness(client)
    with svc.cache.connect() as db:
        old_version = db.execute("PRAGMA schema_version").fetchone()[0]
        assert db.execute(
            "SELECT count(*) FROM sqlite_master WHERE name='assignment_control'"
        ).fetchone()[0] == 0

    r = client.post("/api/assignment-control", json={"mode": mode})
    assert r.status_code == 200
    body = r.json()
    assert body["accepted"] is False
    assert body["applied"] is False
    assert body["preview"] is True
    assert body["dry_run"] is True
    assert body["would_set_mode"] == mode
    assert body["current_mode"] == "enabled"
    assert "event_id" not in body
    assert witness(client) == before
    with svc.cache.connect() as db:
        assert db.execute("PRAGMA schema_version").fetchone()[0] == old_version
        assert db.execute(
            "SELECT count(*) FROM sqlite_master WHERE name='assignment_control'"
        ).fetchone()[0] == 0


def test_explicit_control_apply_only_in_synthetic_cache(tmp_path):
    client = make_client(tmp_path)
    before = witness(client)
    change = client.post(
        "/api/assignment-control",
        json={"mode": "paused", "dry_run": False}
    )
    assert change.status_code == 200
    assert change.json()["applied"] is True
    assert change.json()["preview"] is False
    assert witness(client) != before
    assert client.get("/api/assignment-control").json()["mode"] == "paused"


@pytest.mark.parametrize("route,payload", [
    ("approve", {"approved_by": "test", "approval_reason": "synthetic"}),
    ("release", {"reason": "synthetic", "retryable": True}),
])
def test_default_assignment_action_preview_never_enqueues(
    tmp_path, route, payload
):
    client = make_client(tmp_path)
    assignment = create_assignment(client, "synthetic-already-recorded")
    before = witness(client)
    api = f"/api/assignments/{assignment['assignment_id']}/{route}"
    for _ in range(2):
        response = client.post(api, json=payload)
        assert response.status_code == 200
        body = response.json()
        assert body["accepted"] is False
        assert body["preview"] is True
        assert body["dry_run"] is True
        assert body["reason"].endswith("_preview_only_no_event_enqueued")
        assert "event_id" not in body
        assert "would_emit_event_type" in body
        assert witness(client) == before


@pytest.mark.parametrize("route,payload", [
    ("approve", {"approved_by": "test", "approval_reason": "synthetic"}),
    ("release", {"reason": "synthetic", "retryable": True}),
])
def test_explicit_non_dry_run_action_enqueues_synthetic_event(
    tmp_path, route, payload
):
    client = make_client(tmp_path)
    assignment = create_assignment(client, "synthetic-event")
    before = witness(client)
    response = client.post(
        f"/api/assignments/{assignment['assignment_id']}/{route}",
        json={**payload, "dry_run": False},
    )
    assert response.status_code == 200
    assert response.json()["accepted"] is True
    assert "event_id" in response.json()
    assert witness(client) != before


@pytest.mark.parametrize("route", ["approve", "release"])
def test_missing_assignment_preview_never_authorizes_action(tmp_path, route):
    client = make_client(tmp_path)
    before = witness(client)
    response = client.post(
        f"/api/assignments/synthetic-missing/{route}", json={}
    )
    assert response.status_code == 404
    assert witness(client) == before
