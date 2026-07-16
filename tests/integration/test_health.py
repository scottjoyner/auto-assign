"""Basic integration health tests — all 3 services must be running."""

from __future__ import annotations

import httpx


def test_assistx_health(assistx_client: httpx.Client) -> None:
    resp = assistx_client.get("/health")
    assert resp.is_success, f"AssistX health failed: {resp.status_code}"
    data = resp.json()
    assert data.get("service") in {"assistx", "auto-assist"}
    assert data.get("ok") is not False

def test_router_health(router_client: httpx.Client) -> None:
    resp = router_client.get("/health")
    assert resp.is_success, f"Router health failed: {resp.status_code}"
    data = resp.json()
    assert data.get("service") == "auto-router"


def test_assign_health(assign_client: httpx.Client) -> None:
    resp = assign_client.get("/health")
    assert resp.is_success, f"Assign health failed: {resp.status_code}"
    data = resp.json()
    assert data.get("status") in ("ok", "degraded")


def test_correlation_id_propagation(assistx_client: httpx.Client) -> None:
    cid = "test-cid-integration"
    resp = assistx_client.get("/health", headers={"X-Correlation-ID": cid})
    assert resp.headers.get("X-Correlation-ID") == cid


def test_trace_id_propagation(assistx_client: httpx.Client) -> None:
    tid = "test-tid-integration"
    resp = assistx_client.get("/health", headers={"X-Trace-ID": tid})
    assert resp.headers.get("X-Trace-ID") == tid


def test_stale_heartbeat_watchdog_surface_is_available(assign_client: httpx.Client) -> None:
    stale = assign_client.get("/api/heartbeats/stale?limit=5&stale_after_seconds=1")
    assert stale.is_success, f"Stale heartbeat lookup failed: {stale.status_code}"

    payload = stale.json()
    assert payload.get("source") == "sqlite_cache_mirror"
    assert payload.get("canonical_source") == "neo4j_via_assistx"
    assert payload.get("stale_after_seconds") == 1
    assert isinstance(payload.get("heartbeats"), list)
    assert payload.get("count", 0) >= 0

    health = assign_client.get("/health").json()
    assert health["scheduler"]["control"]["mode"] == "enabled"
    assert health["scheduler"]["dispatch_enabled"] is True
