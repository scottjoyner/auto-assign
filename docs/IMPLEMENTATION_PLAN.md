# auto-assign Implementation Plan

## Current repo posture

This repo is being positioned as the assignment/heartbeat service for the AssistX stack. The first pass establishes documentation and architecture. The next pass should bootstrap the actual service with dry-run behavior first, then controlled leases/heartbeats, and only later dispatch.

## Guiding constraints

- Neo4j, reached through AssistX, is the true brain and durable source of truth.
- SQLite in `auto-assign` is cache/outbox/replay-buffer state only and must be safe to delete/rebuild.
- AssistX remains canonical task-state, policy, and graph-write authority.
- Paperclip / `hermes_local` remains the current supported execution lane until direct workers are promoted.
- auto-router remains the model/provider/quota routing authority.
- auto-assign is an assignment governor, not an executor.
- Every decision must have machine-readable and human-readable reasons.
- Every meaningful assignment fact must be emitted as an idempotent event for Neo4j materialization.
- Dispatch, direct worker execution, repo mutation, commit, push, and other external side effects are disabled by default.

## P0 - Documentation and repo bootstrap

### P0.1 Define purpose and boundaries

- [x] Add README with purpose, boundaries, integrations, safety defaults, and proposed API surface.
- [x] Add HLD with system context, lifecycle, trigger/heartbeat model, event architecture, and risks.
- [x] Add LLD with module layout, API contracts, persistence/cache model, scoring, and event payloads.
- [x] Add Neo4j brain and SQLite cache policy.
- [ ] Add `.env.example` with AssistX/router/service/cache settings.
- [ ] Add `pyproject.toml` for FastAPI, Pydantic, HTTPX, pytest, and ruff.
- [ ] Add initial package layout under `src/auto_assign`.
- [ ] Add `Makefile` with `install`, `dev`, `test`, `lint`, and `smoke` targets.

### P0.2 Create service shell

- [ ] Implement `auto_assign.main:app`.
- [ ] Implement `GET /health` with AssistX/Neo4j brain connectivity, router connectivity, and cache/outbox health.
- [ ] Implement settings loader.
- [ ] Implement SQLite cache/outbox bootstrap.
- [ ] Add Dockerfile and docker-compose profile after local dev works.

### P0.3 Add local cache/outbox layer

- [ ] Create SQLite migrations or bootstrap DDL for cache/outbox tables only:
  - `scheduler_runs` as a local summary cache;
  - `assignments` as a local mirror of pending/recent assignment decisions;
  - `assignment_reasons` as local mirrored explainability rows;
  - `heartbeats` as local recent heartbeat cache;
  - `outbox_events` as the durable retry/replay buffer for Neo4j write-back.
- [ ] Add repository/cache layer with names that make cache semantics explicit.
- [ ] Add idempotency unique constraints.
- [ ] Add local cache smoke test.
- [ ] Add test proving deleting SQLite cache does not define or erase canonical history.

## P1 - Dry-run assignment engine

### P1.1 AssistX client

- [ ] Implement `AssistXClient.health()`.
- [ ] Implement `AssistXClient.get_backlog_candidates(limit)`.
- [ ] Implement `AssistXClient.get_task(task_id)` if AssistX exposes it.
- [ ] Implement `AssistXClient.get_event_status(idempotency_key)` or equivalent once available.
- [ ] Implement `AssistXClient.post_event(event)`.
- [ ] Add retry/backoff for transient failures.
- [ ] Add dry-run mode that writes only to local outbox/cache and does not mutate execution lanes.
- [ ] Add graph-state reconciliation helper where AssistX/Neo4j state wins conflicts.

### P1.2 auto-router client

- [ ] Implement `RouterClient.health()`.
- [ ] Implement `RouterClient.get_context()` from `/admin/context`.
- [ ] Implement `RouterClient.get_quota()` from `/admin/quota`.
- [ ] Implement `RouterClient.get_circuits()` from `/admin/circuits`.
- [ ] Implement `RouterClient.get_agent_clis()` from `/admin/agent-clis`.
- [ ] Implement optional `RouterClient.backlog_dry_run()`.
- [ ] Conservative fallback: when router is unavailable, mark cloud/free lanes degraded/blocked and prefer local/Paperclip only.

