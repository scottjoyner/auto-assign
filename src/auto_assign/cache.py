from __future__ import annotations

import json
import sqlite3
from datetime import timedelta
from pathlib import Path
from typing import Any

from .models import AssignmentDecision, EventEnvelope, HeartbeatRequest, utc_now


class CacheStore:
    """SQLite cache/outbox layer.

    This is intentionally not a source of truth. Neo4j via AssistX is the durable
    brain. Rows here are local mirrors, retry buffers, and operational cache only.
    """

    def __init__(self, path: Path):
        self.path = path
        self.path.parent.mkdir(parents=True, exist_ok=True)
        self._init_schema()

    def connect(self) -> sqlite3.Connection:
        conn = sqlite3.connect(self.path)
        conn.row_factory = sqlite3.Row
        return conn

    def _init_schema(self) -> None:
        with self.connect() as conn:
            conn.executescript(
                """
                CREATE TABLE IF NOT EXISTS scheduler_runs (
                    scheduler_run_id TEXT PRIMARY KEY,
                    started_at TEXT NOT NULL,
                    completed_at TEXT,
                    trigger_reason TEXT NOT NULL,
                    dry_run INTEGER NOT NULL,
                    evaluated_count INTEGER NOT NULL DEFAULT 0,
                    recommended_count INTEGER NOT NULL DEFAULT 0,
                    skipped_count INTEGER NOT NULL DEFAULT 0,
                    error_summary TEXT
                );

                CREATE TABLE IF NOT EXISTS assignments (
                    assignment_id TEXT PRIMARY KEY,
                    task_id TEXT NOT NULL,
                    decision_id TEXT NOT NULL,
                    status TEXT NOT NULL,
                    selected_lane TEXT NOT NULL,
                    selected_target TEXT,
                    score REAL NOT NULL,
                    approval_required INTEGER NOT NULL,
                    lease_expires_at TEXT,
                    context_revision TEXT,
                    canonical_status TEXT,
                    cache_only INTEGER NOT NULL DEFAULT 1,
                    idempotency_key TEXT NOT NULL UNIQUE,
                    payload_json TEXT NOT NULL,
                    created_at TEXT NOT NULL,
                    updated_at TEXT NOT NULL
                );
                CREATE INDEX IF NOT EXISTS idx_assignments_task_id ON assignments(task_id);
                CREATE INDEX IF NOT EXISTS idx_assignments_status ON assignments(status);

                CREATE TABLE IF NOT EXISTS heartbeats (
                    heartbeat_id TEXT PRIMARY KEY,
                    node_id TEXT NOT NULL,
                    worker_id TEXT,
                    assignment_id TEXT,
                    status TEXT NOT NULL,
                    received_at TEXT NOT NULL,
                    payload_json TEXT NOT NULL
                );
                CREATE INDEX IF NOT EXISTS idx_heartbeats_node_id ON heartbeats(node_id);
                CREATE INDEX IF NOT EXISTS idx_heartbeats_assignment_id ON heartbeats(assignment_id);

                CREATE TABLE IF NOT EXISTS inbound_events (
                    event_id TEXT PRIMARY KEY,
                    event_type TEXT NOT NULL,
                    source_service TEXT NOT NULL,
                    idempotency_key TEXT NOT NULL UNIQUE,
                    subject TEXT NOT NULL,
                    payload_json TEXT NOT NULL,
                    received_at TEXT NOT NULL,
                    created_at TEXT NOT NULL,
                    updated_at TEXT NOT NULL
                );
                CREATE INDEX IF NOT EXISTS idx_inbound_events_type ON inbound_events(event_type);
                CREATE INDEX IF NOT EXISTS idx_inbound_events_source ON inbound_events(source_service);

                CREATE TABLE IF NOT EXISTS outbox_events (
                    event_id TEXT PRIMARY KEY,
                    event_type TEXT NOT NULL,
                    idempotency_key TEXT NOT NULL UNIQUE,
                    subject TEXT NOT NULL,
                    payload_json TEXT NOT NULL,
                    status TEXT NOT NULL DEFAULT 'pending',
                    attempts INTEGER NOT NULL DEFAULT 0,
                    next_attempt_at TEXT,
                    last_error TEXT,
                    created_at TEXT NOT NULL,
                    updated_at TEXT NOT NULL
                );
                CREATE INDEX IF NOT EXISTS idx_outbox_status ON outbox_events(status);
                """
            )

    def health(self) -> dict[str, Any]:
        try:
            with self.connect() as conn:
                conn.execute("SELECT 1").fetchone()
            return {"reachable": True, "path": str(self.path), "role": "cache_outbox_only"}
        except Exception as exc:  # pragma: no cover
            return {"reachable": False, "path": str(self.path), "error": str(exc), "role": "cache_outbox_only"}

    def record_scheduler_run(
        self,
        scheduler_run_id: str,
        trigger_reason: str,
        dry_run: bool,
        evaluated_count: int,
        recommended_count: int,
        skipped_count: int,
        error_summary: str | None = None,
    ) -> None:
        now = utc_now().isoformat()
        with self.connect() as conn:
            conn.execute(
                """
                INSERT OR REPLACE INTO scheduler_runs (
                    scheduler_run_id, started_at, completed_at, trigger_reason,
                    dry_run, evaluated_count, recommended_count, skipped_count,
                    error_summary
                ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)
                """,
                (
                    scheduler_run_id,
                    now,
                    now,
                    trigger_reason,
                    int(dry_run),
                    evaluated_count,
                    recommended_count,
                    skipped_count,
                    error_summary,
                ),
            )

    def list_scheduler_runs(self, limit: int = 50) -> list[dict[str, Any]]:
        with self.connect() as conn:
            rows = conn.execute(
                """
                SELECT scheduler_run_id, started_at, completed_at, trigger_reason,
                       dry_run, evaluated_count, recommended_count, skipped_count,
                       error_summary
                FROM scheduler_runs
                ORDER BY completed_at DESC
                LIMIT ?
                """,
                (limit,),
            ).fetchall()
        return [dict(row) for row in rows]

    def upsert_assignment(self, decision: AssignmentDecision) -> None:
        now = utc_now().isoformat()
        payload = decision.model_dump(mode="json")
        with self.connect() as conn:
            conn.execute(
                """
                INSERT INTO assignments (
                    assignment_id, task_id, decision_id, status, selected_lane,
                    selected_target, score, approval_required, lease_expires_at,
                    context_revision, canonical_status, cache_only, idempotency_key,
                    payload_json, created_at, updated_at
                ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                ON CONFLICT(idempotency_key) DO UPDATE SET
                    decision_id=excluded.decision_id,
                    status=excluded.status,
                    selected_lane=excluded.selected_lane,
                    selected_target=excluded.selected_target,
                    score=excluded.score,
                    approval_required=excluded.approval_required,
                    lease_expires_at=excluded.lease_expires_at,
                    context_revision=excluded.context_revision,
                    canonical_status=excluded.canonical_status,
                    cache_only=excluded.cache_only,
                    payload_json=excluded.payload_json,
                    updated_at=excluded.updated_at
                """,
                (
                    decision.assignment_id,
                    decision.task_id,
                    decision.decision_id,
                    decision.status.value,
                    decision.selected_lane.value,
                    decision.selected_target,
                    decision.score,
                    int(decision.approval_required),
                    decision.lease_expires_at.isoformat() if decision.lease_expires_at else None,
                    decision.context_revision,
                    decision.canonical_status,
                    int(decision.cache_only),
                    decision.idempotency_key,
                    json.dumps(payload, sort_keys=True),
                    decision.created_at.isoformat(),
                    now,
                ),
            )

    def list_assignments(self, limit: int = 50) -> list[dict[str, Any]]:
        with self.connect() as conn:
            rows = conn.execute(
                "SELECT payload_json FROM assignments ORDER BY updated_at DESC LIMIT ?", (limit,)
            ).fetchall()
        return [json.loads(row["payload_json"]) for row in rows]

    def get_assignment(self, assignment_id: str) -> dict[str, Any] | None:
        with self.connect() as conn:
            row = conn.execute(
                "SELECT payload_json FROM assignments WHERE assignment_id = ?", (assignment_id,)
            ).fetchone()
        return json.loads(row["payload_json"]) if row else None

    def record_heartbeat(self, heartbeat_id: str, heartbeat: HeartbeatRequest) -> None:
        now = utc_now().isoformat()
        with self.connect() as conn:
            conn.execute(
                """
                INSERT OR REPLACE INTO heartbeats (
                    heartbeat_id, node_id, worker_id, assignment_id, status,
                    received_at, payload_json
                ) VALUES (?, ?, ?, ?, ?, ?, ?)
                """,
                (
                    heartbeat_id,
                    heartbeat.node_id,
                    heartbeat.worker_id,
                    heartbeat.assignment_id,
                    heartbeat.status,
                    now,
                    heartbeat.model_dump_json(),
                ),
            )

    def list_heartbeats(self, limit: int = 50) -> list[dict[str, Any]]:
        with self.connect() as conn:
            rows = conn.execute(
                """
                SELECT heartbeat_id, node_id, worker_id, assignment_id, status,
                       received_at, payload_json
                FROM heartbeats
                ORDER BY received_at DESC
                LIMIT ?
                """,
                (limit,),
            ).fetchall()
        return [{**dict(row), "payload": json.loads(row["payload_json"])} for row in rows]

    def list_stale_heartbeats(self, stale_after_seconds: int, limit: int = 50) -> list[dict[str, Any]]:
        cutoff = (utc_now() - timedelta(seconds=stale_after_seconds)).isoformat()
        with self.connect() as conn:
            rows = conn.execute(
                """
                SELECT heartbeat_id, node_id, worker_id, assignment_id, status,
                       received_at, payload_json
                FROM heartbeats
                WHERE received_at < ?
                ORDER BY received_at ASC
                LIMIT ?
                """,
                (cutoff, limit),
            ).fetchall()
        return [{**dict(row), "payload": json.loads(row["payload_json"])} for row in rows]

    def record_inbound_event(self, event: EventEnvelope) -> None:
        now = utc_now().isoformat()
        with self.connect() as conn:
            conn.execute(
                """
                INSERT INTO inbound_events (
                    event_id, event_type, source_service, idempotency_key, subject,
                    payload_json, received_at, created_at, updated_at
                ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)
                ON CONFLICT(idempotency_key) DO UPDATE SET
                    event_type=excluded.event_type,
                    source_service=excluded.source_service,
                    subject=excluded.subject,
                    payload_json=excluded.payload_json,
                    received_at=excluded.received_at,
                    updated_at=excluded.updated_at
                """,
                (
                    event.event_id,
                    event.event_type,
                    event.source_service,
                    event.idempotency_key,
                    event.subject,
                    event.model_dump_json(),
                    now,
                    now,
                    now,
                ),
            )

    def list_inbound_events(self, limit: int = 50, event_type: str | None = None) -> list[dict[str, Any]]:
        sql = """
            SELECT event_id, event_type, source_service, idempotency_key, subject,
                   payload_json, received_at, created_at, updated_at
            FROM inbound_events
        """
        params: tuple[Any, ...]
        if event_type:
            sql += " WHERE event_type = ?"
            params = (event_type, limit)
        else:
            params = (limit,)
        sql += " ORDER BY received_at DESC LIMIT ?"
        with self.connect() as conn:
            rows = conn.execute(sql, params).fetchall()
        return [{**dict(row), "payload": json.loads(row["payload_json"])} for row in rows]

    def inbound_summary(self) -> dict[str, int]:
        with self.connect() as conn:
            rows = conn.execute(
                "SELECT event_type, COUNT(*) AS count FROM inbound_events GROUP BY event_type"
            ).fetchall()
        return {row["event_type"]: int(row["count"]) for row in rows}

    def enqueue_event(self, event: EventEnvelope) -> None:
        """Queue or refresh an outbound event.

        Pending/failed events with the same idempotency key are refreshed with the
        latest payload. Delivered/dead-letter events are not changed by normal
        enqueue calls, so canonical write-back history is not silently rewritten.
        """
        now = utc_now().isoformat()
        with self.connect() as conn:
            conn.execute(
                """
                INSERT INTO outbox_events (
                    event_id, event_type, idempotency_key, subject, payload_json,
                    status, attempts, created_at, updated_at
                ) VALUES (?, ?, ?, ?, ?, 'pending', 0, ?, ?)
                ON CONFLICT(idempotency_key) DO UPDATE SET
                    event_id=excluded.event_id,
                    event_type=excluded.event_type,
                    subject=excluded.subject,
                    payload_json=excluded.payload_json,
                    updated_at=excluded.updated_at
                WHERE outbox_events.status IN ('pending', 'failed')
                """,
                (
                    event.event_id,
                    event.event_type,
                    event.idempotency_key,
                    event.subject,
                    event.model_dump_json(),
                    now,
                    now,
                ),
            )

    def pending_events(self, limit: int = 25) -> list[EventEnvelope]:
        with self.connect() as conn:
            rows = conn.execute(
                """
                SELECT payload_json FROM outbox_events
                WHERE status IN ('pending', 'failed')
                  AND (next_attempt_at IS NULL OR next_attempt_at <= ?)
                ORDER BY created_at ASC
                LIMIT ?
                """,
                (utc_now().isoformat(), limit),
            ).fetchall()
        return [EventEnvelope.model_validate_json(row["payload_json"]) for row in rows]

    def list_outbox_events(self, limit: int = 50, status: str | None = None) -> list[dict[str, Any]]:
        sql = """
            SELECT event_id, event_type, idempotency_key, subject, payload_json,
                   status, attempts, next_attempt_at, last_error, created_at, updated_at
            FROM outbox_events
        """
        params: tuple[Any, ...]
        if status:
            sql += " WHERE status = ?"
            params = (status, limit)
        else:
            params = (limit,)
        sql += " ORDER BY created_at DESC LIMIT ?"
        with self.connect() as conn:
            rows = conn.execute(sql, params).fetchall()
        return [{**dict(row), "payload": json.loads(row["payload_json"])} for row in rows]

    def mark_event_delivered(self, idempotency_key: str) -> None:
        now = utc_now().isoformat()
        with self.connect() as conn:
            conn.execute(
                """
                UPDATE outbox_events
                SET status='delivered', updated_at=?, last_error=NULL
                WHERE idempotency_key=?
                """,
                (now, idempotency_key),
            )

    def mark_event_failed(self, idempotency_key: str, error: str, dead_letter: bool = False) -> None:
        now = utc_now().isoformat()
        status = "dead_letter" if dead_letter else "failed"
        with self.connect() as conn:
            conn.execute(
                """
                UPDATE outbox_events
                SET status=?, attempts=attempts + 1, updated_at=?, last_error=?
                WHERE idempotency_key=?
                """,
                (status, now, error[:500], idempotency_key),
            )

    def reconcile_delivered(self, idempotency_keys: list[str]) -> int:
        count = 0
        for key in idempotency_keys:
            self.mark_event_delivered(key)
            count += 1
        return count

    def outbox_summary(self) -> dict[str, int]:
        with self.connect() as conn:
            rows = conn.execute(
                "SELECT status, COUNT(*) AS count FROM outbox_events GROUP BY status"
            ).fetchall()
        return {row["status"]: int(row["count"]) for row in rows}

    def ops_summary(self) -> dict[str, Any]:
        with self.connect() as conn:
            assignment_rows = conn.execute(
                "SELECT status, COUNT(*) AS count FROM assignments GROUP BY status"
            ).fetchall()
            latest_scheduler = conn.execute(
                "SELECT completed_at FROM scheduler_runs ORDER BY completed_at DESC LIMIT 1"
            ).fetchone()
            latest_heartbeat = conn.execute(
                "SELECT received_at FROM heartbeats ORDER BY received_at DESC LIMIT 1"
            ).fetchone()
            latest_inbound = conn.execute(
                "SELECT received_at FROM inbound_events ORDER BY received_at DESC LIMIT 1"
            ).fetchone()
            heartbeat_count = conn.execute("SELECT COUNT(*) AS count FROM heartbeats").fetchone()
            inbound_count = conn.execute("SELECT COUNT(*) AS count FROM inbound_events").fetchone()
        return {
            "cache_role": "cache_outbox_only",
            "canonical_source": "neo4j_via_assistx",
            "assignments_by_status": {
                row["status"]: int(row["count"]) for row in assignment_rows
            },
            "outbox_by_status": self.outbox_summary(),
            "inbound_by_type": self.inbound_summary(),
            "inbound_events": {
                "count": int(inbound_count["count"]) if inbound_count else 0,
                "latest_received_at": latest_inbound["received_at"] if latest_inbound else None,
            },
            "heartbeats": {
                "count": int(heartbeat_count["count"]) if heartbeat_count else 0,
                "latest_received_at": latest_heartbeat["received_at"] if latest_heartbeat else None,
            },
            "scheduler": {
                "latest_completed_at": latest_scheduler["completed_at"] if latest_scheduler else None,
            },
        }
