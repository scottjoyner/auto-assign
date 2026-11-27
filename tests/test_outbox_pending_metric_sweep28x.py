"""Hermetic contract: /health must publish the real pending-outbox count.

`sweep28x` — vacuous metric defect. No socket, HTTP, Redis, Neo4j, live DB/WAL,
credential, service or mount is touched; only an in-process temporary SQLite
file under pytest's scratch basetemp and the pure FastAPI endpoint callable.
"""

from __future__ import annotations

import asyncio

from auto_assign import main as main_mod
from auto_assign.cache import CacheStore
from auto_assign.models import EventEnvelope


class _StubDependency:
    async def health(self):
        return {"reachable": True}


class _StubSettings:
    def __init__(self, sqlite_path):
        self.sqlite_path = sqlite_path
        self.scheduler_enabled = False
        self.dispatch_enabled = False
        self.direct_workers_enabled = False


class _StubService:
    """Only the attributes the /health handler reads."""

    def __init__(self, cache: CacheStore, sqlite_path):
        self.cache = cache
        self.settings = _StubSettings(sqlite_path)
        self.assistx = _StubDependency()
        self.router = _StubDependency()
        self.last_tick_at = None

    def list_stale_heartbeats(self, limit=50):
        return {"count": 0, "heartbeats": []}


def _enqueue(cache: CacheStore, count: int) -> None:
    for i in range(count):
        cache.enqueue_event(
            EventEnvelope(
                event_type="assign.test",
                idempotency_key=f"sweep28x-outbox-pending-{i}",
                subject={"kind": "task", "id": f"t{i}"},
            )
        )


def _health_calls(tmp_path, monkeypatch, cache: CacheStore) -> list[int]:
    """Run the real endpoint callable; capture each gauge .set() argument."""
    service = _StubService(cache, tmp_path / "control.sqlite3")
    seen: list[int] = []
    monkeypatch.setattr(main_mod.OUTBOX_PENDING, "set", seen.append)
    response = asyncio.run(main_mod.health(service))
    assert response.ok is True
    return seen


def test_cache_health_returns_a_plain_dict(tmp_path):
    """Root cause: CacheStore.health() is a dict, never a store object."""
    cache = CacheStore(tmp_path / "cache.sqlite3")
    result = cache.health()
    assert isinstance(result, dict)
    assert not hasattr(result, "outbox_summary")


def test_health_publishes_real_pending_count(tmp_path, monkeypatch):
    cache = CacheStore(tmp_path / "cache.sqlite3")
    _enqueue(cache, 3)
    assert cache.outbox_summary().get("pending", 0) == 3
    seen = _health_calls(tmp_path, monkeypatch, cache)
    assert seen[-1] == 3


def test_health_gauge_tracks_outbox_growth(tmp_path, monkeypatch):
    cache = CacheStore(tmp_path / "cache.sqlite3")
    service = _StubService(cache, tmp_path / "control.sqlite3")
    seen: list[int] = []
    monkeypatch.setattr(main_mod.OUTBOX_PENDING, "set", seen.append)
    asyncio.run(main_mod.health(service))
    _enqueue(cache, 2)
    asyncio.run(main_mod.health(service))
    assert seen == [0, 2]


def test_health_publishes_zero_when_outbox_empty(tmp_path, monkeypatch):
    cache = CacheStore(tmp_path / "cache.sqlite3")
    seen = _health_calls(tmp_path, monkeypatch, cache)
    assert seen[-1] == 0


def test_health_does_not_count_delivered_events(tmp_path, monkeypatch):
    cache = CacheStore(tmp_path / "cache.sqlite3")
    _enqueue(cache, 1)
    cache.mark_event_delivered("sweep28x-outbox-pending-0")
    assert cache.outbox_summary().get("pending", 0) == 0
    seen = _health_calls(tmp_path, monkeypatch, cache)
    assert seen[-1] == 0