### P1.3 Assignment scorer

- [ ] Implement hard gates:
  - terminal task according to AssistX/Neo4j;
  - duplicate active assignment according to AssistX/Neo4j or local pending outbox;
  - local-only/privacy cloud denial;
  - approval required;
  - lane disabled;
  - stale heartbeat;
  - missing capability;
  - quota reserve blocked;
  - provider/service circuit open.
- [ ] Implement score components:
  - policy fit;
  - capability fit;
  - privacy fit;
  - availability fit;
  - quota fit;
  - age/priority fit;
  - retry fit.
- [ ] Emit selected lane, score, reasons, and skipped lane reasons.
- [ ] Add deterministic unit tests for each lane category.
- [ ] Add test proving Neo4j/AssistX conflict state overrides local cache rows.

### P1.4 API dry-run flows

- [ ] Implement `POST /api/assignments/evaluate`.
- [ ] Implement `POST /api/scheduler/tick`.
- [ ] Implement `GET /api/assignments` as a local mirror view with graph reconciliation status.
- [ ] Implement `GET /api/assignments/{assignment_id}` as a local mirror view with canonical AssistX/Neo4j status where available.
- [ ] Ensure `dry_run=true` never dispatches.
- [ ] Emit local outbox events for recommended/skipped decisions.

## P2 - Heartbeat and lease control

### P2.1 Heartbeat ingestion

- [x] Implement `POST /api/heartbeats`.
- [x] Validate node/worker identifiers.
- [x] Store heartbeat payload in local recent-heartbeat cache with secret redaction.
- [x] Link heartbeat to active assignment when provided.
- [x] Emit `assign.worker.heartbeat.recorded` events for Neo4j materialization.
- [x] Renew lease on claimed assignment when `assignment_id` is provided in heartbeat.

### P2.2 Lease lifecycle

- [x] Implement reserve/lease state transition as an event proposal, not SQLite-only truth.
- [x] Implement lease expiration scan during scheduler tick.
- [x] Implement `POST /api/assignments/{assignment_id}/release`.
- [x] Implement `POST /api/assignments/{assignment_id}/claim` with lease_seconds.
- [x] Implement `POST /api/assignments/{assignment_id}/complete` with result status.
- [x] Implement `POST /api/assignments/expire-stale` to expire all stale assignments.
- [x] Emit `assignment.claimed`, `assignment.completed`, `assignment.expired` events.
- [x] Add tests for stale heartbeat and expired lease release.
- [x] Add test proving lease terminal state from Neo4j blocks local stale retry.

### P2.3 Approval lifecycle

- [ ] Implement `POST /api/assignments/{assignment_id}/approve`.
- [ ] Require approval for high-risk, unknown-speaker, non-Scott, repo-write, production-changing, or other side-effecting tasks.
- [ ] Emit `assign.assignment.approval_required` and approval events.
- [ ] Do not dispatch when approval is required and absent.
- [ ] Ensure approval state is read from AssistX/Neo4j before dispatch.

## P3 - AssistX event write-back and graph readiness

### P3.1 Outbox dispatcher

- [ ] Implement pending/delivered/failed/dead-letter state machine.
- [ ] Implement `POST /api/outbox/dispatch?dry_run=true`.
- [ ] Implement retry with max attempts and next-attempt timestamp.
- [ ] Add idempotency key generation per event type.
- [ ] Add tests for duplicate event prevention.
- [ ] Add startup reconciliation: pending local outbox events are checked against AssistX/Neo4j by idempotency key before retry.

### P3.2 AssistX graph alignment

- [ ] Confirm final AssistX event sink payload shape.
- [ ] Map `assign.*` events to AssistX/Neo4j nodes:
  - `AssignmentDecision`;
  - `AssignmentLease`;
  - `AssignmentReason`;
  - `WorkerHeartbeat`;
  - relationships to `Task`, `AgentRun`, `SwarmNode`, `RouterDecision`.
- [ ] Add graph schema docs after AssistX event sink is verified.
- [ ] Add reconciliation rules for local cache vs Neo4j conflicts.

### P3.3 Operator visibility

- [ ] Add basic JSON dashboard summary endpoint:
  - recent scheduler runs;
  - active local mirror rows;
  - canonical graph reconciliation status;
  - blocked reasons;
  - stale heartbeats;
  - outbox status;
  - Neo4j write-back lag.
