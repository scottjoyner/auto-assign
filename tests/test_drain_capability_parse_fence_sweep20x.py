"""Hermetic regression tests for capability-admission parsing in drain_ready_backlog.

Pure in-process stubs: no HTTP, Redis, Neo4j, SQLite, DB/WAL, credentials,
services, mounts, or live AssistX state are touched.
"""

from __future__ import annotations

import asyncio
from types import SimpleNamespace

from auto_assign.service import AssignmentService
from auto_assign.settings import Settings


class _StubAssistX:
    def __init__(self, ready: list[dict], *, status_code: int = 200):
        self.ready = ready
        self.status_code = status_code
        self.enqueued_calls: list[str] = []

    async def get_ready_tasks(self, limit: int = 200) -> list[dict]:
        return self.ready

    async def enqueue_task(self, task_id: str, dry_run: bool = False) -> dict:
        self.enqueued_calls.append(task_id)
        return {"enqueued": self.status_code != 409, "status_code": self.status_code}


def _service(*, ready: list[dict], status_code: int = 200, dispatch_enabled: bool = True):
    assistx = _StubAssistX(ready, status_code=status_code)
    settings = Settings(dispatch_enabled=dispatch_enabled, dispatch_concurrency=1)
    service = AssignmentService(
        settings, SimpleNamespace(), assistx, SimpleNamespace(), SimpleNamespace()
    )
    return service, assistx


def _drain(service: AssignmentService, **kwargs) -> dict:
    return asyncio.run(service.drain_ready_backlog(**kwargs))


def test_python_repr_capabilities_are_not_locally_enqueued() -> None:
    service, assistx = _service(
        ready=[{"id": "t-repr", "required_capabilities": "['llm']"}]
    )

    result = _drain(service)

    assert result["enqueued"] == 0
    assert result["skipped"] == 1
    assert assistx.enqueued_calls == []


def test_truncated_json_capabilities_are_not_locally_enqueued() -> None:
    service, assistx = _service(
        ready=[{"id": "t-trunc", "required_capabilities": '["llm",'}]
    )

    result = _drain(service)

    assert result["enqueued"] == 0
    assert result["skipped"] == 1
    assert assistx.enqueued_calls == []


def test_bare_token_capabilities_are_not_locally_enqueued() -> None:
    service, assistx = _service(
        ready=[{"id": "t-bare", "required_capabilities": "llm"}]
    )

    result = _drain(service)

    assert result["enqueued"] == 0
    assert result["skipped"] == 1
    assert assistx.enqueued_calls == []


def test_non_list_json_capabilities_are_not_locally_enqueued() -> None:
    service, assistx = _service(
        ready=[{"id": "t-obj", "required_capabilities": '{"llm": true}'}]
    )

    result = _drain(service)

    assert result["enqueued"] == 0
    assert result["skipped"] == 1
    assert assistx.enqueued_calls == []


def test_json_llm_capabilities_still_skipped() -> None:
    service, assistx = _service(
        ready=[{"id": "t-json-llm", "required_capabilities": '["llm", "chat"]'}]
    )

    result = _drain(service)

    assert result["enqueued"] == 0
    assert result["skipped"] == 1
    assert assistx.enqueued_calls == []


def test_python_list_llm_capabilities_still_skipped() -> None:
    service, assistx = _service(
        ready=[{"id": "t-list-llm", "required_capabilities": ["llm"]}]
    )

    result = _drain(service)

    assert result["enqueued"] == 0
    assert result["skipped"] == 1
    assert assistx.enqueued_calls == []


def test_script_capabilities_still_enqueued() -> None:
    service, assistx = _service(
        ready=[{"id": "t-script", "required_capabilities": '["script"]'}]
    )

    result = _drain(service)

    assert result["enqueued"] == 1
    assert result["skipped"] == 0
    assert assistx.enqueued_calls == ["t-script"]


def test_missing_capabilities_still_enqueued() -> None:
    service, assistx = _service(ready=[{"id": "t-none"}])

    result = _drain(service)

    assert result["enqueued"] == 1
    assert result["skipped"] == 0
    assert assistx.enqueued_calls == ["t-none"]


def test_conflict_enqueue_still_counted_as_skipped() -> None:
    service, assistx = _service(
        ready=[{"id": "t-409", "required_capabilities": ["script"]}], status_code=409
    )

    result = _drain(service)

    assert result["enqueued"] == 0
    assert result["skipped"] == 1
    assert assistx.enqueued_calls == ["t-409"]


def test_dry_run_never_enqueues() -> None:
    service, assistx = _service(
        ready=[{"id": "t-dry", "required_capabilities": ["script"]}]
    )

    result = _drain(service, dry_run=True)

    assert result["enqueued"] == 0
    assert result["skipped"] == 1
    assert assistx.enqueued_calls == []


def test_dispatch_disabled_never_enqueues() -> None:
    service, assistx = _service(
        ready=[{"id": "t-off", "required_capabilities": ["script"]}],
        dispatch_enabled=False,
    )

    result = _drain(service)

    assert result["enqueued"] == 0
    assert result["skipped"] == 0
    assert result["dispatch_disabled"] is True
    assert assistx.enqueued_calls == []
