"""Synthetic-only safety checks for missing API-token fail-closed behavior.

Directly invoke middleware in-process. No live HTTP, Docker, database, or
secret values are used; all credentials below are fabricated test strings.
"""

import asyncio
import base64
from types import SimpleNamespace

import pytest
from starlette.requests import Request
from starlette.responses import Response

from auto_assign.main import api_auth_middleware


FAKE_TOKEN = "synthetic-unit-only-not-a-live-credential"


def _request(path, token, auth=""):
    scope = {
        "type": "http",
        "method": "GET",
        "path": path,
        "raw_path": path.encode(),
        "query_string": b"",
        "headers": (
            [(b"authorization", auth.encode())] if auth else []
        ),
        "app": SimpleNamespace(
            state=SimpleNamespace(
                assignment_service=SimpleNamespace(
                    settings=SimpleNamespace(api_token=token)
                )
            )
        ),
        "scheme": "http",
        "server": ("localhost", 8090),
    }
    return Request(scope)


def _run(path, token, auth=""):
    called = []

    async def call_next(request):
        called.append(request.url.path)
        return Response(status_code=200)

    reply = asyncio.run(api_auth_middleware(_request(path, token, auth), call_next))
    return reply.status_code, called


@pytest.mark.parametrize("path", ["/api/events", "/api/assignment-control", "/api/scheduler/tick"])
def test_missing_token_denies_protected_routes_without_calling_handler(path):
    status, called = _run(path, "")
    assert status == 503
    assert called == []


@pytest.mark.parametrize("path", ["/health", "/metrics"])
def test_public_health_and_metrics_remain_available_with_missing_token(path):
    status, called = _run(path, "")
    assert status == 200
    assert called == [path]


def test_incorrect_bearer_token_is_denied():
    status, called = _run("/api/events", FAKE_TOKEN, "Bearer synthetic-wrong")
    assert status == 401
    assert called == []


def test_missing_header_with_configured_token_is_denied():
    status, called = _run("/api/events", FAKE_TOKEN)
    assert status == 401
    assert called == []


def test_correct_synthetic_bearer_token_is_accepted():
    status, called = _run("/api/events", FAKE_TOKEN, "Bearer " + FAKE_TOKEN)
    assert status == 200
    assert called == ["/api/events"]


def test_correct_synthetic_basic_password_is_accepted():
    encoded = base64.b64encode(("synthetic-user:" + FAKE_TOKEN).encode()).decode()
    status, called = _run("/api/events", FAKE_TOKEN, "Basic " + encoded)
    assert status == 200
    assert called == ["/api/events"]


def test_malformed_basic_auth_is_denied():
    status, called = _run("/api/events", FAKE_TOKEN, "Basic malformed")
    assert status == 401
    assert called == []


def test_optimized_python_does_not_remove_middleware_auth_check():
    # This test checks the implementation's reliance on control flow instead
    # of assert statements, and never needs a real HTTP request.
    import inspect

    source = inspect.getsource(api_auth_middleware)
    assert 'if not settings.api_token:' in source
    assert 'status_code=503' in source
    assert 'return await call_next(request)' not in source.split(
        'if not settings.api_token:', 1
    )[1].split('auth_header =', 1)[0]