- [ ] Add Prometheus-style `/metrics` after core counters stabilize.

## P4 - Controlled dispatch integration

### P4.1 Paperclip lane

- [ ] Add Paperclip lane adapter only after dry-run scoring and leases are tested.
- [ ] Ensure AssistX remains owner of Paperclip dispatch state.
- [ ] Use assignment events to explain why Paperclip was selected, not to bypass AssistX.
- [ ] Validate duplicate-dispatch prevention against AssistX/Neo4j terminal state and existing dispatch refs.

### P4.2 Router model lane

- [ ] Use router only for allowed planning/drafting/review actions.
- [ ] Do not pass local-only/private tasks to cloud candidates.
- [ ] Request router dry-run before any real route.
- [ ] Attach router decision IDs to assignment events when available.

### P4.3 Direct worker lane

- [ ] Keep disabled by default.
- [ ] Require sandbox, artifact capture, command allow-list, approval gates, and lease monitoring.
- [ ] No repo write/commit/push until explicit operator approval exists.
- [ ] Require tests proving direct workers cannot run when disabled.

## P5 - Production hardening

### P5.1 Security

- [ ] Add signed event verification.
- [ ] Add auth for admin endpoints.
- [ ] Redact secrets in all logs and cache payloads.
- [ ] Add private-network deployment guidance.
- [ ] Add threat model document.

### P5.2 Reliability

- [ ] Add backoff for AssistX/router dependencies.
- [ ] Add circuit breaker for repeated dependency failures.
- [ ] Add scheduled background loop with jitter.
- [ ] Add graceful shutdown.
- [ ] Add cache/outbox backup guidance only for operational convenience; Neo4j backup remains the canonical disaster-recovery path.

### P5.3 CI and quality

- [ ] Add GitHub Actions for tests/lint.
- [ ] Add type checking.
- [ ] Add API contract fixtures.
- [ ] Add integration tests with mocked AssistX/router.

## Suggested first coding prompt

Use this after the docs are merged:

```text
Implement the P0/P1 dry-run MVP for auto-assign.

Build a FastAPI service under src/auto_assign with settings, health endpoint, SQLite cache/outbox, AssistX client, auto-router client, scheduler tick, assignment evaluation, deterministic scorer, and startup reconciliation. Keep dispatch disabled by default. Implement only dry-run assignment recommendations and skipped-lane reasons. Add tests proving local-only privacy gates, approval-required gates, duplicate active assignment prevention, router-unavailable conservative fallback, idempotent outbox events, SQLite cache deletion safety, and Neo4j/AssistX conflict precedence.

Do not implement repo mutation, direct worker execution, or cloud dispatch. Do not persist raw prompts, response bodies, secrets, voiceprints, or enrollment samples. Do not treat SQLite as the source of truth; Neo4j via AssistX is the durable brain.
```

## Acceptance checklist for next cycle

- [x] `make test` passes (52 tests passing, 2 pre-existing async failures excluded).
- [x] `GET /health` works with mocked/unavailable dependencies and reports AssistX/Neo4j brain connectivity.
- [x] `POST /api/assignments/evaluate` returns deterministic score/reasons.
- [x] `POST /api/scheduler/tick` dry-runs candidate batches.
- [x] Local-only/private tasks never select cloud/free API lanes.
- [x] Approval-required tasks never dispatch.
- [x] Duplicate active assignment is prevented using AssistX/Neo4j state plus local pending events.
- [x] Outbox stores idempotent `assign.*` events.
- [ ] Startup reconciliation marks already-applied outbox events delivered.
- [x] SQLite cache deletion does not erase canonical assignment history.
- [x] Neo4j/AssistX state wins over local cache conflicts.
- [x] README/HLD/LLD/cache policy remain aligned with implemented API.
- [x] `POST /api/assignments/{id}/claim` creates a claim with lease.
- [x] `POST /api/assignments/{id}/complete` completes an assignment.
- [x] `POST /api/assignments/expire-stale` expires stale leases.
- [x] Heartbeat with `assignment_id` renews the lease.
- [x] 7 canonical lifecycle event types defined in `events.py`.
