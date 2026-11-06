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
    clis: int = 404, anon_by_path: dict[str, int] | None = None,
    malformed_path: str | None = None,
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
            code = (anon_by_path or {}).get(path, 404 if path == "/admin/agent-clis" else anonymous)
            return httpx.Response(code, json={"public": True})
        if path == failed_path:
            return httpx.Response(401, json={"detail": "denied"})
        if supplied != VALID:
            return httpx.Response(authorized, json={"detail": "denied"})
        if path == "/admin/agent-clis":
            return httpx.Response(clis, json={"agents": []})
        if path == malformed_path:
            return httpx.Response(200, content="not-valid-json")
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
        assert len(calls) == 10
        assert not any(had_token for _, had_token in calls[:5])
        assert all(had_token for _, had_token in calls[5:])
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
        assert len(calls) == 5
        assert all(not had_token for _, had_token in calls)
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
        assert len(calls) == 10
        assert all(not had_token for _, had_token in calls[:5])
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
        assert len(calls) == 10
        assert all(not had_token for _, had_token in calls[:5])
    finally:
        await client.close()
        await original.aclose()


@pytest.mark.asyncio
@pytest.mark.parametrize("path", [
    "/admin/context", "/admin/quota", "/admin/circuits",
    "/admin/agent-clis", "/admin/ops/summary",
])
async def test_mixed_auth_router_other_unguarded_routes_fail_before_token(path):
    # This was the security gap in the first preflight implementation.
    client, original, calls = _mock_router(
        anon_by_path={path: 200}, token=VALID,
    )
    try:
        result = await client.snapshot()
        assert not result.reachable
        assert result.providers == [] and result.services == []
        assert len(calls) == 5
        assert all(not had_token for _, had_token in calls)
    finally:
        await client.close()
        await original.aclose()


@pytest.mark.asyncio
async def test_optional_absent_admin_clis_is_allowed_without_auth():
    client, original, calls = _mock_router(anon_by_path={"/admin/agent-clis": 404})
    try:
        result = await client.snapshot()
        assert result.reachable
        assert len(calls) == 10
    finally:
        await client.close()
        await original.aclose()


@pytest.mark.asyncio
@pytest.mark.parametrize("path", [
    "/admin/context", "/admin/quota", "/admin/circuits", "/admin/ops/summary",
])
async def test_malformed_authorized_response_returns_only_empty_snapshot(path):
    client, original, _ = _mock_router(malformed_path=path)
    try:
        result = await client.snapshot()
        assert not result.reachable
        assert not result.providers
        assert not result.services
        assert not result.quota
        assert not result.circuits
        assert not result.ops_summary
    finally:
        await client.close()
        await original.aclose()


@pytest.mark.asyncio
async def test_router_admin_client_disables_environment_proxy_and_redirects(monkeypatch):
    # A third-party proxy inherited from HTTP_PROXY must never receive a
    # credential-bearing admin request. Redirects must also remain disabled.
    constructor = httpx.AsyncClient
    captured = {}

    def capture_constructor(*args, **kwargs):
        captured.update(kwargs)
        return constructor(*args, **kwargs)

    monkeypatch.setattr("auto_assign.clients.httpx.AsyncClient", capture_constructor)
    client = RouterClient(Settings(router_base_url=BASE, router_admin_token=VALID))
    try:
        assert captured["trust_env"] is False
        assert captured["follow_redirects"] is False
    finally:
        await client.close()


@pytest.mark.asyncio
async def test_public_health_redacts_transport_error_that_contains_sensitive_data():
    client = RouterClient(Settings(router_base_url=BASE, router_admin_token=VALID))
    original = client._client

    def error_transport(request: httpx.Request):
        raise httpx.ConnectError("SECRET_SHOULD_NEVER_REACH_PUBLIC_HEALTH", request=request)

    client._client = httpx.AsyncClient(transport=httpx.MockTransport(error_transport))
    try:
        result = await client.health()
        assert result == {"reachable": False, "error": "ConnectError"}
        assert "SECRET_" not in str(result)
    finally:
        await client.close()
        await original.aclose()
