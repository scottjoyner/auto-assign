"""Integration test fixtures for cross-service health checks."""

from __future__ import annotations

import os

import httpx
import pytest

ASSISTX_URL = os.getenv("ASSISTX_URL", "http://localhost:8000")
ROUTER_URL = os.getenv("ROUTER_URL", "http://localhost:8088")
ASSIGN_URL = os.getenv("ASSIGN_URL", "http://localhost:8090")


def _assign_auth_headers() -> dict[str, str]:
    """Auth headers for auto-assign /api/* routes.

    Token comes from the environment, falling back to the deployed .env next
    to this checkout so integration runs against a secured live service work
    without extra setup.
    """
    token = os.getenv("AUTO_ASSIGN_API_TOKEN", "").strip()
    if not token:
        env_file = os.path.join(os.path.dirname(__file__), "..", "..", ".env")
        try:
            with open(env_file) as fh:
                for line in fh:
                    if line.startswith("AUTO_ASSIGN_API_TOKEN="):
                        token = line.split("=", 1)[1].strip()
                        break
        except OSError:
            pass
    return {"Authorization": f"Bearer {token}"} if token else {}


@pytest.fixture
def assistx_client() -> httpx.Client:
    return httpx.Client(base_url=ASSISTX_URL, timeout=10)


@pytest.fixture
def router_client() -> httpx.Client:
    return httpx.Client(base_url=ROUTER_URL, timeout=10)


@pytest.fixture
def assign_client() -> httpx.Client:
    return httpx.Client(base_url=ASSIGN_URL, timeout=10, headers=_assign_auth_headers())
