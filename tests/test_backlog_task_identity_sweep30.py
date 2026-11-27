"""Offline contract tests for backlog candidate task identity (sweep30).

Synthetic payloads and an in-process async GET stub only: no socket, HTTP,
Redis, Neo4j, SQLite/DB/WAL, container, credential file, network mount or live
AssistX endpoint is contacted.
"""

from __future__ import annotations

from typing import Any

import pytest

from auto_assign.clients import AssistXClient
from auto_assign.settings import Settings


class _FakeResponse:
    def __init__(self, payload: Any) -> None:
        self._payload = payload
        self.status_code = 200

    def raise_for_status(self) -> None:
        return None

    def json(self) -> Any:
        return self._payload


class _FakeGetClient:
    """Minimal async GET stub; records calls without any network I/O."""

    def __init__(self, payload: Any) -> None:
        self.payload = payload
        self.calls: list[tuple[str, dict[str, Any]]] = []

    async def get(self, url: str, **kwargs: Any) -> _FakeResponse:
        self.calls.append((url, kwargs))
        return _FakeResponse(self.payload)


def _client(payload: Any = None) -> AssistXClient:
    client = AssistXClient(Settings())
    if payload is not None:
        client._client = _FakeGetClient(payload)  # type: ignore[assignment]
    return client


# --- fail-closed: a backlog item without an authoritative identity ---------


def test_missing_identifier_is_rejected() -> None:
    with pytest.raises(ValueError, match="task identifier"):
        _client()._candidate_from_item({"title": "Untitled", "prompt": "do work"})


def test_title_only_identifier_is_rejected() -> None:
    with pytest.raises(ValueError, match="task identifier"):
        _client()._candidate_from_item({"title": "only-a-title"})


def test_blank_task_id_is_rejected() -> None:
    with pytest.raises(ValueError, match="task identifier"):
        _client()._candidate_from_item({"task_id": "   ", "title": "blank"})


def test_blank_legacy_identifiers_are_rejected() -> None:
    with pytest.raises(ValueError, match="task identifier"):
        _client()._candidate_from_item({"id": "", "uuid": None, "title": "blank legacy"})


# --- controls: valid identities and normalization are preserved ------------


def test_authoritative_task_id_is_preserved() -> None:
    assert _client()._candidate_from_item({"task_id": "task-1", "title": "T"}).task_id == "task-1"


def test_legacy_id_and_uuid_are_preserved() -> None:
    client = _client()
    assert client._candidate_from_item({"id": "assistx-task-1"}).task_id == "assistx-task-1"
    assert client._candidate_from_item({"uuid": "ASS-metadata-privacy"}).task_id == (
        "ASS-metadata-privacy"
    )


def test_privacy_normalization_still_applies() -> None:
    candidate = _client()._candidate_from_item(
        {"task_id": "safe-privacy-control", "privacy": "private"}
    )
    assert candidate.local_only is True
    assert candidate.allow_cloud is False


# --- end-to-end through the async backlog fetch ----------------------------


async def test_mixed_backlog_skips_unidentified_item() -> None:
    payload = {
        "tasks": [
            {"task_id": "good-1"},
            {"title": "unidentified"},
            {"id": "good-2", "privacy": "local_only"},
        ]
    }
    got = await _client(payload).get_backlog_candidates(limit=10)
    assert [candidate.task_id for candidate in got] == ["good-1", "good-2"]


async def test_all_unidentified_backlog_yields_no_candidates() -> None:
    client = _client({"tasks": [{"title": "a"}, {"prompt": "b"}]})
    assert await client.get_backlog_candidates(limit=10) == []
