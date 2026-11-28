"""Integration test fixtures for cross-service health checks."""

from __future__ import annotations

import os

import httpx
import pytest

from tests.service_reachability import service_reachable

ASSISTX_URL = os.getenv("ASSISTX_URL", "http://localhost:8000")
ROUTER_URL = os.getenv("ROUTER_URL", "http://localhost:8088")
ASSIGN_URL = os.getenv("ASSIGN_URL", "http://localhost:8090")


@pytest.fixture(scope="session", autouse=True)
def require_live_services() -> None:
    """Skip these integration tests when any live service is not reachable.

    Point ASSISTX_URL, ROUTER_URL and ASSIGN_URL at running services to enable
    the full health checks; no assertions are stubbed or relaxed here.
    """
    for service, url, env in (
        ("AssistX", ASSISTX_URL, "ASSISTX_URL"),
        ("Router", ROUTER_URL, "ROUTER_URL"),
        ("Assign", ASSIGN_URL, "ASSIGN_URL"),
    ):
        if not service_reachable(url):
            pytest.skip(
                reason=f"live {service} not available; set {env} to run integration tests"
            )


@pytest.fixture
def assistx_client() -> httpx.Client:
    return httpx.Client(base_url=ASSISTX_URL, timeout=10)


@pytest.fixture
def router_client() -> httpx.Client:
    return httpx.Client(base_url=ROUTER_URL, timeout=10)


@pytest.fixture
def assign_client() -> httpx.Client:
    return httpx.Client(base_url=ASSIGN_URL, timeout=10)
