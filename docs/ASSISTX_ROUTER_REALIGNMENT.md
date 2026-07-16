# auto-assign Realignment with auto-assist and auto-router

## 1. Purpose

This document realigns `auto-assign` against the current AssistX and auto-router architecture before the next implementation cycle.

`auto-assign` should become the assignment governor: the service that decides what work is eligible, where it should go, what lane is safest, what approval is required, and whether the worker holding the work is still alive.

It should not become another task database, model router, executor, or private memory store.

## 2. Current ecosystem roles

| System | Long-term role | Notes |
|---|---|---|
| `auto-assist` | Canonical task, policy, Sophia, event sink, graph authority | Owns Neo4j materialization and user-facing task/policy state. |
| Neo4j | True brain / durable graph | Canonical memory for tasks, decisions, leases, heartbeats, agent runs, provenance, and relationships. |
| `auto-router` | Model/provider/quota/service discovery routing authority | Owns OpenAI-compatible routing, quota, provider/model registry, service scan state, and route provenance. |
| `auto-assign` | Assignment governor, scheduler, lease, heartbeat, lane-selection service | Should evaluate work, produce assignment decisions, hold local outbox/cache only, and write `assign.*` events back to AssistX. |
| Paperclip / `hermes_local` | Current cutover execution lane | Remains the approved execution path until direct workers are separately promoted. |
| Future direct workers | Deferred execution lanes | Must require sandbox, approval, command allow-list, artifacts, leases, and write-back contracts. |

## 3. Important update from recent AssistX work

Recent AssistX additions introduced useful passive-agent primitives:

- passive heartbeat plans;
- advisory leases;
- passive claims;
- passive claim renewal/release/expiry;
- passive status;
- passive events;
- global passive control.

These features are useful, but their long-term conceptual home is mostly `auto-assign`, not `auto-assist`.

### Recommended interpretation

AssistX passive endpoints should be treated as a proving ground and compatibility bridge while `auto-assign` is bootstrapped.

`auto-assign` should eventually own:

- scheduler ticks;
- passive work selection;
- assignment scoring;
- worker heartbeat ingestion;
- passive/direct assignment leases;
- stale claim/lease release;
- global assignment control mode;
- assignment status summaries;
- assignment event outbox.

AssistX should continue to own:

- canonical task state;
- graph write-back/materialization;
- user/Sophia policy;
- approval state;
- event sink;
- Paperclip dispatch state while the cutover path is active.

## 4. Boundary decisions

### 4.1 auto-assign owns decisions, not canonical truth

`auto-assign` may produce:

- `assign.assignment.recommended`;
- `assign.assignment.skipped`;
- `assign.assignment.approval_required`;
- `assign.assignment.reserved`;
- `assign.assignment.released`;
- `assign.worker.heartbeat.recorded`;
- `assign.scheduler.tick.completed`.

But Neo4j through AssistX remains the canonical materialized state.

### 4.2 auto-assign owns assignment leases, not task lifecycle

`auto-assign` should track assignment leases locally only as a cache/outbox mirror. The canonical lease/decision facts must be emitted through AssistX and materialized in Neo4j.

A local SQLite lease row is valid only if it reconciles with AssistX/Neo4j state.

### 4.3 auto-router owns provider/model/quota routing

`auto-assign` can read router summaries:

- `/admin/context`;
- `/admin/quota`;
- `/admin/circuits`;
- `/admin/services`;
- `/admin/agent-clis`;
- `/admin/ops/summary`;
- optional `/admin/backlog/dry-run`.

It should not duplicate quota counters or decide concrete hosted model routing internally.

### 4.4 AssistX owns approval and graph policy

`auto-assign` may compute `approval_required=true`, but the authoritative approval state should come from AssistX/Neo4j.

If AssistX says approval is absent, `auto-assign` must not dispatch.

## 5. Passive-agent migration strategy

### Phase A — Compatibility bridge

Keep AssistX passive endpoints available:

- `/api/agents/heartbeat-plan`;
- `/api/agents/passive-claim`;
- `/api/agents/passive-claim/renew`;
- `/api/agents/passive-claim/release`;
- `/api/agents/passive-claims`;
- `/api/agents/passive-status`;
- `/api/agents/passive-control`;
- `/api/agents/passive-events`.

`auto-assign` can initially consume these endpoints as part of the AssistX client while building its own scheduler/scorer/outbox.

### Phase B — auto-assign becomes passive-work governor

Move ownership of the following concepts into `auto-assign`:

| Existing AssistX passive concept | Future auto-assign concept |
|---|---|
| `heartbeat-plan` | `POST /api/heartbeats` plus assignment recommendation state |
| passive claim | assignment lease / reservation |
| passive claim renewal | lease renewal / heartbeat extension |
| passive claim release | assignment release event |
| passive claim expiry | lease expiration scan |
| passive status | assignment dashboard summary |
| passive control | assignment control mode |
| passive events | `assign.*` events/outbox |

AssistX should then keep only the graph-materialized canonical view and event sink.

### Phase C — deprecate AssistX passive coordination endpoints

After `auto-assign` has equivalent behavior and event write-back is stable:

1. Mark AssistX passive endpoints as compatibility-only.
2. Point agents/operators to `auto-assign` endpoints.
3. Preserve AssistX graph/event materialization.
4. Remove or freeze AssistX passive logic only after no clients depend on it.

## 6. MVP implementation realignment

The next implementation cycle should not start with direct dispatch. It should start with a dry-run assignment governor.

