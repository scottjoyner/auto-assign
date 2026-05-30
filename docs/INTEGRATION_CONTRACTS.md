# Integration Contracts

`auto-assign` sits between AssistX/Neo4j and auto-router. It should be conservative whenever either dependency is missing, malformed, or stale.

## Canonical ownership

| Domain | Owner | `auto-assign` behavior |
|---|---|---|
| Task state | AssistX + Neo4j | Reads candidates and canonical status; never becomes the source of truth. |
| Assignment graph history | AssistX + Neo4j | Emits idempotent `assign.*` events for AssistX to materialize. |
| Router/model/provider health | auto-router | Reads context/quota/circuit snapshots; blocks hosted/router lanes if snapshots are unavailable. |
| Local cache/outbox | auto-assign | Stores retry buffers, local mirrors, inbound event processing state, and operator views only. |

## AssistX endpoints consumed by `auto-assign`

| Method | Endpoint | Required | Purpose | Degraded behavior |
|---|---|---:|---|---|
| `GET` | `/health` | Yes | Health check and Neo4j brain reachability signal. | `/health` reports degraded. |
| `GET` | `/api/router/backlog-candidates` | No | Scheduler backlog candidate intake. | Scheduler evaluates zero backlog candidates unless explicit task IDs are provided. |
| `GET` | `/api/tasks/{task_id}` | No | Task lookup for direct evaluation. | Falls back to a minimal candidate built from the requested task ID. |
| `GET` | `/api/router/tasks/{task_id}` | No | Compatibility task lookup path. | Falls back to a minimal candidate built from the requested task ID. |
| `GET` | `/api/events/status?idempotency_key=...` | No | Idempotency and canonical status reconciliation. | Treats event as unknown; local decision remains cache-only until outbox dispatch/reconcile succeeds. |
| `POST` | `/api/events` | Yes for write-back | Materialize `auto-assign` authored outbound events in AssistX/Neo4j. | Event stays pending/failed in local outbox. |

### AssistX candidate normalization

`auto-assign` accepts multiple task candidate shapes and normalizes them to `AssignmentCandidate`.

Supported candidate IDs:

- `task_id`
- `id`
- `uuid`
- `title` as last fallback

Supported collection keys for backlog payloads:

- `tasks`
- `candidates`
- `items`
- `results`
- `backlog`
- bare list payloads

Privacy and cloud gating fields:

| Field | Accepted shapes | Effect |
|---|---|---|
| `privacy_labels` | list, tuple, set, comma-separated string, semicolon-separated string | Labels are normalized to lowercase. |
| `privacy` / `privacy_label` | string | Added to `privacy_labels`. |
| `metadata.privacy` | string | Added to `privacy_labels`. |
| `local_only` | bool or boolean-like string | Forces `allow_cloud = false`. |
| `sensitive` | bool or boolean-like string | Used by scoring to prefer local lanes. |
| `allow_cloud` | bool or boolean-like string | Must be true and not local-only for hosted/router lanes. |

Boolean strings are parsed intentionally. `"false"`, `"no"`, `"0"`, and `"off"` are false, not truthy Python strings.

## auto-router endpoints consumed by `auto-assign`

| Method | Endpoint | Required | Purpose | Degraded behavior |
|---|---|---:|---|---|
| `GET` | `/health` | Yes | Router reachability. | `/health` reports router degraded. |
| `GET` | `/admin/context` | Yes for router lanes | Provider, service, node, and context revision snapshot. | Router snapshot marked unreachable; hosted/router lanes are blocked. |
| `GET` | `/admin/quota` | No | Quota and preserve-mode state. | Empty quota object. |
| `GET` | `/admin/circuits` | No | Circuit breaker state. | Empty circuits object. |
| `GET` | `/admin/agent-clis` | No | Available local agent CLI adapters. | Empty agent CLI list. |
| `GET` | `/admin/ops/summary` | No | Router operator status summary. | Empty ops summary. |

### Router context normalization

`auto-assign` accepts context payloads as:

- direct object;
- nested `context`;
- nested `snapshot`;
- nested `router_context`;
- nested `data`.

Recognized keys:

| RouterSnapshot field | Accepted keys |
|---|---|
| `context_revision` | `revision`, `context_revision`, `version`, `metadata.revision`, `metadata.context_revision` |
| `nodes` | `nodes`, `workers`, `worker_nodes`, `hosts` |
| `providers` | `providers`, `models`, `model_providers` |
| `services` | `services`, `endpoints`, `routes` |
| `agent_clis` | `agents`, `agent_clis`, `clis`, `items`, `results` |

Malformed context payloads mark the router snapshot as unreachable. This is intentional: if `auto-assign` cannot prove router state is valid, it must not send work to hosted/router lanes.

## Scoring safety expectations

The scorer must:

- block hosted/free API lanes for local-only, private, secret, voice-auth, enrollment, or cloud-disallowed work;
- block router/free API lanes when router context is unreachable;
- preserve quota when auto-router reports preserve mode;
- block duplicate work when AssistX/Neo4j reports active assignment state;
- block terminal work when AssistX/Neo4j reports terminal task state;
- emit idempotent assignment decision events that AssistX can safely upsert.

## Event dispatch expectations

Outbound event dispatch is opt-in and dry-run by default.

```text
POST /api/outbox/dispatch?dry_run=true
  -> shows pending events without POSTing to AssistX

POST /api/outbox/dispatch?dry_run=false
  -> POSTs pending outbox events to AssistX /api/events
```

`409 Conflict` from AssistX is treated as delivered because it usually means the event idempotency key already exists canonically.

## Local cache failure model

SQLite loss is survivable but not free:

- pending outbox events may need to be regenerated by scheduler ticks or inbound replay;
- inbound event mirrors are lost locally but can be resent by the source system if needed;
- canonical history remains in Neo4j via AssistX;
- cache rebuild should never invent canonical state.
