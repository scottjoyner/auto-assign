# Assignment Control

## 1. Purpose

Assignment control is the operator-level control plane for `auto-assign`.

It lets the assignment governor be placed into one of four modes before real lease or dispatch behavior is enabled:

- `enabled`
- `paused`
- `draining`
- `maintenance`

The control state is stored locally as a SQLite cache mirror and emits an `assign.control.changed` event into the local outbox for AssistX/Neo4j materialization.

Neo4j via AssistX remains the canonical durable source once the event is delivered/materialized.

## 2. Endpoints

### Read current control state

```text
GET /api/assignment-control
```

Example response:

```json
{
  "mode": "enabled",
  "assignment_allowed": true,
  "new_assignments_allowed": true,
  "scheduler_ticks_allowed": true,
  "lease_renewals_allowed": true,
  "dispatch_allowed": true,
  "recommended_scheduler_state": "active",
  "reason": "default enabled; no local assignment_control row exists yet",
  "updated_by": null,
  "updated_at": null,
  "metadata": {},
  "cache_role": "assignment_control_cache",
  "canonical_source": "neo4j_via_assistx"
}
```

### Update control state

```text
POST /api/assignment-control
```

Request:

```json
{
  "mode": "draining",
  "reason": "operator drain before interactive work",
  "updated_by": "operator",
  "metadata": {
    "source": "dashboard"
  },
  "dry_run": true
}
```

Response includes the updated state and the outbox event reference:

```json
{
  "mode": "draining",
  "new_assignments_allowed": false,
  "lease_renewals_allowed": true,
  "event_id": "evt_...",
  "idempotency_key": "assign.control.changed:...:draining",
  "cache_role": "assignment_control_cache_and_outbox",
  "canonical_source": "neo4j_via_assistx"
}
```

## 3. Modes

| Mode | New assignment decisions | Scheduler ticks | Lease renewals | Dispatch | Recommended state |
|---|---:|---:|---:|---:|---|
| `enabled` | yes | yes | yes | yes, if dispatch flag allows | `active` |
| `paused` | no | no | no | no | `paused` |
| `draining` | no | no | yes | no by policy until dispatch phase | `draining` |
| `maintenance` | no | no | no | no | `maintenance` |

During the current MVP, dispatch is still separately disabled by configuration. Control mode does not override `AUTO_ASSIGN_DISPATCH_ENABLED=false` or `AUTO_ASSIGN_DIRECT_WORKERS_ENABLED=false`.

## 4. Enforcement behavior

### Assignment evaluation

`POST /api/assignments/evaluate` now checks assignment control before creating a normal recommendation.

When mode is `paused`, `draining`, or `maintenance`, evaluation returns a blocked decision and emits an `assign.assignment.skipped` event:

```json
{
  "status": "blocked",
  "selected_lane": "blocked",
  "canonical_status": "paused",
  "reasons": [
    "assignment control mode is paused; new assignment decisions are paused"
  ]
}
```

This is intentionally visible in local assignment history and outbox state so AssistX/Neo4j can later materialize why the task was skipped.

### Scheduler ticks

`POST /api/scheduler/tick` checks assignment control before reading AssistX backlog candidates.

When mode is `paused`, `draining`, or `maintenance`, scheduler tick returns a no-op response:

```json
{
  "evaluated": 0,
  "recommended": 0,
  "approval_required": 0,
  "skipped": 0,
  "decisions": []
}
```

A scheduler run is still recorded locally with an `error_summary` such as:

```text
assignment control mode draining blocks scheduler ticks
```

This gives operators a visible audit trail without accidentally pulling new work.

## 5. Assignment summary integration

`GET /api/assignments/summary` includes the control state:

```json
{
  "control": {
    "mode": "paused",
    "new_assignments_allowed": false,
    "scheduler_ticks_allowed": false
  },
  "safety": {
    "control_mode": "paused",
    "assignment_allowed": false,
    "scheduler_ticks_allowed": false
  },
  "recommendations": [
    {
      "action": "keep_assignment_governor_paused"
    }
  ]
}
```

Recommendation behavior:

| Control mode | Summary recommendation |
|---|---|
| `paused` | `keep_assignment_governor_paused` |
| `maintenance` | `keep_assignment_governor_paused` |
| `draining` | `drain_assignment_governor` |
| `enabled` | normal outbox/heartbeat/dry-run recommendations |

## 6. Health and ops integration

`GET /health` includes control under the scheduler block.

`GET /api/ops/summary` includes the current control object.

## 7. Operator commands

Pause new assignment decisions:

```bash
curl -X POST http://localhost:8090/api/assignment-control \
  -H 'Content-Type: application/json' \
  -d '{
    "mode": "paused",
    "reason": "interactive operator work in progress",
    "updated_by": "operator"
  }' | jq
```

Drain current work:

```bash
curl -X POST http://localhost:8090/api/assignment-control \
  -H 'Content-Type: application/json' \
  -d '{
    "mode": "draining",
    "reason": "finish safe checkpoints and avoid new recommendations",
    "updated_by": "operator"
  }' | jq
```

Return to normal dry-run operation:

```bash
curl -X POST http://localhost:8090/api/assignment-control \
  -H 'Content-Type: application/json' \
  -d '{
    "mode": "enabled",
    "reason": "operator resumed assignment governor",
    "updated_by": "operator"
  }' | jq
```

## 8. Current boundary

Implemented now:

- local assignment control store;
- `GET /api/assignment-control`;
- `POST /api/assignment-control`;
- `assign.control.changed` outbox event;
- health integration;
- ops summary integration;
- assignment summary integration;
- control-aware recommendations;
- `POST /api/assignments/evaluate` blocked decisions when new assignments are not allowed;
- `POST /api/scheduler/tick` no-op when scheduler ticks are blocked;
- tests for defaults, updates, paused/draining summary, blocked evaluation, blocked scheduler ticks, and enabled scheduler ticks.

Still future work:

- future lease reserve/renew endpoints must honor `new_assignments_allowed` and `lease_renewals_allowed`;
- eventual dispatch must honor both assignment control and dispatch/direct-worker feature flags;
- AssistX/Neo4j should materialize `assign.control.changed` and blocked `assign.assignment.skipped` events as canonical graph facts.