### P0 updates

- Add package shell under `src/auto_assign`.
- Add `.env.example`, `pyproject.toml`, `Makefile`.
- Add FastAPI `GET /health`.
- Add settings for AssistX/router/cache/dispatch flags.
- Add SQLite cache/outbox DDL.
- Add Docker only after local dev works.

### P1 updates

- Implement AssistX client using current endpoints:
  - `/health`;
  - `/api/router/backlog-candidates`;
  - `/api/router/context-projection`;
  - `/api/events`;
  - optionally `/api/agents/passive-status` during bridge phase.
- Implement router client using current endpoints:
  - `/health`;
  - `/admin/context`;
  - `/admin/quota`;
  - `/admin/circuits`;
  - `/admin/services`;
  - `/admin/agent-clis`;
  - `/admin/ops/summary`.
- Implement deterministic assignment scorer.
- Implement `POST /api/assignments/evaluate` dry-run.
- Implement `POST /api/scheduler/tick` dry-run.
- Emit local outbox `assign.assignment.recommended` and `assign.assignment.skipped` events only.

### P2 updates

- Implement heartbeat ingestion.
- Implement assignment lease proposal/reservation.
- Implement stale lease release.
- Migrate passive claim semantics into assignment lease semantics.
- Keep dispatch disabled by default.

### P3 updates

- Add outbox dispatcher to AssistX `/api/events`.
- Confirm AssistX materializes `assign.*` events into Neo4j.
- Add reconciliation where Neo4j wins over local cache.

### P4 updates

- Add Paperclip lane adapter only after dry-run and lease flows are tested.
- Keep direct workers disabled.
- Add approval checks before any side-effecting work.

## 7. Concrete API mapping

| Need | Current bridge endpoint | Future auto-assign endpoint |
|---|---|---|
| Read task candidates | AssistX `/api/router/backlog-candidates` | `auto-assign` client reads AssistX; no public mirror needed initially |
| Evaluate one task | none / manual scorer | `POST /api/assignments/evaluate` |
| Run scheduler cycle | none | `POST /api/scheduler/tick` |
| Agent heartbeat | AssistX `/api/agents/heartbeat-plan` | `POST /api/heartbeats` |
| Passive claim | AssistX `/api/agents/passive-claim` | `AssignmentLease` via `assign.assignment.reserved` |
| Claim renewal | AssistX `/api/agents/passive-claim/renew` | heartbeat/lease renewal event |
| Claim release | AssistX `/api/agents/passive-claim/release` | `POST /api/assignments/{assignment_id}/release` |
| Passive control | AssistX `/api/agents/passive-control` | future `GET/POST /api/assignment-control` |
| Passive status | AssistX `/api/agents/passive-status` | future `/api/assignments/summary` |
| Event history | AssistX `/api/agents/passive-events` | `assign.*` outbox + Neo4j materialization |

## 8. Scoring alignment

The assignment scorer should prefer lanes in this order unless hard gates override:

1. `router_model` for current execution tasks.
2. `local_only` for sensitive/private/local tasks.
3. `router_model` for safe planning/review/drafting.
4. `free_api` only for non-sensitive backlog burn-down with safe quota posture.
5. `direct_worker` only after explicit enablement, sandboxing, approval, and artifact capture.
6. `blocked` if no safe route exists.

Hard gates must remain stronger than scoring:

- terminal task;
- duplicate active assignment;
- local-only/privacy mismatch;
- missing approval;
- lane disabled;
- stale heartbeat;
- missing capability;
- router circuit/quota block;
- direct workers disabled;
- secrets/voice-auth/enrollment data.

## 9. Recommended next coding prompt

```text
Implement the P0/P1 dry-run MVP for auto-assign as an assignment governor, not an executor.

Use the current HLD/LLD and the realignment doc. Build the FastAPI service shell, settings, health endpoint, SQLite cache/outbox, AssistX client, auto-router client, deterministic assignment scorer, assignment evaluate endpoint, and scheduler tick dry-run endpoint. Integrate with AssistX `/api/router/backlog-candidates`, `/api/router/context-projection`, and `/api/events`; integrate with auto-router `/admin/context`, `/admin/quota`, `/admin/circuits`, `/admin/services`, `/admin/agent-clis`, and `/admin/ops/summary`.

Do not implement dispatch, direct worker execution, repo mutation, shell execution, commits, pushes, or cloud routing bypasses. Keep SQLite as cache/outbox only. Emit idempotent `assign.*` events to the local outbox. Add tests for privacy gates, approval-required gates, duplicate active assignment prevention, router-unavailable conservative fallback, deterministic scoring, outbox idempotency, and Neo4j/AssistX state precedence.
```

## 10. Success criteria for the next iteration

- `GET /health` reports AssistX, router, and cache status.
- `POST /api/assignments/evaluate` returns selected/skipped lanes with reasons.
- `POST /api/scheduler/tick` dry-runs candidate batches.
- No dispatch occurs unless an explicit future flag is enabled.
- Local-only/private tasks never select cloud/free API lanes.
- Approval-required tasks never dispatch.
- Duplicate active assignments are blocked by AssistX/Neo4j state and local outbox idempotency.
- Local SQLite can be deleted and rebuilt without losing canonical assignment history.
- The docs continue to make clear that Neo4j is the brain, AssistX is canonical task/policy/event authority, auto-router owns model/quota routing, and auto-assign owns assignment decisions.
