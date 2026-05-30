# Assignment Governor Status

## 1. Purpose

`auto-assign` exposes an assignment-focused status endpoint for operators and future dashboards.

This endpoint is separate from the generic ops summary. It answers the core governor questions:

- what assignment decisions exist locally;
- what assign.* events are waiting for AssistX/Neo4j materialization;
- whether workers are heartbeating;
- whether any heartbeats are stale;
- whether the scheduler has run recently;
- whether dispatch/direct workers remain safely disabled.

The endpoint is a local read model only. Neo4j via AssistX remains canonical.

## 2. Endpoint

```text
GET /api/assignments/summary
GET /api/assignments/summary?limit=25
```

Response shape:

```json
{
  "source": "sqlite_cache_mirror",
  "canonical_source": "neo4j_via_assistx",
  "cache_role": "assignment_governor_read_model",
  "assignments_by_status": {
    "recommended": 3,
    "approval_required": 1
  },
  "outbox_by_status": {
    "pending": 4
  },
  "inbound_by_type": {
    "router.service_snapshot.recorded": 1
  },
  "heartbeats": {
    "count": 2,
    "latest_received_at": "2026-05-30T16:00:00Z",
    "stale_count": 0,
    "stale_after_seconds": 120
  },
  "scheduler": {
    "latest_completed_at": "2026-05-30T16:00:00Z",
    "enabled": false,
    "last_tick_at": "2026-05-30T16:00:00Z"
  },
  "safety": {
    "dispatch_enabled": false,
    "direct_workers_enabled": false,
    "cache_is_canonical": false,
    "canonical_source": "neo4j_via_assistx"
  },
  "recent_assignments": [],
  "stale_heartbeats": [],
  "recommendations": []
}
```

## 3. Recommendation actions

| Action | Meaning |
|---|---|
| `dispatch_or_reconcile_outbox` | Pending `assign.*` events should be dispatched or reconciled with AssistX/Neo4j. |
| `inspect_outbox_failures` | Failed or dead-lettered outbox events need operator review. |
| `inspect_stale_heartbeats` | One or more workers/nodes are stale and should not receive new assignments. |
| `review_required_approvals` | Some assignments are waiting on approval before future dispatch. |
| `dry_run_only` | Dispatch is disabled; service is operating as a dry-run governor. |
| `direct_workers_disabled` | Direct worker lane is disabled pending sandbox/approval/artifact controls. |
| `steady_state` | No local cache, outbox, or heartbeat risks detected. |

## 4. Safety guarantees

This endpoint does not:

- dispatch work;
- mutate assignments;
- mark events delivered;
- execute direct workers;
- write repository files;
- override AssistX/Neo4j canonical state.

It only aggregates local SQLite cache/outbox data and safety settings into a dashboard-friendly read model.

## 5. Recommended operator loop

```bash
curl 'http://localhost:8090/api/assignments/summary' | jq
curl 'http://localhost:8090/api/outbox/summary' | jq
curl -X POST 'http://localhost:8090/api/outbox/reconcile' | jq
curl -X POST 'http://localhost:8090/api/outbox/dispatch?dry_run=true' | jq
```

Only run non-dry-run outbox dispatch when AssistX event materialization has been validated.
