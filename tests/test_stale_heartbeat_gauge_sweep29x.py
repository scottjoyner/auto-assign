"""Hermetic regression for the /health auto_assign_stale_heartbeats gauge (sweep29x).

Everything runs in-process against a fresh file-backed SQLite cache under pytest
tmp_path. assistx/router dependency health is stubbed; no HTTP, socket, live DB,
WAL, credential, service or credential is touched.
"""

from __future__ import annotations

from datetime import UTC, datetime, timedelta

from auto_assign.cache import CacheStore
from auto_assign.main import health
from auto_assign.metrics import STALE_HEARTBEATS
from auto_assign.scorer import AssignmentScorer
from auto_assign.service import AssignmentService
from auto_assign.settings import Settings

STALE_SECONDS = 120
_INSERT = (
    "INSERT INTO heartbeats (heartbeat_id, node_id, worker_id, assignment_id, "
    "status, received_at, payload_json) VALUES (?, ?, ?, ?, ?, ?, ?)"
)


class _HealthyAssistX:
    async def health(self):
        return {"reachable": True}


class _HealthyRouter:
    async def health(self):
        return {"reachable": True}


def _make_service(tmp_path):
    settings = Settings(
        database_url=f"sqlite:///{tmp_path / 'stale-gauge.sqlite3'}",
        stale_heartbeat_seconds=STALE_SECONDS,
    )
    cache = CacheStore(settings.sqlite_path)
    service = AssignmentService(
        settings, cache, _HealthyAssistX(), _HealthyRouter(), AssignmentScorer(settings)
    )
    return settings, cache, service


def _seed(cache, *, tag: str, stale: int = 0, fresh: int = 0) -> None:
    old = (datetime.now(UTC) - timedelta(seconds=3600)).isoformat()
    now = datetime.now(UTC).isoformat()
    with cache.connect() as conn:
        for i in range(stale):
            conn.execute(
                _INSERT,
                (f"{tag}-old-{i}", f"{tag}-node", "w", None, "online", old, "{}"),
            )
        for i in range(fresh):
            conn.execute(
                _INSERT,
                (f"{tag}-new-{i}", f"{tag}-node", "w", None, "online", now, "{}"),
            )


def _gauge() -> float:
    return STALE_HEARTBEATS._value.get()


async def test_health_gauge_reports_true_stale_count(tmp_path):
    """The public gauge must show all stale heartbeats, not just the first page."""
    _settings, cache, service = _make_service(tmp_path)
    _seed(cache, tag="count", stale=5, fresh=1)
    await health(service)
    assert _gauge() == 5
    cache.close()


async def test_health_gauge_tracks_growth(tmp_path):
    """A rising stale population must be visible on the next health scrape."""
    _settings, cache, service = _make_service(tmp_path)
    _seed(cache, tag="first", stale=2)
    await health(service)
    first = _gauge()
    _seed(cache, tag="second", stale=3)
    await health(service)
    second = _gauge()
    assert [first, second] == [2, 5]
    cache.close()


async def test_health_gauge_zero_when_no_stale(tmp_path):
    """Control: only fresh heartbeats must leave the gauge at zero."""
    _settings, cache, service = _make_service(tmp_path)
    _seed(cache, tag="fresh", stale=0, fresh=3)
    await health(service)
    assert _gauge() == 0
    cache.close()


async def test_count_query_matches_unpaged_list(tmp_path):
    """Control: the COUNT(*) cutoff must match the existing list query cutoff."""
    settings, cache, _service = _make_service(tmp_path)
    _seed(cache, tag="cutoff", stale=4, fresh=2)
    listed = cache.list_stale_heartbeats(stale_after_seconds=STALE_SECONDS, limit=100)
    cutoff = (datetime.now(UTC) - timedelta(seconds=settings.stale_heartbeat_seconds)).isoformat()
    with cache.connect() as conn:
        true_count = conn.execute(
            "SELECT COUNT(*) AS c FROM heartbeats WHERE received_at < ?", (cutoff,)
        ).fetchone()["c"]
    assert len(listed) == 4
    assert true_count == 4
    cache.close()
