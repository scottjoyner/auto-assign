"""Hermetic regression coverage for decision -> Task -> RQ dispatch (2ad4f27).

No Redis, RQ worker, Neo4j, or network is used: AssistX/RQ is faked at the
service boundary and httpx is faked at the client boundary.
"""

from __future__ import annotations

import logging
from collections import deque
from typing import Any

import httpx
import pytest

from auto_assign.cache import CacheStore
from auto_assign.clients import AssistXClient, RouterClient
from auto_assign.control import AssignmentControlStore
from auto_assign.models import (
    AssignmentCandidate,
    AssignmentControlRequest,
    AssignmentDecision,
    AssignmentStatus,
    Lane,
    RouterSnapshot,
    SchedulerTickRequest,
)
from auto_assign.scorer import AssignmentScorer
from auto_assign.service import AssignmentService
from auto_assign.settings import Settings


@pytest.fixture
def anyio_backend() -> str:
    return "asyncio"


class RecordingAssistX(AssistXClient):
    """Records ordered create/enqueue calls; every result is scripted in memory."""

    def __init__(
        self,
        settings: Settings,
        *,
        create_results: list[Any] | None = None,
        enqueue_results: list[Any] | None = None,
    ) -> None:
        super().__init__(settings)
        self.calls: list[str] = []
        self.create_calls: list[dict[str, Any]] = []
        self.enqueue_calls: list[tuple[str, bool]] = []
        self._create_results = deque(create_results or [])
        self._enqueue_results = deque(enqueue_results or [])

    async def health(self) -> dict[str, Any]:
        return {"reachable": True, "brain": "neo4j_via_assistx"}

    async def get_task(self, task_id: str) -> None:
        return None

    async def get_backlog_candidates(self, limit: int = 25) -> list[AssignmentCandidate]:
        return [AssignmentCandidate(task_id=f"ASS-{i}") for i in range(min(limit, 2))]

    async def event_status(self, idempotency_key: str) -> dict[str, Any]:
        return {"known": False}

    @staticmethod
    def _resolve(result: deque[Any], default: dict[str, Any]) -> dict[str, Any]:
        if not result:
            return default
        value = result.popleft()
        if isinstance(value, Exception):
            raise value
        return value

    async def create_task(self, **kwargs: Any) -> dict[str, Any]:
        self.calls.append(f"create:{kwargs['task_id']}")
        self.create_calls.append(kwargs)
        return self._resolve(self._create_results, {"created": True, "status_code": 201})

    async def enqueue_task(self, task_id: str, dry_run: bool = False) -> dict[str, Any]:
        self.calls.append(f"enqueue:{task_id}")
        self.enqueue_calls.append((task_id, dry_run))
        return self._resolve(self._enqueue_results, {"enqueued": True, "status_code": 201})


class FakeRouterClient(RouterClient):
    async def health(self) -> dict[str, Any]:
        return {"reachable": True}

    async def snapshot(self) -> RouterSnapshot:
        return RouterSnapshot(reachable=True, context_revision="test-rev")


def make_service(
    tmp_path: Any,
    *,
    create_results: list[Any] | None = None,
    enqueue_results: list[Any] | None = None,
) -> tuple[AssignmentService, RecordingAssistX, Settings]:
    settings = Settings(database_url=f"sqlite:///{tmp_path / 'dispatch.sqlite3'}")
    assistx = RecordingAssistX(
        settings, create_results=create_results, enqueue_results=enqueue_results
    )
    service = AssignmentService(
        settings=settings,
        cache=CacheStore(settings.sqlite_path),
        assistx=assistx,
        router=FakeRouterClient(settings),
        scorer=AssignmentScorer(settings),
    )
    return service, assistx, settings


def seed_recommended(service: AssignmentService, task_id: str = "ASS-1") -> None:
    service.cache.upsert_assignment(
        AssignmentDecision(
            assignment_id=f"assign-{task_id}",
            task_id=task_id,
            decision_id=f"dec-{task_id}",
            status=AssignmentStatus.RECOMMENDED,
            selected_lane=Lane.LOCAL_ONLY,
            score=0.9,
            idempotency_key=f"decision-{task_id}",
        )
    )


