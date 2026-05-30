# Interface Contracts: auto-assign, auto-assist, and auto-router

## 1. Purpose

This document defines the initial API and event contracts for `auto-assign` integrations.

The goal is to make the next implementation pass deterministic and safe:

- `auto-assign` reads candidates and canonical policy state from AssistX.
- `auto-assign` reads context/quota/capability state from auto-router.
- `auto-assign` emits idempotent `assign.*` events back to AssistX.
- `auto-assign` never stores canonical task/assignment state in SQLite.
- `auto-assign` never dispatches or mutates external systems during P0/P1.

## 2. Shared event envelope

Every outbound event from `auto-assign` should use this envelope.

```json
{
  "event_id": "uuid",
  "event_type": "assign.assignment.recommended",
  "source_service": "auto-assign",
  "schema_version": "assign.v1",
  "occurred_at": "2026-05-30T16:00:00Z",
  "idempotency_key": "assign.assignment.recommended:task-123:decision-hash",
  "subject": {
    "type": "task",
    "id": "task-123"
  },
  "privacy": {
    "labels": ["non_sensitive"],
    "local_only": false,
    "sensitive": false
  },
  "payload": {}
}
```

Rules:

- `event_id` is unique per event creation.
- `idempotency_key` is stable for the same logical event.
- `payload` must not contain raw prompts, secrets, voiceprints, enrollment samples, or response bodies.
- `subject` should identify the canonical graph object affected by the event.
- AssistX/Neo4j is the canonical materialization target.

## 3. AssistX read contracts

### 3.1 Health

```text
GET /health
```

Required behavior:

- returns `200` when AssistX API is reachable;
- includes enough info for health display if available.

### 3.2 Candidate intake

```text
GET /api/router/backlog-candidates?limit=25&queue=backlog&dry_run=true
```

Expected response shapes accepted by `auto-assign`:

```json
{"tasks": []}
```

or:

```json
[]
```

Candidate shape:

```json
{
  "task_id": "task-123",
  "title": "Review router docs",
  "prompt": "safe summary or non-sensitive prompt only",
  "status": "READY",
  "priority": "background",
  "risk_level": "low",
  "approval_required": false,
  "privacy": "",
  "privacy_labels": [],
  "local_only": false,
  "sensitive": false,
  "allow_cloud": true,
  "required_capabilities": ["docs"],
  "retry_count": 0,
  "queue": "backlog",
  "created_at": "2026-05-30T15:00:00Z",
  "metadata": {
    "assistx_source": true
  }
}
```

Required safety behavior:

- If `local_only=true`, cloud/free API lanes must be skipped.
- If `sensitive=true`, cloud/free API lanes must be skipped.
- If `privacy` is `private`, `secret`, `voice_auth`, `enrollment`, or `enrollment_sample`, cloud/free API lanes must be skipped and approval may be required.
- If candidate lacks fields, `auto-assign` must default conservative.

### 3.3 Context projection

```text
GET /api/router/context-projection
```

Used as AssistX-side graph/service context. `auto-assign` should treat this as canonical task ecosystem context, not as model routing authority.

Important fields:

- `revision`;
- `source`;
- `generated_at`;
- `nodes`;
- `providers`;
- `services`;
- `metadata.graph`.

## 4. AssistX write contract

### 4.1 Event sink

```text
POST /api/events
```

`GET /api/events` returning `405` is acceptable and means the route likely exists as POST-only.

Expected behavior:

- accepts idempotent event envelopes;
- returns `2xx` for accepted events;
- returns `409` for already-applied duplicate idempotency keys where applicable;
- never requires raw prompt bodies for assignment materialization.

## 5. auto-router read contracts

### 5.1 Health

```text
GET /health
```

Required use:

- dependency health;
- conservative fallback if unreachable.

### 5.2 Context

```text
GET /admin/context
```

Used for node/provider/service context.

Important fields:

- context revision;
- known nodes;
- known providers;
- service URLs/status;
- lane hints such as `local`, `free_api`, `blocked`.

