"""Offline HTTP-contract fixtures; opt in to live services explicitly.

The synthetic mode exercises the REAL auto-assign ASGI app and SQLite.
AssistX/Router are bounded fake health transports, NOT integration evidence
for a production or staging multi-service rollout.
"""
from __future__ import annotations

import os

import httpx
import pytest
from fastapi.testclient import TestClient


def _health_transport(name: str) -> httpx.MockTransport:
    def respond(request: httpx.Request) -> httpx.Response:
        if request.method != "GET" or request.url.path != "/health":
            return httpx.Response(404)
        headers = {
            key: value
            for key in ("X-Correlation-ID", "X-Trace-ID")
            if (value := request.headers.get(key))
        }
        return httpx.Response(
            200, json={"service": name, "ok": True}, headers=headers,
        )

    return httpx.MockTransport(respond)


@pytest.fixture
def assistx_client():
    if os.getenv("AUTO_ASSIGN_INTEGRATION_MODE") == "live":
        with httpx.Client(base_url=os.environ["ASSISTX_URL"], timeout=10) as client:
            yield client
        return
    with httpx.Client(
        base_url="http://synthetic-assistx.invalid",
        transport=_health_transport("assistx"),
        timeout=2,
    ) as client:
        yield client


@pytest.fixture
def router_client():
    if os.getenv("AUTO_ASSIGN_INTEGRATION_MODE") == "live":
        with httpx.Client(base_url=os.environ["ROUTER_URL"], timeout=10) as client:
            yield client
        return
    with httpx.Client(
        base_url="http://synthetic-router.invalid",
        transport=_health_transport("auto-router"),
        timeout=2,
    ) as client:
        yield client


@pytest.fixture
def assign_client(tmp_path):
    from auto_assign import main
    from auto_assign.settings import Settings

    if os.getenv("AUTO_ASSIGN_INTEGRATION_MODE") == "live":
        token = os.environ["AUTO_ASSIGN_API_TOKEN"]
        assert token, "Staging integration token must be provisioned"
        with httpx.Client(
            base_url=os.environ["ASSIGN_URL"],
            timeout=10,
            headers={"Authorization": "Bearer " + token},
        ) as client:
            yield client
        return

    settings = Settings(
        database_url="sqlite:///" + str(tmp_path / "cache.sqlite3"),
        api_token="synthetic-contract-only",
        scheduler_enabled=False,
        dispatch_enabled=True,
        direct_workers_enabled=False,
    )
    service = main.build_service(settings)
    service.assistx._client = httpx.AsyncClient(
        transport=_health_transport("assistx"), timeout=2,
    )
    service.router._client = httpx.AsyncClient(
        transport=_health_transport("auto-router"), timeout=2,
    )
    assert not hasattr(main.app.state, "assignment_service")
    main.app.state.assignment_service = service
    try:
        with TestClient(main.app) as client:
            client.headers["Authorization"] = "Bearer synthetic-contract-only"
            yield client
    finally:
        del main.app.state.assignment_service
