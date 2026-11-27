"""Persistent-5xx contract for AssistXClient._request_with_retry (sweep30x).

All cases are hermetic: the httpx AsyncClient is replaced with a in-process
stub, and asyncio.sleep is monkeypatched to a no-op. No socket, HTTP, Redis,
Neo4j, SQLite/DB/WAL, credential, service, mount or live AssistX endpoint is
read or written.
"""

from __future__ import annotations

import httpx
import pytest

from auto_assign.clients import AssistXClient
from auto_assign.models import EventEnvelope
from auto_assign.settings import Settings


class _Response:
    """Minimal httpx.Response shape used by _request_with_retry and callers."""

    def __init__(self, status_code: int) -> None:
        self.status_code = status_code
        self.is_success = 200 <= status_code < 300

    def json(self) -> dict:
        return {}


class _StubClient:
    """Return the configured status codes in order, repeating the last one."""

    def __init__(self, *codes: int, raises: Exception | None = None) -> None:
        self.codes = list(codes)
        self.raises = raises
        self.calls = 0

    async def request(self, method: str, url: str, **kwargs: object) -> _Response:
        self.calls += 1
        if self.raises is not None:
            raise self.raises
        code = self.codes[min(self.calls - 1, len(self.codes) - 1)]
        return _Response(code)


@pytest.fixture(autouse=True)
def _no_sleep(monkeypatch: pytest.MonkeyPatch) -> None:
    async def noop(*args: object, **kwargs: object) -> None:
        return None

    monkeypatch.setattr("auto_assign.clients.asyncio.sleep", noop)


def _client(stub: _StubClient) -> AssistXClient:
    client = AssistXClient(Settings())
    # Replace the real client without issuing any network I/O.
    client._client = stub  # type: ignore[assignment]
    return client


async def test_persistent_500_returns_last_response_without_retries() -> None:
    stub = _StubClient(500)
    client = _client(stub)

    result = await client._request_with_retry(
        "POST", "http://example.invalid/events", max_retries=0
    )

    assert isinstance(result, _Response)
    assert result.status_code == 500
    assert stub.calls == 1


async def test_persistent_503_returns_last_response_after_retries() -> None:
    stub = _StubClient(503)
    client = _client(stub)

    result = await client._request_with_retry(
        "POST", "http://example.invalid/events", max_retries=2
    )

    assert isinstance(result, _Response)
    assert result.status_code == 503
    assert stub.calls == 3


async def test_post_event_reports_upstream_status_on_persistent_5xx() -> None:
    stub = _StubClient(500)
    client = _client(stub)
    event = EventEnvelope(event_type="assign.test", idempotency_key="sweep30x-key")

    result = await client.post_event(event, dry_run=False)

    assert result.get("delivered") is False
    assert result.get("status_code") == 500
    assert "error" not in result


async def test_transient_5xx_then_success_returns_success() -> None:
    stub = _StubClient(500, 200)
    client = _client(stub)

    result = await client._request_with_retry(
        "POST", "http://example.invalid/events", max_retries=3
    )

    assert result.status_code == 200
    assert stub.calls == 2


async def test_non_retryable_4xx_is_returned_without_retry() -> None:
    stub = _StubClient(403)
    client = _client(stub)

    result = await client._request_with_retry(
        "POST", "http://example.invalid/events", max_retries=3
    )

    assert result.status_code == 403
    assert stub.calls == 1


async def test_connection_error_on_final_attempt_still_raises() -> None:
    stub = _StubClient(500, raises=httpx.ConnectError("no route"))
    client = _client(stub)

    with pytest.raises(httpx.ConnectError):
        await client._request_with_retry("POST", "http://example.invalid/events", max_retries=0)