class TestDecisionToRqDispatch:
    @pytest.mark.anyio
    async def test_decision_creates_task_then_enqueues_in_order(self, tmp_path):
        service, assistx, _ = make_service(tmp_path)
        tick = await service.scheduler_tick(
            SchedulerTickRequest(dry_run=False, limit=1, reason="test")
        )
        assert tick.recommended == 1

        result = await service.dispatch_tasks(dry_run=False)

        assert result == {
            "dry_run": False,
            "considered": 1,
            "created": 1,
            "failed": 0,
            "skipped": 0,
        }
        assert assistx.calls == ["create:ASS-0", "enqueue:ASS-0"]
        assert assistx.enqueue_calls == [("ASS-0", False)]
        create_call = assistx.create_calls[0]
        assert create_call["task_id"] == "ASS-0"
        assert create_call["priority"] == "background"
        assert create_call["correlation_id"]
        assert create_call["payload"]["source"] == "auto-assign"

    @pytest.mark.anyio
    async def test_dry_run_dispatch_never_creates_or_enqueues(self, tmp_path):
        service, assistx, _ = make_service(tmp_path)
        seed_recommended(service)

        result = await service.dispatch_tasks(dry_run=True)

        assert result["skipped"] == 1
        assert result["created"] == 0
        assert assistx.calls == []

    @pytest.mark.anyio
    @pytest.mark.parametrize(
        "create_result, expected_key",
        [
            ({"created": False, "status_code": 500}, "skipped"),
            (RuntimeError("neo4j unreachable"), "failed"),
        ],
    )
    async def test_failed_create_never_enqueues(self, tmp_path, create_result, expected_key):
        service, assistx, _ = make_service(tmp_path, create_results=[create_result])
        seed_recommended(service)

        result = await service.dispatch_tasks(dry_run=False)

        assert result[expected_key] == 1
        assert result["created"] == 0
        assert assistx.calls == ["create:ASS-1"]
        assert assistx.enqueue_calls == []

    @pytest.mark.anyio
    async def test_idempotent_create_conflict_does_not_enqueue_twice(self, tmp_path):
        service, assistx, _ = make_service(
            tmp_path,
            create_results=[
                {"created": True, "status_code": 201},
                {"created": False, "status_code": 409},
            ],
        )
        seed_recommended(service)

        first = await service.dispatch_tasks(dry_run=False)
        second = await service.dispatch_tasks(dry_run=False)

        assert first["created"] == 1
        assert second["created"] == 0
        assert second["skipped"] == 1
        assert assistx.enqueue_calls == [("ASS-1", False)]
        assert assistx.calls == ["create:ASS-1", "enqueue:ASS-1", "create:ASS-1"]

    @pytest.mark.anyio
    async def test_enqueue_failure_is_surfaced_and_task_remains_dispatchable(
        self, tmp_path, caplog
    ):
        service, assistx, _ = make_service(
            tmp_path,
            enqueue_results=[{"enqueued": False, "error": "rq down"}],
        )
        seed_recommended(service)

        with caplog.at_level(logging.WARNING, logger="auto_assign.service"):
            result = await service.dispatch_tasks(dry_run=False)

        assert result["created"] == 1
        assert any("ASS-1" in rec.message and "rq down" in rec.message for rec in caplog.records)
        # The decision is still RECOMMENDED, so the next cycle can retry the enqueue.
        assert service.list_assignments()[0]["status"] == AssignmentStatus.RECOMMENDED.value
        retry = await service.dispatch_tasks(dry_run=False)
        assert retry["created"] == 1
        assert assistx.enqueue_calls == [("ASS-1", False), ("ASS-1", False)]

    @pytest.mark.anyio
    async def test_enqueue_conflict_is_logged_as_already_enqueued(self, tmp_path, caplog):
        service, assistx, _ = make_service(
            tmp_path,
            enqueue_results=[{"enqueued": False, "status_code": 409}],
        )
        seed_recommended(service)

        with caplog.at_level(logging.WARNING, logger="auto_assign.service"):
            result = await service.dispatch_tasks(dry_run=False)

        assert result["created"] == 1
        assert any("409" in rec.message and "ASS-1" in rec.message for rec in caplog.records)
        assert assistx.enqueue_calls == [("ASS-1", False)]

    @pytest.mark.anyio
    async def test_enqueue_exception_is_logged_and_other_decisions_continue(
        self, tmp_path, caplog
    ):
        service, assistx, _ = make_service(
            tmp_path,
            enqueue_results=[RuntimeError("rq unreachable"), {"enqueued": True, "status_code": 201}],
        )
        seed_recommended(service, "ASS-1")
        seed_recommended(service, "ASS-2")

        with caplog.at_level(logging.WARNING, logger="auto_assign.service"):
            result = await service.dispatch_tasks(dry_run=False)

        assert result["created"] == 2
        assert result["failed"] == 0
        assert any("rq unreachable" in rec.message for rec in caplog.records)
        assert sorted(assistx.enqueue_calls) == [("ASS-1", False), ("ASS-2", False)]