### 5.3 Quota

```text
GET /admin/quota
```

Used to decide whether `free_api` or `router_model` lane should be considered.

Rules:

- Router owns quota internals.
- `auto-assign` should not reserve provider quota directly.
- If quota endpoint is unavailable, treat hosted/free lanes as degraded or blocked unless task is explicitly safe and local fallback exists.

### 5.4 Circuits

```text
GET /admin/circuits
```

Used to skip providers/services that router marks degraded/open.

### 5.5 Services

```text
GET /admin/services
```

Used to understand service availability, including local services and Paperclip/Paperclip-like lanes.

### 5.6 Agent CLIs

```text
GET /admin/agent-clis
```

Used as capability input only. CLI discovery does not authorize execution.

### 5.7 Ops summary

```text
GET /admin/ops/summary
```

Useful combined status for:

- model registry;
- outbox;
- services;
- CLI discovery;
- AssistX task/event config.

### 5.8 Optional backlog dry-run

```text
POST /admin/backlog/dry-run
```

Optional route preview. `auto-assign` can use it for advisory scoring but must not treat it as canonical assignment authority.

## 6. auto-assign API contract

### 6.1 Health

```text
GET /health
```

Response:

```json
{
  "status": "ok",
  "service": "auto-assign",
  "version": "0.1.0",
  "assistx": {"reachable": true, "latency_ms": 12},
  "router": {"reachable": true, "latency_ms": 9},
  "store": {"reachable": true, "cache_only": true},
  "dispatch_enabled": false,
  "direct_workers_enabled": false
}
```

### 6.2 Assignment evaluation

```text
POST /api/assignments/evaluate
```

Request:

```json
{
  "task_id": "task-123",
  "candidate": null,
  "dry_run": true,
  "candidate_lanes": ["paperclip", "router_model", "local_only", "free_api", "direct_worker"],
  "force_refresh_context": false
}
```

Response:

```json
{
  "assignment_id": "assign_task-123_abc123",
  "task_id": "task-123",
  "status": "recommended",
  "selected_lane": "paperclip",
  "selected_target": "hermes_local",
  "approval_required": false,
  "score": 0.87,
  "reasons": [
    "paperclip is current approved cutover lane",
    "task is non-sensitive",
    "worker heartbeat is fresh"
  ],
  "skipped_lanes": [
    {"lane": "free_api", "reason_code": "not_needed", "reason": "higher-priority safe lane available"},
    {"lane": "direct_worker", "reason_code": "disabled", "reason": "direct workers disabled"}
  ],
  "dry_run": true,
  "event_id": "uuid-or-null",
  "idempotency_key": "assign.assignment.recommended:task-123:abc123"
}
```

### 6.3 Scheduler tick

```text
POST /api/scheduler/tick
```

Request:

```json
{
  "dry_run": true,
  "limit": 25,
  "reason": "manual_operator_tick",
  "include_blocked": false,
  "task_ids": []
}
```

Response:

```json
{
  "scheduler_run_id": "tick_20260530_001",
  "dry_run": true,
  "evaluated": 12,
  "recommended": 3,
  "approval_required": 2,
  "skipped": 7,
  "released_expired": 0,
  "decisions": []
}
```

### 6.4 Heartbeats

```text
POST /api/heartbeats
```

Initial P2 shape:

```json
{
  "node_id": "x1-370",
  "worker_id": "paperclip-hermes-local",
  "assignment_id": "assign_task-123_abc123",
  "status": "running",
  "capabilities": ["code", "terminal", "docs"],
  "services": [],
  "metrics": {
    "active_jobs": 1
  }
}
```

### 6.5 Assignment control

Future endpoint mirroring current AssistX passive control:

```text
GET /api/assignment-control
POST /api/assignment-control
```

Modes:

- `enabled`;
- `paused`;
- `draining`;
- `maintenance`.

## 7. Initial assign.* event payloads

### 7.1 `assign.assignment.recommended`

