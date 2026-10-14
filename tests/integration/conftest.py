"""Explicitly authorized cross-service integration fixtures.

The six tests in this package probe LIVE network services, not synthetic
unit fixtures. Missing authority must FAIL, not silently run against localhost
or read a nearby .env file containing potentially exposed credentials.
"""

from __future__ import annotations

import os
from urllib.parse import urlsplit

import httpx
import pytest


def _live_configuration() -> tuple[dict[str, str], str]:
    """Fail closed before opening a client; never read secrets from disk."""
    if os.environ.get("AUTO_ASSIGN_LIVE_INTEGRATION_APPROVED") != "1":
        raise RuntimeError(
            "Live integration NOT authorized: require separately approved "
            "AUTO_ASSIGN_LIVE_INTEGRATION_APPROVED=1"
        )
    targets: dict[str, str] = {}
    for name in ("ASSISTX_URL", "ROUTER_URL", "ASSIGN_URL"):
        raw = os.environ.get(name, "").strip()
        parsed = urlsplit(raw)
        if (
            parsed.scheme not in ("http", "https")
            or not parsed.hostname
            or parsed.username is not None
            or parsed.password is not None
            or parsed.query
            or parsed.fragment
        ):
            raise RuntimeError(f"{name} must be an explicit HTTP(S) service URL")
        targets[name] = raw.rstrip("/")

    # Must be supplied by the authorized operator's secret-injection path.
    # Never silently obtain a historical/stale token from checkout .env.
    token = os.environ.get("AUTO_ASSIGN_API_TOKEN", "")
    if not token:
        raise RuntimeError(
            "AUTO_ASSIGN_API_TOKEN must be explicitly injected by the "
            "authorized integration operator"
        )
    return targets, token


@pytest.fixture
def assistx_client() -> httpx.Client:
    targets, _ = _live_configuration()
    return httpx.Client(base_url=targets["ASSISTX_URL"], timeout=10)


@pytest.fixture
def router_client() -> httpx.Client:
    targets, _ = _live_configuration()
    return httpx.Client(base_url=targets["ROUTER_URL"], timeout=10)


@pytest.fixture
def assign_client() -> httpx.Client:
    targets, token = _live_configuration()
    return httpx.Client(
        base_url=targets["ASSIGN_URL"],
        timeout=10,
        headers={"Authorization": f"Bearer {token}"},
    )
