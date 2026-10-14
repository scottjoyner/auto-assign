"""Fail-closed canonical Task identity gate; all sources are synthetic.

No real AssistX, Router, graph, services, credentials or sockets are used.
"""
from __future__ import annotations

import asyncio

import httpx
import pytest
from fastapi.testclient import TestClient

from auto_assign.cache import CacheStore
from auto_assign.clients import AssistXClient
from auto_assign.main import app
from auto_assign.models import AssignmentCandidate, AssignmentEvaluateRequest, SchedulerTickRequest
from auto_assign.scorer import AssignmentScorer
from auto_assign.service import AssignmentService, CanonicalTaskUnverified
from auto_assign.settings import Settings

TEST_TOKEN = "synthetic-canonical-task-verification-test-only"
TASK_ID = "synthetic-canonical-task"


class SyntheticCanonicalClient:
    def __init__(self, response=None, error=None):
        self.response = response
        self.error = error
        self.calls = 0

    async def get_task(self, task_id):
        self.calls += 1
        if self.error is not None:
            raise self.error
        return self.response

    async def event_status(self, idempotency_key):
        raise AssertionError("no downstream event_status without canonical task")


class NoRouterRequests:
    async def snapshot(self):
        raise AssertionError("no router calls without canonical task")


def service_for(tmp_path, result=None, error=None):
    settings = Settings(
        database_url=f"sqlite:///{tmp_path / 'canonical.sqlite3'}",
        api_token=TEST_TOKEN,
    )
    cache = CacheStore(settings.sqlite_path)
    upstream = SyntheticCanonicalClient(response=result, error=error)
    svc = AssignmentService(
        settings=settings,
        cache=cache,
        assistx=upstream,
        router=NoRouterRequests(),
        scorer=AssignmentScorer(settings),
    )
    return svc, upstream


def assert_no_assignment_effects(svc):
    with svc.cache.connect() as conn:
        assert conn.execute("SELECT count(*) FROM assignments").fetchone()[0] == 0
        assert conn.execute("SELECT count(*) FROM outbox_events").fetchone()[0] == 0
        assert conn.execute(
            "SELECT count(*) FROM inbound_event_processing"
        ).fetchone()[0] == 0
        assert conn.execute("SELECT count(*) FROM scheduler_runs").fetchone()[0] == 0


@pytest.mark.parametrize("dry_run", [True, False])
@pytest.mark.parametrize(
    ("response", "error"),
    [
        (None, None),
        (AssignmentCandidate(task_id="synthetic-different-task"), None),
        (None, RuntimeError("synthetic private upstream failure detail")),
    ],
)
def test_missing_mismatch_or_upstream_error_is_denied_without_effects(
    tmp_path, dry_run, response, error, caplog
):
    svc, upstream = service_for(tmp_path, result=response, error=error)
    request = AssignmentEvaluateRequest(task_id=TASK_ID, dry_run=dry_run)
    with pytest.raises(
        CanonicalTaskUnverified,
        match="canonical task identity unavailable",
    ) as exc:
        asyncio.run(svc.evaluate(request))
    assert "synthetic private upstream failure detail" not in str(exc.value)
    assert upstream.calls == 1
    assert_no_assignment_effects(svc)


@pytest.mark.parametrize("response", [None, AssignmentCandidate(task_id="mismatch")])
def test_api_returns_sanitized_503_not_a_fabricated_assignment(
    tmp_path, response
):
    svc, upstream = service_for(tmp_path, result=response)
    app.state.assignment_service = svc
    client = TestClient(
        app, headers={"Authorization": f"Bearer {TEST_TOKEN}"}
    )
    denial = client.post(
        "/api/assignments/evaluate",
        json={"task_id": TASK_ID, "dry_run": False},
    )
    assert denial.status_code == 503
    assert denial.json() == {
        "detail": "canonical task identity unavailable; assignment denied"
    }
    assert upstream.calls == 1
    assert_no_assignment_effects(svc)