```json
{
  "assignment_id": "assign_task-123_abc123",
  "task_id": "task-123",
  "decision_id": "decision_task-123_abc123",
  "selected_lane": "paperclip",
  "selected_target": "hermes_local",
  "score": 0.87,
  "approval_required": false,
  "reasons": ["paperclip is current approved cutover lane"],
  "skipped_lanes": [
    {"lane": "direct_worker", "reason_code": "disabled", "reason": "direct workers disabled"}
  ],
  "context_revision": "router-rev-123",
  "dry_run": true
}
```

### 7.2 `assign.assignment.skipped`

```json
{
  "task_id": "task-123",
  "reason_code": "privacy_cloud_denied",
  "reason": "local-only/private task cannot use hosted free API lane",
  "candidate_lanes": ["paperclip", "router_model", "free_api"],
  "context_revision": "router-rev-123",
  "dry_run": true
}
```

### 7.3 `assign.scheduler.tick.completed`

```json
{
  "scheduler_run_id": "tick_20260530_001",
  "trigger_reason": "manual_operator_tick",
  "dry_run": true,
  "evaluated_count": 12,
  "recommended_count": 3,
  "approval_required_count": 2,
  "skipped_count": 7,
  "released_expired_count": 0,
  "duration_ms": 248
}
```

### 7.4 `assign.worker.heartbeat.recorded`

```json
{
  "heartbeat_id": "hb_uuid",
  "node_id": "x1-370",
  "worker_id": "paperclip-hermes-local",
  "assignment_id": "assign_task-123_abc123",
  "status": "running",
  "capabilities": ["docs", "code"],
  "service_status": [],
  "received_at": "2026-05-30T16:00:00Z"
}
```

### 7.5 `assign.control.changed`

```json
{
  "mode": "draining",
  "reason": "operator requested drain before interactive session",
  "updated_by": "operator",
  "updated_at": "2026-05-30T16:00:00Z"
}
```

## 8. Idempotency keys

Recommended formats:

| Event type | Idempotency key |
|---|---|
| `assign.assignment.recommended` | `assign.assignment.recommended:{task_id}:{decision_hash}` |
| `assign.assignment.skipped` | `assign.assignment.skipped:{task_id}:{reason_code}:{context_revision}` |
| `assign.assignment.reserved` | `assign.assignment.reserved:{assignment_id}` |
| `assign.assignment.released` | `assign.assignment.released:{assignment_id}:{reason_code}` |
| `assign.scheduler.tick.completed` | `assign.scheduler.tick.completed:{scheduler_run_id}` |
| `assign.worker.heartbeat.recorded` | `assign.worker.heartbeat.recorded:{heartbeat_id}` |
| `assign.control.changed` | `assign.control.changed:{updated_at_ts}:{mode}` |

## 9. Conservative fallback rules

When dependency data is missing:

| Missing dependency | Required behavior |
|---|---|
| AssistX unavailable | Do not evaluate new candidates unless caller supplied explicit candidate body in dry-run. Do not dispatch. |
| AssistX event sink unavailable | Enqueue local outbox only. Do not treat event as canonical. |
| Router unavailable | Mark `free_api` and hosted `router_model` lanes degraded/blocked; prefer safe local/Paperclip only. |
| Router quota unavailable | Do not select free API lane. |
| Router service/CLI unavailable | Treat related lanes as degraded; do not select direct workers. |
| SQLite unavailable | Health degraded; do not claim/reserve work because idempotency/outbox cannot be guaranteed. |

## 10. P0/P1 contract tests

The first implementation should include fixtures for:

1. non-sensitive docs task selects Paperclip or router planning lane;
2. local-only task blocks free API/cloud lanes;
3. sensitive voice-auth task blocks cloud and requires approval;
4. direct worker lane is always skipped while disabled;
5. router unavailable produces conservative fallback;
6. duplicate active local assignment blocks new recommendation;
7. same logical recommendation produces the same idempotency key;
8. dry-run scheduler never dispatches;
9. outbox enqueue stores no raw prompt/secrets;
10. cache deletion does not alter canonical AssistX/Neo4j state.
