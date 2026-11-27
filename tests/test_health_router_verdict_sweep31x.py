"""Hermetic /health verdict contract tests (sweep31x).

No sockets, HTTP, Redis, Neo4j, SQLite/WAL, credentials, services, mounts or
live AssistX/router endpoints are used: the two async clients, the cache read
model and the control store are in-process stubs.
"""

from __future__ import annotations

from types import SimpleNamespace

import pytest

from auto_assign.main import health

UP_CACHE = {"reachable": True, "role": "cache_outbox_only"}
UP_ASSISTX = {"reachable": True, "brain": "neo4j_via_assistx"}
UP_ROUTER = {"reachable": True, "status_code": 200}
DOWN_ROUTER = {"reachable": False, "error": "ConnectError"}
DOWN_ASSISTX = {"reachable": False, "error": "ConnectError"}
DOWN_CACHE = {"reachable": False, "error": "OperationalError"}


class _StubControlStore:
    def get(self):
        return {
            "mode": "enabled",
            "new_assignments_allowed": True,
            "scheduler_ticks_allowed": True,
        }


def _service(cache_r, assistx_r, router_r):
    async def assistx_health():
        return dict(assistx_r)

    async def router_health():
        return dict(router_r)

    return SimpleNamespace(
        settings=SimpleNamespace(
            scheduler_enabled=False,
            dispatch_enabled=False,
            direct_workers_enabled=False,
        ),
        cache=SimpleNamespace(health=lambda: dict(cache_r)),
        assistx=SimpleNamespace(health=assistx_health),
        router=SimpleNamespace(health=router_health),
        last_tick_at=None,
        _control_store=_StubControlStore(),
        list_stale_heartbeats=lambda limit=50: {"count": 0},
    )


@pytest.mark.asyncio
async def test_unreachable_router_degrades_health_verdict():
    """A published, unreachable dependency must not be reported as all-ok."""
    resp = await health(_service(UP_CACHE, UP_ASSISTX, DOWN_ROUTER))

    assert resp.deps["router"]["reachable"] is False
    assert resp.ok is False
    assert resp.status == "degraded"


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "cache_r,assistx_r,router_r,expected_ok",
    [
        (UP_CACHE, UP_ASSISTX, UP_ROUTER, True),
        (UP_CACHE, UP_ASSISTX, DOWN_ROUTER, False),
        (UP_CACHE, DOWN_ASSISTX, UP_ROUTER, False),
        (DOWN_CACHE, UP_ASSISTX, UP_ROUTER, False),
    ],
    ids=["all_up", "router_down", "assistx_down", "cache_down"],
)
async def test_verdict_matches_all_published_dependencies(
    cache_r, assistx_r, router_r, expected_ok
):
    resp = await health(_service(cache_r, assistx_r, router_r))

    published = {name: dep.get("reachable") for name, dep in resp.deps.items()}
    assert resp.deps["router"]["reachable"] == router_r["reachable"]
    assert resp.ok is expected_ok
    assert resp.ok == all(published.values())
    assert resp.status == ("ok" if expected_ok else "degraded")


@pytest.mark.asyncio
async def test_all_dependencies_up_reports_ok():
    resp = await health(_service(UP_CACHE, UP_ASSISTX, UP_ROUTER))

    assert resp.ok is True
    assert resp.status == "ok"
