from __future__ import annotations

import logging
import sqlite3
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Callable

logger = logging.getLogger(__name__)

MigrationFn = Callable[[sqlite3.Connection], None]


@dataclass
class Migration:
    version: int
    description: str
    fn: MigrationFn


MIGRATIONS: list[Migration] = []


def _register(version: int, description: str) -> Callable[[MigrationFn], MigrationFn]:
    def decorator(fn: MigrationFn) -> MigrationFn:
        MIGRATIONS.append(Migration(version=version, description=description, fn=fn))
        return fn
    return decorator


def _ensure_version_table(conn: sqlite3.Connection) -> None:
    conn.execute(
        "CREATE TABLE IF NOT EXISTS _schema_version ("
        "  version INTEGER PRIMARY KEY,"
        "  applied_at TEXT NOT NULL DEFAULT (datetime('now')),"
        "  description TEXT NOT NULL"
        ")"
    )


def current_version(conn: sqlite3.Connection) -> int:
    _ensure_version_table(conn)
    row = conn.execute("SELECT COALESCE(MAX(version), 0) FROM _schema_version").fetchone()
    return row[0] if row else 0


def migrate(conn: sqlite3.Connection) -> list[dict[str, Any]]:
    applied: list[dict[str, Any]] = []
    current = current_version(conn)
    pending = [m for m in sorted(MIGRATIONS, key=lambda m: m.version) if m.version > current]
    for m in pending:
        logger.info("Running migration v%d: %s", m.version, m.description)
        try:
            m.fn(conn)
            conn.execute(
                "INSERT INTO _schema_version (version, description) VALUES (?, ?)",
                (m.version, m.description),
            )
            conn.commit()
            applied.append({"version": m.version, "description": m.description})
            logger.info("Migration v%d complete", m.version)
        except Exception:
            conn.rollback()
            logger.exception("Migration v%d failed", m.version)
            raise
    return applied


# ---------------------------------------------------------------------------
# Schema migrations
# ---------------------------------------------------------------------------

@_register(1, "initial schema — scheduler_runs, assignments, heartbeats, inbound_events, outbox_events")
def _migration_v1(conn: sqlite3.Connection) -> None:
    conn.executescript("""
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
        CREATE INDEX IF NOT EXISTS idx_outbox_events_status ON outbox_events(status);

        CREATE TABLE IF NOT EXISTS inbound_event_processing (
            idempotency_key TEXT PRIMARY KEY,
            event_id TEXT,
            event_type TEXT NOT NULL,
            status TEXT NOT NULL,
            action_json TEXT,
            processed_at TEXT NOT NULL,
            updated_at TEXT NOT NULL
        );
        CREATE INDEX IF NOT EXISTS idx_inbound_event_processing_status
            ON inbound_event_processing(status);
    """)
