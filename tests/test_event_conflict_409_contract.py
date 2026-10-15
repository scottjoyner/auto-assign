"""Offline HTTP response-contract tests; no transport, network or graph calls."""
import asyncio

import httpx
import pytest

from auto_assign.clients import AssistXClient
from auto_assign.models import EventEnvelope
from auto_assign.settings import Settings


def post_result(status_code: int):
    async def case():
        client = AssistXClient(Settings())
        async def fake_request(method, url, **kwargs):
            assert method == "POST" and url.endswith("/api/events")
            return httpx.Response(status_code)
        client._request_with_retry = fake_request
        event = EventEnvelope(event_type="assign.test", idempotency_key="offline:synthetic")
        return await client.post_event(event, dry_run=False)
    return asyncio.run(case())


@pytest.mark.parametrize("code,delivered", [
    (200, True), (201, True), (202, True), (409, False),
    (400, False), (401, False), (422, False), (503, False),
])
def test_only_http_success_counts_as_delivered(code, delivered):
    result = post_result(code)
    assert result["delivered"] is delivered
    assert result["status_code"] == code


def test_dry_run_does_not_invoke_http():
    async def case():
        client = AssistXClient(Settings())
        async def fail_request(*args, **kwargs):
            raise AssertionError("not permitted")
        client._request_with_retry = fail_request
        return await client.post_event(
            EventEnvelope(event_type="assign.test", idempotency_key="offline:dry"),
            dry_run=True)
    assert asyncio.run(case()) == {"delivered": False, "dry_run": True}