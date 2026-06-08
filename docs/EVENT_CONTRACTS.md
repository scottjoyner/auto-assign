# Event Contracts

`auto-assign` uses event envelopes to keep assignment decisions, inbound triggers, heartbeat state, and lifecycle actions idempotent across service restarts and AssistX/Neo4j outages.

Neo4j via AssistX remains canonical. Events cached inside `auto-assign` are either:

- inbound mirrors received from AssistX/router/other control-plane systems;
- outbound outbox events authored by `auto-assign` and intended for AssistX/Neo4j materialization;
- local processing markers that prevent replaying the same inbound trigger repeatedly.

The implementation source of truth for event names is `src/auto_assign/events.py`.

## Canonical cross-repo lifecycle events (2026-06-08)

These are the canonical assignment lifecycle event types used across the auto-* ecosystem:

| Event type | When emitted | Source |
|---|---|---|
| `assignment.requested` | AssistX requests an assignment for a routed task | auto-assist |
| `assignment.recommended` | auto-assign recommends a worker/lane for a task | auto-assign |
| `assignment.claimed` | A worker claims an assignment, starting a lease | auto-assign |
| `assignment.heartbeat` | A worker sends a heartbeat, renewing its lease | auto-assign |
| `assignment.completed` | A worker completes an assignment | auto-assign |
| `assignment.failed` | A worker reports failure on an assignment | auto-assign |
| `assignment.expired` | An assignment lease expires without heartbeat renewal | auto-assign |
| `assignment.released` | An assignment is released (operator or lease expiry) | auto-assign |

## Envelope

All internal events use `EventEnvelope`:

```json
{
  "event_id": "evt_<uuid>",
  "event_type": "assign.assignment.recommended",
  "source_service": "auto-assign",
  "occurred_at": "2026-05-30T00:00:00Z",
  "idempotency_key": "assign.assignment.decision:ASS-123:ready:unknown",
  "schema_version": "assign.v1",
  "subject": "ASS-123",
  "payload": {},
  "privacy": []
}
```

## Authored outbound events

These are queued in `outbox_events` and can be dispatched to AssistX.

| Event type | When emitted | Subject | Idempotency key shape |
|---|---|---|---|
| `assign.assignment.recommended` | A task is eligible for a lane. | `task_id` | `assign.assignment.decision:{task_id}:{candidate_status}:{canonical_status}` |
| `assign.assignment.approval_required` | A task is high-risk or explicitly requires operator approval. | `task_id` | `assign.assignment.decision:{task_id}:{candidate_status}:{canonical_status}` |
| `assign.assignment.skipped` | No lane is eligible or canonical graph state blocks assignment. | `task_id` | `assign.assignment.decision:{task_id}:{candidate_status}:{canonical_status}` |
| `assign.assignment.approved` | Operator approves an approval-gated assignment. | `task_id` | `assign.assignment.approved:{assignment_id}:{approved_by}:{approval_reason}` |
| `assign.assignment.released` | Operator releases an assignment mirror row. | `task_id` | `assign.assignment.released:{assignment_id}:{reason}:{retryable}` |
| `assign.worker.heartbeat.recorded` | A node/worker heartbeat is received. | `assignment_id`, `worker_id`, or `node_id` | `assign.worker.heartbeat.recorded:{node_id}:{worker_id}:{assignment_id}:{heartbeat_id}` |

## Consumed inbound events

These are stored in `inbound_events` and do not automatically enter the outbound outbox.

| Event type | Processor action |
|---|---|
| `task.candidate.created` | Evaluate the referenced task and queue an assignment decision event. |
| `router.quota_snapshot.recorded` | Run an explicit scheduler tick using the latest router context. |
| `router.service_snapshot.recorded` | Run an explicit scheduler tick using the latest router context. |

Unknown event types are mirrored and then marked as ignored when processed.

## Processing-state rules

Inbound event processing is tracked by `inbound_event_processing.idempotency_key`.

Default behavior:

```text
POST /api/events/process
  -> skips events already present in inbound_event_processing
```

Explicit replay:

```text
POST /api/events/process?include_processed=true
  -> reprocesses matching inbound events and refreshes their processing result
```

## SQLite role

SQLite is not canonical. It is only used for:

- `inbound_events`: received event mirror;
- `inbound_event_processing`: replay guard and processing history;
- `outbox_events`: outbound retry/replay buffer;
- `assignments`: local assignment mirror;
- `heartbeats`: local heartbeat mirror;
- `scheduler_runs`: local scheduler summaries.

Deleting SQLite loses local replay buffers and mirrors, but does not define canonical graph history. AssistX/Neo4j is the durable brain.
