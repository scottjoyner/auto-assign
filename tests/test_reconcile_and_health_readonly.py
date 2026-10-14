"""Synthetic read-only control GET and default outbox reconciliation contract."""
from __future__ import annotations

import pytest

from test_api import create_assignment, make_client


def sqlite_witness(client):
    service = client.app.state.assignment_service
    with service.cache.connect() as conn:
        return (
            "\n".join(conn.iterdump()),
            conn.total_changes,
            conn.execute("PRAGMA schema_version").fetchone()[0],
        )


@pytest.mark.parametrize("path", ["/health", "/api/ops/summary"])
def test_health_and_ops_do_not_create_assignment_control_table(tmp_path, path):
    client = make_client(tmp_path)
    service = client.app.state.assignment_service
    before = sqlite_witness(client)
    with service.cache.connect() as conn:
        assert conn.execute(
            "SELECT count(*) FROM sqlite_master WHERE name='assignment_control'"
        ).fetchone()[0] == 0
    for _ in range(2):
        response = client.get(path)
        assert response.status_code == 200
        assert sqlite_witness(client) == before
    with service.cache.connect() as conn:
        assert conn.execute(
            "SELECT count(*) FROM sqlite_master WHERE name='assignment_control'"
        ).fetchone()[0] == 0


@pytest.mark.parametrize("request_path", [
    "/api/outbox/reconcile",
    "/api/outbox/reconcile?dry_run=true",
    "/api/outbox/reconcile?limit=1",
])
def test_default_reconciliation_never_acknowledges_and_never_queries_remote(
    tmp_path, request_path
):
    client = make_client(tmp_path)
    decision = create_assignment(client, "synthetic-pending-outbox")
    service = client.app.state.assignment_service
    service.assistx.applied_keys.add(decision["idempotency_key"])

    async def forbid_remote_status(_key):
        raise AssertionError("reconcile preview must not contact AssistX")

    service.assistx.event_status = forbid_remote_status
    before = sqlite_witness(client)
    for _ in range(2):
        response = client.post(request_path)
        assert response.status_code == 200
        data = response.json()
        assert data["dry_run"] is True
        assert data["read_only_preview"] is True
        assert data["would_check"] == 1
        assert data["checked"] == 0
        assert data["reconciled_delivered"] == 0
        assert data["conflicts_dead_lettered"] == 0
        assert data["conflicts"] == []
        assert data["summary"] == {"pending": 1}
        assert "idempotency_key" not in str(data)
        assert "synthetic-pending-outbox" not in str(data)
        assert sqlite_witness(client) == before


def test_default_reconcile_untrusted_conflict_cannot_dead_letter(tmp_path):
    client = make_client(tmp_path)
    create_assignment(client, "synthetic-dead-letter-guard")
    service = client.app.state.assignment_service

    async def conflict_status(_key):
        return {"known": True, "applied": False, "conflict": True}

    service.assistx.event_status = conflict_status
    before = sqlite_witness(client)
    response = client.post("/api/outbox/reconcile")
    assert response.status_code == 200
    assert response.json()["conflicts_dead_lettered"] == 0
    assert sqlite_witness(client) == before


def test_explicit_synthetic_apply_retains_existing_reconcile_contract(tmp_path):
    client = make_client(tmp_path)
    decision = create_assignment(client, "synthetic-explicit-reconcile")
    service = client.app.state.assignment_service
    service.assistx.applied_keys.add(decision["idempotency_key"])
    before = sqlite_witness(client)
    response = client.post("/api/outbox/reconcile?dry_run=false")
    assert response.status_code == 200
    assert response.json()["dry_run"] is False
    assert response.json()["reconciled_delivered"] == 1
    assert response.json()["summary"] == {"delivered": 1}
    assert sqlite_witness(client) != before
