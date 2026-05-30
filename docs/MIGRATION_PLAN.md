# Migration Plan: AssistX Passive Coordination to auto-assign

## 1. Purpose

This plan describes how to migrate the passive coordination primitives currently incubated in `auto-assist` into `auto-assign` without breaking the existing AssistX/Paperclip cutover path.

The goal is not to remove AssistX functionality immediately. The goal is to introduce `auto-assign` as the assignment governor while AssistX remains the canonical task, policy, event, and graph authority.

## 2. Starting point

Recent AssistX work added:

- passive heartbeat planning;
- advisory leases;
- passive claims;
- passive claim renewal/release/expiry;
- passive coordination status;
- passive global control;
- passive event history.

Those features proved the need for a scheduling and assignment coordination layer. They should now guide the `auto-assign` MVP, but the final ownership should move from AssistX route handlers into an assignment-focused service.

## 3. Target ownership

| Capability | Bridge owner now | Target owner | Canonical state |
|---|---|---|---|
| Task state | AssistX | AssistX | Neo4j via AssistX |
| Policy and approval state | AssistX | AssistX | Neo4j via AssistX |
| Passive heartbeat planning | AssistX | auto-assign | Neo4j via `assign.*` events |
| Passive claim/reservation | AssistX | auto-assign assignment lease | Neo4j via `assign.assignment.reserved` |
| Claim renewal | AssistX | auto-assign heartbeat/lease renewal | Neo4j via `assign.assignment.heartbeat` |
| Claim release/expiry | AssistX | auto-assign lease release | Neo4j via `assign.assignment.released` |
| Global passive control | AssistX | auto-assign assignment control | Neo4j via `assign.control.changed` |
| Passive events | AssistX | auto-assign outbox + AssistX event sink | Neo4j materialization |
| Model/provider/quota routing | auto-router | auto-router | auto-router state + AssistX provenance |
| Dispatch execution | Paperclip/AssistX | Paperclip initially, direct workers later | Neo4j via AssistX |

## 4. Migration phases

### Phase 0 — Documentation alignment

Status: in progress.

Deliverables:

- README/HLD/LLD define `auto-assign` as assignment governor.
- Realignment doc maps AssistX passive concepts to future assignment concepts.
- This migration plan defines sequence, risks, and compatibility boundaries.

Exit criteria:

- Stakeholders can tell which system owns each state transition.
- No implementation assumes SQLite is canonical.

### Phase 1 — Dry-run auto-assign service shell

Build the service without migrating clients yet.

Deliverables:

- FastAPI app under `src/auto_assign`.
- `GET /health` reports AssistX, router, and cache status.
- SQLite cache/outbox tables are created on startup.
- AssistX client reads:
  - `/health`;
  - `/api/router/backlog-candidates`;
  - `/api/router/context-projection`;
  - optionally `/api/agents/passive-status` for bridge visibility.
- Router client reads:
  - `/health`;
  - `/admin/context`;
  - `/admin/quota`;
  - `/admin/circuits`;
  - `/admin/services`;
  - `/admin/agent-clis`;
  - `/admin/ops/summary`.

Exit criteria:

- Service runs locally.
- `make test` passes.
- Cache deletion does not affect canonical state.

### Phase 2 — Dry-run assignment scoring

Build the decision engine without leases or dispatch.

Deliverables:

- `POST /api/assignments/evaluate` returns selected/skipped lane reasons.
- `POST /api/scheduler/tick` evaluates batches from AssistX backlog candidates.
- Local outbox stores idempotent events:
  - `assign.assignment.recommended`;
  - `assign.assignment.skipped`;
  - `assign.scheduler.tick.started`;
  - `assign.scheduler.tick.completed`.
- Router-unavailable fallback blocks/degrades cloud/free lanes and prefers safe local/Paperclip options only.

Exit criteria:

- Local-only/private tasks never select cloud/free API lanes.
- Approval-required tasks are never auto-dispatched.
- Duplicate active assignment is blocked by canonical AssistX/Neo4j state or local pending idempotency.

### Phase 3 — Lease and heartbeat parity

Move passive coordination semantics into auto-assign, but keep execution disabled.

Deliverables:

- `POST /api/heartbeats` ingests node/worker heartbeats.
- Assignment lease model replaces passive claim model.
- Lease renewal is represented by heartbeat/progress events.
- Lease release/expiry is represented by assignment release events.
- Assignment control mode mirrors passive global control semantics:
  - `enabled`;
  - `paused`;
  - `draining`;
  - `maintenance`.
- AssistX passive endpoints remain available as compatibility bridges.

Exit criteria:

- auto-assign can run a heartbeat + lease cycle in dry-run/reservation mode.
- Stale leases are released safely.
- AssistX receives idempotent `assign.*` events.

### Phase 4 — AssistX graph materialization

Make AssistX/Neo4j the durable view of auto-assign decisions.

Deliverables:

