"""Synthetic read-only admin snapshot admission tests; no live credentials/network."""

from __future__ import annotations

import asyncio

import httpx
import pytest

from auto_assign.clients import RouterClient
from auto_assign.settings import Settings


VALID_CONTEXT = {
    "revision": "synthetic-router-revision",
    "source": "synthetic",
    "providers": [{"name": "synthetic-provider"}],
    "nodes": [],
}
ROUTES = {
    "/admin/context": 200,
    "/admin/quota": 200,
    "/admin/circuits": 200,
    "/admin/agent-clis": 404,  # Old routers omit this optional endpoint.
    "/admin/ops/summary": 200,
}


class FakeAdminHttp:
    def __init__(self, *, statuses=None, context=None):
        self.statuses = {**ROUTES, **(statuses or {})}
        self.context = VALID_CONTEXT if context is None else context
        self.calls = []

    async def get(self, url, *, headers):
        path = httpx.URL(url).path
        self.calls.append({
            "path": path,
            "token_present": bool(headers.get("X-Admin-Token")),
            "method": "GET",
        })
        code = self.statuses[path]
        body = (
            self.context if path == "/admin/context"
            else {"status": "synthetic"} if code < 300
            else {"detail": "synthetic denial"}
        )
        return httpx.Response(
            code,
            json=body,
            request=httpx.Request("GET", url),
        )

    async def aclose(self):
        return None


def probe(*, token="synthetic-test-token-not-live", statuses=None, context=None):
    async def run():
        client = RouterClient(Settings(
            router_base_url="http://router.synthetic.invalid:8088",
            router_admin_token=token,
        ))
        await client._client.aclose()
        fake = FakeAdminHttp(statuses=statuses, context=context)
        client._client = fake
        result = await client.snapshot()
        await client.close()
        return result, fake.calls

    return asyncio.run(run())


def test_missing_admin_credential_fails_closed_without_admin_network_calls():
    result, calls = probe(token="")
    assert result.reachable is False
    assert result.providers == []
    assert calls == []


def test_full_authenticated_snapshot_with_optional_404_is_available_only():
    result, calls = probe()
    assert result.reachable is True
    assert result.context_revision == "synthetic-router-revision"
    assert result.providers == [{"name": "synthetic-provider"}]
    assert len(calls) == 5
    assert all(c["method"] == "GET" and c["token_present"] for c in calls)


@pytest.mark.parametrize("route", [
    "/admin/context", "/admin/quota",
    "/admin/circuits", "/admin/ops/summary",
])
@pytest.mark.parametrize("status", [401, 403, 429, 503, 302])
def test_partial_auth_or_provider_failure_cannot_admit_router_snapshot(route, status):
    result, calls = probe(statuses={route: status})
    assert len(calls) == 5
    assert result.reachable is False
    assert result.providers == []
    assert result.context_revision is None


@pytest.mark.parametrize("status", [401, 403, 429, 502, 302])
def test_optional_agent_cli_endpoint_fails_closed_on_denial_or_error(status):
    result, calls = probe(statuses={"/admin/agent-clis": status})
    assert len(calls) == 5
    assert result.reachable is False


@pytest.mark.parametrize("payload", [
    {"detail": "synthetic failure"},
    {"error": "synthetic gateway unavailable"},
    {"revision": "r1"},
    {"providers": [{"name": "untrusted"}]},
    {"revision": "", "providers": []},
    ["synthetic", "unexpected-list"],
])
def test_http_200_error_bodies_and_missing_provenance_fail_closed(payload):
    result, calls = probe(context=payload)
    assert len(calls) == 5
    assert result.reachable is False
    assert result.providers == []


def test_structurally_valid_empty_provider_catalog_does_not_claim_residency():
    result, _ = probe(context={
        "revision": "synthetic-zero-capacity",
        "source": "synthetic",
        "providers": [],
    })
    assert result.reachable is True
    assert result.providers == []
    assert result.context_revision == "synthetic-zero-capacity"
