from __future__ import annotations

import json
import sqlite3
from contextlib import closing
from pathlib import Path
from urllib.parse import quote
from typing import Any

from .models import AssignmentControlMode, AssignmentControlRequest, EventEnvelope, utc_now


def recommended_scheduler_state(mode: AssignmentControlMode | str) -> str:
    value = str(mode)
    return {
        AssignmentControlMode.ENABLED.value: "active",
        AssignmentControlMode.PAUSED.value: "paused",
        AssignmentControlMode.DRAINING.value: "draining",
        AssignmentControlMode.MAINTENANCE.value: "maintenance",
    }.get(value, "paused")


def control_state_for_mode(
    mode: AssignmentControlMode | str,
    *,
    reason: str | None = None,
    updated_by: str | None = None,
    updated_at: str | None = None,
    metadata: dict[str, Any] | None = None,
) -> dict[str, Any]:
    value = str(mode)
    return {
        "mode": value,
        "assignment_allowed": value == AssignmentControlMode.ENABLED.value,
        "new_assignments_allowed": value == AssignmentControlMode.ENABLED.value,
        "scheduler_ticks_allowed": value == AssignmentControlMode.ENABLED.value,
        "lease_renewals_allowed": value in {AssignmentControlMode.ENABLED.value, AssignmentControlMode.DRAINING.value},
        "dispatch_allowed": value == AssignmentControlMode.ENABLED.value,
        "recommended_scheduler_state": recommended_scheduler_state(value),
        "reason": reason,
        "updated_by": updated_by,
        "updated_at": updated_at,
        "metadata": metadata or {},
        "cache_role": "assignment_control_cache",
        "canonical_source": "neo4j_via_assistx",
    }


class AssignmentControlStore:
    """Small cache/outbox-backed assignment control store.

    The local SQLite row is an operational mirror. The durable/canonical control
    history is the `assign.control.changed` event emitted through the outbox to
    AssistX/Neo4j.
    """

    def __init__(self, sqlite_path: Path):
        self.sqlite_path = sqlite_path
        self.sqlite_path.parent.mkdir(parents=True, exist_ok=True)
        self._init_schema()

    def _connect(self) -> sqlite3.Connection:
        conn = sqlite3.connect(self.sqlite_path)
        conn.row_factory = sqlite3.Row
        return conn

    def _init_schema(self) -> None:
        with self._connect() as conn:
            conn.execute(
                """
                CREATE TABLE IF NOT EXISTS assignment_control (
                    id TEXT PRIMARY KEY,
                    mode TEXT NOT NULL,
                    reason TEXT,
                    updated_by TEXT,
                    updated_at TEXT NOT NULL,
                    metadata_json TEXT NOT NULL
                )
                """
            )

    def get(self) -> dict[str, Any]:
        with self._connect() as conn:
            row = conn.execute("SELECT * FROM assignment_control WHERE id='global'").fetchone()
        if not row:
            return control_state_for_mode(
                AssignmentControlMode.ENABLED,
                reason="default enabled; no local assignment_control row exists yet",
            )
        metadata = json.loads(row["metadata_json"] or "{}")
        return control_state_for_mode(
            row["mode"],
            reason=row["reason"],
            updated_by=row["updated_by"],
            updated_at=row["updated_at"],
            metadata=metadata,
        )

    @classmethod
    def peek_read_only(cls, sqlite_path: Path) -> dict[str, Any]:
        """Read control without DDL, file creation, or a writable connection.

        CacheStore initializes its own schema, but a first call to the normal
        AssignmentControlStore constructor performs CREATE TABLE IF NOT EXISTS.
        A scheduler preview must never invoke that constructor.
        """
        db_path = sqlite_path.resolve()
        uri = "file:" + quote(str(db_path), safe="/") + "?mode=ro"
        try:
            with closing(sqlite3.connect(uri, uri=True, timeout=3)) as conn:
                conn.row_factory = sqlite3.Row
                conn.execute("PRAGMA query_only=ON")
                # Mirror historical default on an initialized cache where
                # the control table has never been initialized.
                exists = conn.execute(
                    "SELECT 1 FROM sqlite_master WHERE type='table' "
                    "AND name='assignment_control'"
                ).fetchone()
                row = (
                    conn.execute(
                        "SELECT * FROM assignment_control WHERE id='global'"
                    ).fetchone()
                    if exists else None
                )
        except sqlite3.Error:
            # Do not fabricate an enabled control state if the cache itself
            # is unreadable. A preview can safely return a blocked decision.
            return control_state_for_mode(
                AssignmentControlMode.PAUSED,
                reason="assignment control unavailable in read-only preview",
            )
        if row is None:
            return control_state_for_mode(
                AssignmentControlMode.ENABLED,
                reason="default enabled; no local assignment_control row exists yet",
            )
        return control_state_for_mode(
            row["mode"],
            reason=row["reason"],
            updated_by=row["updated_by"],
            updated_at=row["updated_at"],
            metadata=json.loads(row["metadata_json"] or "{}"),
        )

    def set(self, request: AssignmentControlRequest) -> tuple[dict[str, Any], EventEnvelope]:
        now = utc_now().isoformat()
        state = control_state_for_mode(
            request.mode,
            reason=request.reason,
            updated_by=request.updated_by,
            updated_at=now,
            metadata=request.metadata,
        )
        with self._connect() as conn:
            conn.execute(
                """
                INSERT INTO assignment_control (id, mode, reason, updated_by, updated_at, metadata_json)
                VALUES ('global', ?, ?, ?, ?, ?)
                ON CONFLICT(id) DO UPDATE SET
                    mode=excluded.mode,
                    reason=excluded.reason,
                    updated_by=excluded.updated_by,
                    updated_at=excluded.updated_at,
                    metadata_json=excluded.metadata_json
                """,
                (
                    request.mode.value,
                    request.reason,
                    request.updated_by,
                    now,
                    json.dumps(request.metadata or {}, sort_keys=True),
                ),
            )
        event = EventEnvelope(
            event_type="assign.control.changed",
            idempotency_key=f"assign.control.changed:{now}:{request.mode.value}",
            subject="assignment-control:global",
            payload={
                "mode": request.mode.value,
                "reason": request.reason,
                "updated_by": request.updated_by,
                "updated_at": now,
                "metadata": request.metadata or {},
                "dry_run": request.dry_run,
            },
        )
        return state, event