- AssistX event sink accepts and materializes `assign.*` events.
- Neo4j creates assignment graph nodes/relationships:
  - `AssignmentDecision`;
  - `AssignmentReason`;
  - `AssignmentLease`;
  - `WorkerHeartbeat`;
  - relationships to `Task`, `SwarmNode`, `RouterProvider`, `PaperclipAdapter`, `AgentRun`, and artifacts.
- Startup reconciliation checks pending local outbox against AssistX/Neo4j idempotency state.

Exit criteria:

- Local outbox can replay safely.
- Local cache can be deleted and rebuilt.
- Neo4j wins all conflict resolution.

### Phase 5 — Controlled Paperclip dispatch

Only after dry-run scoring, leases, and event materialization are stable.

Deliverables:

- Paperclip lane adapter.
- Dispatch remains disabled by default.
- Approval-required tasks block dispatch until AssistX/Neo4j approval is present.
- Duplicate-dispatch prevention checks AssistX/Neo4j terminal state and existing dispatch refs.

Exit criteria:

- Paperclip dispatch can be manually enabled for safe low-risk tasks.
- Direct worker lane remains disabled.

### Phase 6 — Deprecate AssistX passive coordination endpoints

Only after agents/operators use auto-assign endpoints.

Deliverables:

- AssistX passive endpoints are marked compatibility-only.
- auto-assign endpoints become the recommended integration path.
- AssistX keeps graph/event materialization.
- Existing passive graph nodes are migrated or mapped to assignment graph nodes.

Exit criteria:

- No active agent depends on AssistX passive endpoints for coordination.
- All new assignment facts are emitted as `assign.*` events.

## 5. Compatibility map

| AssistX bridge endpoint | Keep during phase | auto-assign replacement |
|---|---:|---|
| `POST /api/agents/heartbeat-plan` | 1-5 | `POST /api/heartbeats` + scheduler recommendation |
| `POST /api/agents/passive-claim` | 1-5 | `assign.assignment.reserved` / assignment lease |
| `POST /api/agents/passive-claim/renew` | 1-5 | `assign.assignment.heartbeat` / lease extension |
| `POST /api/agents/passive-claim/release` | 1-5 | `POST /api/assignments/{assignment_id}/release` |
| `GET /api/agents/passive-claims` | 1-5 | `GET /api/assignments?status=reserved,running` |
| `GET /api/agents/passive-status` | 1-5 | `GET /api/assignments/summary` |
| `GET/POST /api/agents/passive-control` | 1-5 | `GET/POST /api/assignment-control` |
| `GET /api/agents/passive-events` | 1-5 | `GET /api/outbox/events` plus Neo4j assignment graph |

## 6. Event mapping

| AssistX passive event | auto-assign event |
|---|---|
| `passive_claim.created` | `assign.assignment.reserved` |
| `passive_claim.renewed` | `assign.assignment.heartbeat` |
| `passive_claim.released` | `assign.assignment.released` |
| `passive_claim.expired` | `assign.assignment.released` with `reason_code=lease_expired` |
| `passive_claim.rollback` | `assign.assignment.released` with `reason_code=validation_failed` |
| `passive_control.changed` | `assign.control.changed` |

## 7. Data migration notes

No immediate destructive migration is required.

When ready, create a Neo4j migration/materialization pass that:

1. Finds existing `PassiveAgentEvent` nodes.
2. Converts or links them to `AssignmentDecision`/`AssignmentLease` history where a task ID is present.
3. Preserves original passive event nodes as provenance.
4. Marks the translated assignment graph with `source='assistx_passive_bridge'`.

Do not delete passive event nodes until the assignment graph has been validated.

## 8. Cutover risks

| Risk | Mitigation |
|---|---|
| Two systems try to claim the same work | Keep AssistX passive endpoints as bridge-only; introduce idempotency keys and lease checks before auto-assign dispatch. |
| SQLite becomes hidden source of truth | Treat every local row as cache/outbox; reconcile with AssistX/Neo4j on startup and before dispatch. |
| Agents keep using old endpoints | Keep compatibility phase long enough; add docs and dashboard warnings. |
| Router quota is duplicated | Read router summaries only; never reimplement provider quota reservation. |
| Direct workers slip in too early | Keep `AUTO_ASSIGN_DIRECT_WORKERS_ENABLED=false`; hard tests must prove disabled direct workers cannot run. |
| Sensitive tasks route to cloud | Hard privacy gate before router dry-run or free API lane scoring. |

## 9. Immediate next implementation target

Implement P0/P1 only:

- service shell;
- health;
- settings;
- SQLite cache/outbox;
- AssistX client;
- router client;
- deterministic scorer;
- assignment evaluate dry-run;
- scheduler tick dry-run;
- outbox enqueue only.

Explicitly do not implement:

- dispatch;
- direct workers;
- repo mutation;
- shell execution;
- cloud bypass;
- canonical task storage.