@pytest.mark.parametrize(
    ("payload", "should_accept"),
    [
        ({"task_id": TASK_ID}, True),
        ({"id": TASK_ID}, True),
        ({"uuid": TASK_ID}, True),
        ({"task_id": TASK_ID, "id": TASK_ID}, True),
        ({"title": TASK_ID}, False),
        ({"task_id": "some-other-task"}, False),
        ({"task_id": TASK_ID, "id": "conflicting-id"}, False),
        ({"task_id": TASK_ID, "uuid": None, "id": None}, True),
        ({"title": "synthetic-title-only"}, False),
        ({"id": 3}, False),
        (["invalid", "task", "envelope"], False),
    ],
)
def test_mocked_assistx_task_read_requires_explicit_matching_ids(
    tmp_path, payload, should_accept
):
    settings = Settings(
        database_url=f"sqlite:///{tmp_path / 'client.sqlite3'}",
        api_token=TEST_TOKEN,
    )
    source = AssistXClient(settings)
    calls = []

    def mock_transport(request):
        calls.append(request.url.path)
        return httpx.Response(200, json=payload)

    asyncio.run(source._client.aclose())
    source._client = httpx.AsyncClient(transport=httpx.MockTransport(mock_transport))
    try:
        result = asyncio.run(source.get_task(TASK_ID))
    finally:
        asyncio.run(source.close())
    assert (result is not None) is should_accept
    if should_accept:
        assert result.task_id == TASK_ID
    assert len(calls) == 1


def test_unavailable_canonical_http_reads_return_no_candidate(tmp_path):
    settings = Settings(
        database_url=f"sqlite:///{tmp_path / 'missing.sqlite3'}",
        api_token=TEST_TOKEN,
    )
    source = AssistXClient(settings)
    asyncio.run(source._client.aclose())
    source._client = httpx.AsyncClient(
        transport=httpx.MockTransport(
            lambda request: httpx.Response(503, json={"detail": "unavailable"})
        )
    )
    try:
        assert asyncio.run(source.get_task(TASK_ID)) is None
    finally:
        asyncio.run(source.close())


@pytest.mark.parametrize("dry_run", [True, False])
@pytest.mark.parametrize("failure", ["missing", "mismatched", "error"])
def test_scheduler_full_batch_preflight_prevents_partial_assignment_effects(
    tmp_path, dry_run, failure
):
    svc, upstream = service_for(tmp_path)
    seen = []

    async def resolve(task_id):
        seen.append(task_id)
        if task_id == "synthetic-first":
            return AssignmentCandidate(task_id=task_id)
        if failure == "error":
            raise RuntimeError("synthetic sensitive client failure")
        if failure == "mismatched":
            return AssignmentCandidate(task_id="synthetic-other")
        return None

    upstream.get_task = resolve
    req = SchedulerTickRequest(
        task_ids=["synthetic-first", "synthetic-second"],
        dry_run=dry_run,
        limit=2,
    )
    with pytest.raises(CanonicalTaskUnverified):
        asyncio.run(svc.scheduler_tick(req))
    assert seen == ["synthetic-first", "synthetic-second"]
    # The verified first task may not be scored or written if the second
    # task cannot be verified. This must be all-or-nothing at the gate.
    assert_no_assignment_effects(svc)


def test_scheduler_endpoint_denies_unverified_canonical_task(tmp_path):
    svc, upstream = service_for(tmp_path)
    app.state.assignment_service = svc
    client = TestClient(app, headers={"Authorization": f"Bearer {TEST_TOKEN}"})
    result = client.post(
        "/api/scheduler/tick",
        json={"task_ids": [TASK_ID], "limit": 1, "dry_run": True},
    )
    assert result.status_code == 503
    assert result.json() == {
        "detail": "canonical task identity unavailable; assignment denied"
    }
    assert upstream.calls == 1
    assert_no_assignment_effects(svc)


def test_unverified_backlog_candidate_never_reaches_scorer(tmp_path):
    svc, upstream = service_for(tmp_path)

    async def backlog(limit):
        return [AssignmentCandidate(task_id=TASK_ID)]

    upstream.get_backlog_candidates = backlog
    with pytest.raises(CanonicalTaskUnverified):
        asyncio.run(svc.scheduler_tick(SchedulerTickRequest(dry_run=True, limit=1)))
    assert_no_assignment_effects(svc)
