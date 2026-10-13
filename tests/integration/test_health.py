"""Basic integration health tests — all 3 services must be running."""

from __future__ import annotations

import os

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

    # This endpoint test validates read-only response shape, NOT authorization
    # to enable the scheduler, dispatch, or new assignments.


# Live acceptance must compare all activation controls against separately
# approved expectations, not a hardcoded "enabled" deployment assumption.
_ACTIVATION_BOOLEAN_FIELDS = {
    "AUTO_ASSIGN_EXPECT_SCHEDULER_ENABLED": ("enabled",),
    "AUTO_ASSIGN_EXPECT_DISPATCH_ENABLED": ("dispatch_enabled",),
    "AUTO_ASSIGN_EXPECT_DIRECT_WORKERS_ENABLED": ("direct_workers_enabled",),
    "AUTO_ASSIGN_EXPECT_NEW_ASSIGNMENTS_ALLOWED": ("control", "new_assignments_allowed"),
    "AUTO_ASSIGN_EXPECT_SCHEDULER_TICKS_ALLOWED": ("control", "scheduler_ticks_allowed"),
}


def _read_owner_declared_activation() -> dict[str, object]:
    """Fail closed without explicit external operator expectations."""
    expected: dict[str, object] = {}
    for env_name, path in _ACTIVATION_BOOLEAN_FIELDS.items():
        raw = os.environ.get(env_name)
        if raw not in ("true", "false"):
            raise AssertionError(
                f"{env_name} must be explicitly set to 'true' or 'false' "
                "by the authorized live-integration operator"
            )
        expected[".".join(path)] = raw == "true"
    mode = os.environ.get("AUTO_ASSIGN_EXPECT_CONTROL_MODE", "").strip()
    if not mode:
        raise AssertionError(
            "AUTO_ASSIGN_EXPECT_CONTROL_MODE requires an explicitly reviewed value"
        )
    expected["control.mode"] = mode
    return expected


def _assert_owner_declared_activation(
    scheduler: object, expected: dict[str, object]
) -> None:
    """Observe state; never mutate assignment policy to satisfy a test."""
    assert isinstance(scheduler, dict), "scheduler health metadata missing"
    for field, approved in expected.items():
        actual: object = scheduler
        for part in field.split("."):
            assert isinstance(actual, dict) and part in actual, (
                f"required activation field {field} missing"
            )
            actual = actual[part]
        if isinstance(approved, bool):
            assert type(actual) is bool, f"activation field {field} is not boolean"
        else:
            assert isinstance(actual, str), f"activation field {field} is not string"
        assert actual == approved, f"activation {field} differs from owner declaration"


def test_live_activation_matches_owner_declared_profile(
    assign_client: httpx.Client,
) -> None:
    # Evaluate expectations BEFORE making an HTTP request.
    expected = _read_owner_declared_activation()
    response = assign_client.get("/health")
    assert response.is_success, f"Auto-Assign health HTTP {response.status_code}"
    scheduler = response.json().get("scheduler")
    _assert_owner_declared_activation(scheduler, expected)
