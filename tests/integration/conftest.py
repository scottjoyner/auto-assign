"""Integration test fixtures for cross-service health checks."""

from __future__ import annotations

import os

import httpx
import pytest

ASSISTX_URL = os.getenv("ASSISTX_URL", "http://localhost:8000")
ROUTER_URL = os.getenv("ROUTER_URL", "http://localhost:8088")
ASSIGN_URL = os.getenv("ASSIGN_URL", "http://localhost:8090")


@pytest.fixture
def assistx_client() -> httpx.Client:
    return httpx.Client(base_url=ASSISTX_URL, timeout=10)


@pytest.fixture
def router_client() -> httpx.Client:
    return httpx.Client(base_url=ROUTER_URL, timeout=10)


@pytest.fixture
def assign_client() -> httpx.Client:
    return httpx.Client(base_url=ASSIGN_URL, timeout=10)
