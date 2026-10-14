"""Outbox preview is strictly read-only and must not query remote services.

All evidence comes from FastAPI TestClient, fake clients, and disposable SQLite.
"""
from __future__ import annotations

import pytest

from test_api import create_assignment, make_client


def witness(client):
    svc = client.app.state.assignment_service
    with svc.cache.connect() as conn:
        return "\n".join(conn.iterdump()), conn.total_changes


@pytest.mark.parametrize("request_kwargs", [
    {},
    {"json": {"dry_run": True}},
    {"params": {"dry_run": "true"}},
    {"json": {"dry_run": True}, "params": {"dry_run": "true"}},
])
def test_outbox_default_preview_skips_remote_acks_and_sqlite_writes(
    tmp_path, request_kwargs
):
    client = make_client(tmp_path)
    decision = create_assignment(client, "synthetic-outbox-preview")
    svc = client.app.state.assignment_service
    svc.assistx.applied_keys.add(decision["idempotency_key"])

    async def should_never_query_remote(_key):
        raise AssertionError("outbox dry-run must not even call AssistX status")

    svc.assistx.event_status = should_never_query_remote
    before = witness(client)
    for _ in range(2):
        response = client.post("/api/outbox/dispatch", **request_kwargs)
        assert response.status_code == 200
        body = response.json()
        assert body["dry_run"] is True
        assert body["read_only_preview"] is True
        assert body["considered"] == 1
        assert body["delivered"] == body["failed"] == 0
        assert body["summary"] == {"pending": 1}
        assert body["dry_run_events"] == [
            {"event_type": "assign.decision", "action": "would_check_and_dispatch"}
        ]
        assert "idempotency_key" not in str(body)
        assert "synthetic-outbox-preview" not in str(body)
        assert witness(client) == before


@pytest.mark.parametrize("json_body", [
    {"dry_run": "false"},
    {"dry_run": "true"},
    {"dry_run": None},
    {"dry_run": 0},
    {"dry_run": 1},
    {"dry_run": []},
    ["not an object"],
])
def test_outbox_rejects_non_boolean_body_with_zero_effects(tmp_path, json_body):
    client = make_client(tmp_path)
    create_assignment(client, "synthetic-invalid-outbox")
    before = witness(client)
    response = client.post("/api/outbox/dispatch", json=json_body)
    assert response.status_code == 422
    assert witness(client) == before


@pytest.mark.parametrize("body,query", [
    (True, "false"),
    (False, "true"),
])
def test_outbox_conflicting_query_body_is_rejected(tmp_path, body, query):
    client = make_client(tmp_path)
    create_assignment(client, "synthetic-conflict")
    before = witness(client)
    result = client.post(
        "/api/outbox/dispatch",
        params={"dry_run": query},
        json={"dry_run": body},
    )
    assert result.status_code == 422
    assert witness(client) == before


def test_outbox_invalid_json_is_rejected_without_side_effects(tmp_path):
    client = make_client(tmp_path)
    create_assignment(client, "synthetic-invalid-json")
    before = witness(client)
    result = client.post(
        "/api/outbox/dispatch",
        data=b"{broken-json",
        headers={"Content-Type": "application/json"},
    )
    assert result.status_code == 422
    assert witness(client) == before


def test_explicit_nondryrun_keeps_synthetic_status_reconciliation(tmp_path):
    client = make_client(tmp_path)
    decision = create_assignment(client, "synthetic-outbox-real-mode")
    svc = client.app.state.assignment_service
    svc.assistx.applied_keys.add(decision["idempotency_key"])
    before = witness(client)
    response = client.post(
        "/api/outbox/dispatch", json={"dry_run": False}
    )
    assert response.status_code == 200
    assert response.json()["dry_run"] is False
    assert response.json()["delivered"] == 1
    assert response.json()["summary"] == {"delivered": 1}
    assert witness(client) != before