class TestEnqueueClient:
    @pytest.mark.anyio
    async def test_enqueue_posts_to_enqueue_endpoint(self, monkeypatch):
        client = AssistXClient(Settings())
        calls: list[tuple[Any, ...]] = []

        async def fake_request(method: str, url: str, **kwargs: Any) -> httpx.Response:
            calls.append((method, url, kwargs))
            return httpx.Response(201, request=httpx.Request(method, url))

        monkeypatch.setattr(client, "_request_with_retry", fake_request)
        try:
            result = await client.enqueue_task("ASS-1", dry_run=False)
        finally:
            await client.close()

        assert result == {"enqueued": True, "status_code": 201}
        assert len(calls) == 1
        assert calls[0][0] == "POST"
        assert calls[0][1].endswith("/api/tasks/ASS-1/enqueue")

    @pytest.mark.anyio
    async def test_enqueue_conflict_means_already_enqueued_not_duplicate(self, monkeypatch):
        client = AssistXClient(Settings())
        calls: list[str] = []

        async def fake_request(method: str, url: str, **kwargs: Any) -> httpx.Response:
            calls.append(url)
            return httpx.Response(409, request=httpx.Request(method, url))

        monkeypatch.setattr(client, "_request_with_retry", fake_request)
        try:
            result = await client.enqueue_task("ASS-1", dry_run=False)
        finally:
            await client.close()

        assert result == {"enqueued": False, "status_code": 409}
        assert len(calls) == 1  # no retry, no second RQ job

    @pytest.mark.anyio
    async def test_enqueue_dry_run_skips_http(self, monkeypatch):
        client = AssistXClient(Settings())

        async def forbidden(*args: Any, **kwargs: Any) -> None:
            pytest.fail("dry-run enqueue must not call AssistX")

        monkeypatch.setattr(client, "_request_with_retry", forbidden)
        try:
            result = await client.enqueue_task("ASS-1", dry_run=True)
        finally:
            await client.close()

        assert result == {"enqueued": False, "dry_run": True}

    @pytest.mark.anyio
    async def test_enqueue_transport_error_is_reported_not_swallowed(
        self, monkeypatch, caplog
    ):
        client = AssistXClient(Settings())

        async def failing(*args: Any, **kwargs: Any) -> None:
            raise RuntimeError("rq unreachable")

        monkeypatch.setattr(client, "_request_with_retry", failing)
        with caplog.at_level(logging.WARNING, logger="auto_assign.clients"):
            try:
                result = await client.enqueue_task("ASS-1")
            finally:
                await client.close()

        assert result == {"enqueued": False, "error": "rq unreachable"}
        assert any("ASS-1" in rec.message for rec in caplog.records)


class TestAssignmentControlGate:
    @pytest.mark.anyio
    @pytest.mark.parametrize("mode", ["paused", "draining", "maintenance"])
    async def test_control_blocks_new_decisions_so_nothing_is_enqueued(self, tmp_path, mode):
        service, assistx, settings = make_service(tmp_path)
        store = AssignmentControlStore(settings.sqlite_path)
        store.set(
            AssignmentControlRequest(mode=mode, reason="test gate", updated_by="test")
        )
        assert store.get()["dispatch_allowed"] is False

        tick = await service.scheduler_tick(
            SchedulerTickRequest(dry_run=False, limit=2, reason="test")
        )
        assert tick.evaluated == 0
        assert tick.recommended == 0
        assert tick.decisions == []

        result = await service.dispatch_tasks(dry_run=False)

        assert result["created"] == 0
        assert assistx.calls == []
