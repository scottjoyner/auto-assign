"""Router admin-boundary regression: offline MockTransport, no live fleet requests.

A live legacy Router may return 200 anonymously even with the wrong configured
AUTO_ROUTER_ADMIN_TOKEN. The client must fail closed rather than treating a
public /admin/context response as proof of authorization.
"""
from __future__ import annotations

import httpx
import pytest

from auto_assign.clients import RouterClient
from auto_assign.settings import Settings

BASE = "http://router.example.invalid"
VALID = "synthetic-correct-operator-credential"
WRONG = "synthetic-stale-operator-credential"


def _mock_router(
    *, token: str = VALID, anonymous: int = 401,
    authorized: int = 200, failed_path: str | None = None,
    clis: int = 404,
):
    calls: list[tuple[str, bool]] = []

    def handler(request: httpx.Request) -> httpx.Response:
        path = request.url.path
        supplied = request.headers.get("X-Admin-Token", "")
        calls.append((path, bool(supplied)))
        if path == "/health":
            assert not supplied, "admin credential leaked to public health"
            return httpx.Response(200, json={"status": "ok"})
        assert path in {
            "/admin/context", "/admin/quota", "/admin/circuits",
            "/admin/agent-clis", "/admin/ops/summary",
        }, path
        if not supplied:
            return httpx.Response(anonymous, json={"public": True})
        if path == failed_path:
            return httpx.Response(401, json={"detail": "denied"})
        if supplied != VALID:
            return httpx.Response(authorized, json={"detail": "denied"})
        if path == "/admin/agent-clis":
            return httpx.Response(clis, json={"agents": []})
        payload = {
            "/admin/context": {
                "context": {"revision": "synthetic-v1", "providers": [{"id": "p1"}],
                            "services": [{"id": "s1"}]}
            },
            "/admin/quota": {"remaining": 4},
            "/admin/circuits": {"open": []},
            "/admin/ops/summary": {"status": "synthetic-ok"},
        }[path]
        return httpx.Response(200, json=payload)

    client = RouterClient(Settings(router_base_url=BASE, router_admin_token=token))
    # Replace only this test instance's transport; no real outbound sockets.
    original = client._client
    client._client = httpx.AsyncClient(transport=httpx.MockTransport(handler))
    return client, original, calls


@pytest.mark.asyncio
async def test_secure_valid_identity_and_optional_404_still_work():
    client, original, calls = _mock_router()
    try:
        result = await client.snapshot()
        assert result.reachable
        assert result.context_revision == "synthetic-v1"
        assert result.providers == [{"id": "p1"}]
        assert result.services == [{"id": "s1"}]
        assert result.agent_clis == []
        assert calls[0] == ("/admin/context", False)
        assert len(calls) == 6
        assert all(token for _, token in calls[1:])
    finally:
        await client.close()
        await original.aclose()


@pytest.mark.asyncio
@pytest.mark.parametrize("anonymous", [200, 201, 301, 302, 404, 500])
async def test_legacy_anonymous_or_unverifiable_admin_never_admits_work(anonymous):
    client, original, calls = _mock_router(token=WRONG, anonymous=anonymous)
    try:
        result = await client.snapshot()
        assert not result.reachable
        assert not result.providers and not result.services
        assert calls == [("/admin/context", False)]
    finally:
        await client.close()
        await original.aclose()


@pytest.mark.asyncio
async def test_missing_admin_token_does_not_issue_any_admin_request():
    client, original, calls = _mock_router(token="")
    try:
        result = await client.snapshot()
        assert not result.reachable
        assert calls == []
    finally:
        await client.close()
        await original.aclose()


@pytest.mark.asyncio
async def test_wrong_credential_denied_by_secured_router():
    client, original, calls = _mock_router(token=WRONG, anonymous=401, authorized=401)
    try:
        result = await client.snapshot()
        assert not result.reachable
        assert not result.services
        assert calls[0] == ("/admin/context", False)
        assert len(calls) == 6
    finally:
        await client.close()
        await original.aclose()


@pytest.mark.asyncio
@pytest.mark.parametrize("path", ["/admin/context", "/admin/quota",
                                  "/admin/circuits", "/admin/ops/summary"])
async def test_any_required_admin_endpoint_denial_fails_closed(path):
    client, original, calls = _mock_router(failed_path=path)
    try:
        result = await client.snapshot()
        assert not result.reachable
        assert not result.providers and not result.services
    finally:
        await client.close()
        await original.aclose()


@pytest.mark.asyncio
async def test_optional_clis_rejects_auth_error_but_allows_absence():
    client, original, _ = _mock_router(clis=403)
    try:
        assert not (await client.snapshot()).reachable
    finally:
        await client.close()
        await original.aclose()


@pytest.mark.asyncio
async def test_public_health_never_receives_admin_token():
    client, original, calls = _mock_router()
    try:
        assert (await client.health())["reachable"]
        assert calls == [("/health", False)]
    finally:
        await client.close()
        await original.aclose()


@pytest.mark.asyncio
async def test_explicit_anonymous_403_is_valid_negative_control():
    client, original, calls = _mock_router(anonymous=403)
    try:
        assert (await client.snapshot()).reachable
        assert calls[0] == ("/admin/context", False)
    finally:
        await client.close()
        await original.aclose()
