"""Health-test fixtures: synthetic ASGI by default, explicit live release stage.

Hosted CI must never quietly dial developer or production localhost services.
In synthetic mode the *real* auto-assign FastAPI app runs in-process against
isolated temporary SQLite; only external AssistX/Router health are stubbed.
This does not count as live three-service integration or production GO.

For a supervised physical integration run, explicitly set
AUTO_ASSIGN_INTEGRATION_MODE=live and all three *_URL environment variables.
Never load real tokens from repository files or infer running services.
"""
from __future__ import annotations

import os
from urllib.parse import urlsplit

import httpx
import pytest
from fastapi.testclient import TestClient

from auto_assign.cache import CacheStore
from auto_assign.clients import AssistXClient, RouterClient
from auto_assign.main import app
from auto_assign.scorer import AssignmentScorer
from auto_assign.service import AssignmentService
from auto_assign.settings import Settings


def _mode() -> str:
    mode = os.getenv("AUTO_ASSIGN_INTEGRATION_MODE", "synthetic").strip().lower()
    if mode not in ("synthetic", "live"):
        raise pytest.UsageError("AUTO_ASSIGN_INTEGRATION_MODE must be synthetic or live")
    return mode


def _live_origin(name: str) -> str:
    raw = os.getenv(name, "").strip()
    parts = urlsplit(raw)
    if (parts.scheme not in ("http", "https") or not parts.netloc
            or parts.username or parts.password or parts.path not in ("", "/")
            or parts.query or parts.fragment):
        raise pytest.UsageError(f"{name} must be an explicit credential-free HTTP(S) origin")
    return raw.rstrip("/")


def _synthetic_health(request: httpx.Request, service: str) -> httpx.Response:
    assert request.method == "GET" and request.url.path == "/health"
    headers = {}
    for name in ("X-Correlation-ID", "X-Trace-ID"):
        if name in request.headers:
            headers[name] = request.headers[name]
    return httpx.Response(200, headers=headers, json={"service": service, "ok": True})


class _SyntheticAssistX(AssistXClient):
    async def health(self):
        return {"reachable": True, "brain": "synthetic_neo4j_boundary"}


class _SyntheticRouter(RouterClient):
    async def health(self):
        return {"reachable": True}


@pytest.fixture
def assistx_client():
    if _mode() == "live":
        with httpx.Client(base_url=_live_origin("ASSISTX_URL"), timeout=10) as client:
            yield client
    else:
        with httpx.Client(
            base_url="http://synthetic-assistx.invalid",
            transport=httpx.MockTransport(lambda req: _synthetic_health(req, "assistx")),
        ) as client:
            yield client


@pytest.fixture
def router_client():
    if _mode() == "live":
        with httpx.Client(base_url=_live_origin("ROUTER_URL"), timeout=10) as client:
            yield client
    else:
        with httpx.Client(
            base_url="http://synthetic-router.invalid",
            transport=httpx.MockTransport(lambda req: _synthetic_health(req, "auto-router")),
        ) as client:
            yield client


@pytest.fixture
def assign_client(tmp_path):
    if _mode() == "live":
        # Only an explicitly supplied protected environment credential is valid.
        # Do not read repo/.env or otherwise guess a live credential.
        token = os.getenv("AUTO_ASSIGN_API_TOKEN", "").strip()
        if not token:
            raise pytest.UsageError("live integration requires protected AUTO_ASSIGN_API_TOKEN")
        headers = {"Authorization": f"Bearer {token}"}
        with httpx.Client(base_url=_live_origin("ASSIGN_URL"), timeout=10, headers=headers) as client:
            yield client
        return

    settings = Settings(
        database_url=f"sqlite:///{tmp_path / 'integration-synthetic.sqlite3'}",
        assistx_base_url="http://synthetic-assistx.invalid",
        router_base_url="http://synthetic-router.invalid",
        scheduler_enabled=False,
        dispatch_enabled=True,
        direct_workers_enabled=False,
        api_token="",
    )
    service = AssignmentService(
        settings=settings,
        cache=CacheStore(settings.sqlite_path),
        assistx=_SyntheticAssistX(settings),
        router=_SyntheticRouter(settings),
        scorer=AssignmentScorer(settings),
    )
    old_present = hasattr(app.state, "assignment_service")
    old_service = getattr(app.state, "assignment_service", None)
    app.state.assignment_service = service
    try:
        # Actual FastAPI middleware, dependencies and routes, not an HTTP stub.
        with TestClient(app, raise_server_exceptions=True) as client:
            yield client
    finally:
        if old_present:
            app.state.assignment_service = old_service
        elif hasattr(app.state, "assignment_service"):
            delattr(app.state, "assignment_service")
