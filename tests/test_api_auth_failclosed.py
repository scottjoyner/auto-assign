"""API fail-closed authentication regression tests (no live HTTP or services)."""

from __future__ import annotations

import base64
from types import SimpleNamespace

import pytest
from fastapi.responses import JSONResponse
from starlette.requests import Request

from auto_assign.main import api_auth_middleware
from auto_assign.settings import Settings


async def check_auth(
    *, token: str = "", dev: bool = False, peer: str = "10.0.3.42",
    method: str = "POST", path: str = "/api/assignment-control",
    authorization: tuple[str, ...] = (),
) -> tuple[int, bool]:
    settings = Settings(
        api_token=token, allow_unauthenticated_local_dev=dev,
    )
    fake_app = SimpleNamespace(
        state=SimpleNamespace(
            assignment_service=SimpleNamespace(settings=settings)
        )
    )
    scope = {
        "type": "http",
        "http_version": "1.1",
        "method": method,
        "scheme": "http",
        "path": path,
        "raw_path": path.encode("ascii"),
        "root_path": "",
        "query_string": b"",
        "headers": [(b"authorization", value.encode("utf-8"))
                    for value in authorization],
        "client": (peer, 12345),
        "server": ("testserver", 8090),
        "app": fake_app,
    }
    called = False

    async def next_handler(request: Request):
        nonlocal called
        called = True
        return JSONResponse({"allowed": True})

    result = await api_auth_middleware(Request(scope), next_handler)
    return result.status_code, called


@pytest.mark.asyncio
@pytest.mark.parametrize("peer", ["10.0.3.42", "127.0.0.1", "::1", "testclient"])
async def test_missing_api_token_denies_all_peers_by_default(peer):
    assert await check_auth(peer=peer) == (401, False)


@pytest.mark.asyncio
@pytest.mark.parametrize("peer", ["10.0.3.42", "192.168.1.1", "172.18.0.2"])
async def test_explicit_local_dev_flag_never_allows_remote_peers(peer):
    assert await check_auth(peer=peer, dev=True) == (401, False)


@pytest.mark.asyncio
@pytest.mark.parametrize("peer", ["127.0.0.1", "::1", "testclient"])
async def test_explicit_local_dev_flag_allows_only_test_or_loopback(peer):
    assert await check_auth(peer=peer, dev=True) == (200, True)


@pytest.mark.asyncio
@pytest.mark.parametrize("path", ["/health", "/metrics"])
async def test_public_endpoints_remain_accessible_without_token(path):
    assert await check_auth(path=path) == (200, True)


@pytest.mark.asyncio
async def test_protected_route_requires_exact_bearer_and_rejects_duplicates():
    assert await check_auth(token="synthetic-secret") == (401, False)
    assert await check_auth(token="synthetic-secret", authorization=("Bearer wrong",)) == (401, False)
    assert await check_auth(token="synthetic-secret", authorization=("Bearer synthetic-secret",)) == (200, True)
    assert await check_auth(
        token="synthetic-secret",
        authorization=("Bearer synthetic-secret", "Bearer wrong"),
    ) == (401, False)


@pytest.mark.asyncio
async def test_basic_auth_is_strictly_decoded_and_wrong_password_denied():
    valid = base64.b64encode(b"operator:synthetic-secret").decode("ascii")
    wrong = base64.b64encode(b"operator:wrong").decode("ascii")
    assert await check_auth(token="synthetic-secret", authorization=(f"Basic {valid}",)) == (200, True)
    assert await check_auth(token="synthetic-secret", authorization=(f"Basic {wrong}",)) == (401, False)
    assert await check_auth(token="synthetic-secret", authorization=("Basic invalid***",)) == (401, False)


@pytest.mark.asyncio
async def test_unicode_token_mismatch_fails_closed_without_500():
    assert await check_auth(token="synthetic-secret", authorization=("Bearer ☃",)) == (401, False)


@pytest.mark.asyncio
async def test_dev_opt_in_cannot_bypass_configured_token():
    assert await check_auth(token="synthetic-secret", dev=True, peer="127.0.0.1") == (401, False)
