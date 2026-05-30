# auto-assign Implementation Plan

## Current repo posture

This repo is being positioned as the assignment/heartbeat service for the AssistX stack. The first pass establishes documentation and architecture. The next pass should bootstrap the actual service with dry-run behavior first, then controlled leases/heartbeats, and only later dispatch.

## Guiding constraints

- AssistX remains canonical task-state authority.
- Paperclip / `hermes_local` remains the current supported execution lane until direct workers are promoted.
- auto-router remains the model/provider/quota routing authority.
- auto-assign is an assignment governor, not an executor.
- Every decision must have machine-readable and human-readable reasons.
- Every write-back event must be idempotent.
- Dispatch, direct worker execution, repo mutation, commit, push, and external side effects are disabled by default.

## P0 - Documentation and repo bootstrap

### P0.1 Define purpose and boundaries

- [x] Add README with purpose, boundaries, integrations, safety defaults, and proposed API surface.
- [x] Add HLD with system context, lifecycle, trigger/heartbeat model, event architecture, and risks.
- [x] Add LLD with module layout, API contracts, persistence, scoring, and event payloads.
- [ ] Add `.env.example` with AssistX/router/service/store settings.
- [ ] Add `pyproject.toml` for FastAPI, Pydantic, HTTPX, pytest, and ruff.
- [ ] Add initial package layout under `src/auto_assign`.
- [ ] Add `Makefile` with `install`, `dev`, `test`, `lint`, and `smoke` targets.

### P0.2 Create service shell

- [ ] Implement `auto_assign.main:app`.
- [ ] Implement `GET /health`.
- [ ] Implement settings loader.
- [ ] Implement SQLite connection/bootstrap.
- [ ] Add Dockerfile and docker-compose profile after local dev works.

### P0.3 Add local persistence

- [ ] Create SQLite migrations or bootstrap DDL for:
  - `scheduler_runs`;
  - `assignments`;
  - `assignment_reasons`;
  - `heartbeats`;
  - `outbox_events`.
- [ ] Add repository/store layer.
- [ ] Add idempotency unique constraints.
- [ ] Add local DB smoke test.

## P1 - Dry-run assignment engine

### P1.1 AssistX client

- [ ] Implement `AssistXClient.health()`.
- [ ] Implement `AssistXClient.get_backlog_candidates(limit)`.
- [ ] Implement `AssistXClient.get_task(task_id)` if AssistX exposes it.
- [ ] Implement `AssistXClient.post_event(event)`.
- [ ] Add retry/backoff for transient failures.
- [ ] Add dry-run mode that writes only to local outbox.

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
  - terminal task;
  - duplicate active assignment;
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

### P1.4 API dry-run flows

- [ ] Implement `POST /api/assignments/evaluate`.
- [ ] Implement `POST /api/scheduler/tick`.
- [ ] Implement `GET /api/assignments`.
- [ ] Implement `GET /api/assignments/{assignment_id}`.
- [ ] Ensure `dry_run=true` never dispatches.
- [ ] Emit local outbox events for recommended/skipped decisions.

## P2 - Heartbeat and lease control

### P2.1 Heartbeat ingestion

- [ ] Implement `POST /api/heartbeats`.
- [ ] Validate node/worker identifiers.
- [ ] Store heartbeat payload with secret redaction.
- [ ] Link heartbeat to active assignment when provided.
- [ ] Emit `assign.worker.heartbeat.recorded` events.

### P2.2 Lease lifecycle

- [ ] Implement reserve/lease state transition.
- [ ] Implement lease expiration scan during scheduler tick.
- [ ] Implement `POST /api/assignments/{assignment_id}/release`.
- [ ] Emit `assign.assignment.released` with retryable/terminal classification.
- [ ] Add tests for stale heartbeat and expired lease release.

### P2.3 Approval lifecycle

- [ ] Implement `POST /api/assignments/{assignment_id}/approve`.
- [ ] Require approval for high-risk, unknown-speaker, non-Scott, repo-write, production, financial, legal, or external side-effect tasks.
- [ ] Emit `assign.assignment.approval_required` and approval events.
- [ ] Do not dispatch when approval is required and absent.

## P3 - AssistX event write-back and dashboard readiness

### P3.1 Outbox dispatcher

- [ ] Implement pending/delivered/failed/dead-letter state machine.
- [ ] Implement `POST /api/outbox/dispatch?dry_run=true`.
- [ ] Implement retry with max attempts and next-attempt timestamp.
- [ ] Add idempotency key generation per event type.
- [ ] Add tests for duplicate event prevention.

### P3.2 AssistX graph alignment

- [ ] Confirm final AssistX event sink payload shape.
- [ ] Map `assign.*` events to AssistX/Neo4j nodes:
  - `AssignmentDecision`;
  - `AssignmentLease`;
  - `AssignmentReason`;
  - `WorkerHeartbeat`;
  - relationships to `Task`, `AgentRun`, `SwarmNode`, `RouterDecision`.
- [ ] Add docs for graph model after AssistX event sink is verified.

### P3.3 Operator visibility

- [ ] Add basic JSON dashboard summary endpoint:
  - recent scheduler runs;
  - active assignments;
  - blocked reasons;
  - stale heartbeats;
  - outbox status.
- [ ] Add Prometheus-style `/metrics` after core counters stabilize.

## P4 - Controlled dispatch integration

### P4.1 Paperclip lane

- [ ] Add Paperclip lane adapter only after dry-run scoring and leases are tested.
- [ ] Ensure AssistX remains owner of Paperclip dispatch state.
- [ ] Use assignment events to explain why Paperclip was selected, not to bypass AssistX.
- [ ] Validate duplicate-dispatch prevention against AssistX terminal state and existing dispatch refs.

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
- [ ] Redact secrets in all logs and DB payloads.
- [ ] Add private-network deployment guidance.
- [ ] Add threat model document.

### P5.2 Reliability

- [ ] Add backoff for AssistX/router dependencies.
- [ ] Add circuit breaker for repeated dependency failures.
- [ ] Add scheduled background loop with jitter.
- [ ] Add graceful shutdown.
- [ ] Add DB backup guidance.

### P5.3 CI and quality

- [ ] Add GitHub Actions for tests/lint.
- [ ] Add type checking.
- [ ] Add API contract fixtures.
- [ ] Add integration tests with mocked AssistX/router.

## Suggested first coding prompt

Use this after the docs are merged:

```text
Implement the P0/P1 dry-run MVP for auto-assign.

Build a FastAPI service under src/auto_assign with settings, health endpoint, SQLite store, outbox, AssistX client, auto-router client, scheduler tick, assignment evaluation, and deterministic scorer. Keep dispatch disabled by default. Implement only dry-run assignment recommendations and skipped-lane reasons. Add tests proving local-only privacy gates, approval-required gates, duplicate active assignment prevention, router-unavailable conservative fallback, and idempotent outbox events.

Do not implement repo mutation, direct worker execution, or cloud dispatch. Do not persist raw prompts, response bodies, secrets, voiceprints, or enrollment samples.
```

## Acceptance checklist for next cycle

- [ ] `make test` passes.
- [ ] `GET /health` works with mocked/unavailable dependencies.
- [ ] `POST /api/assignments/evaluate` returns deterministic score/reasons.
- [ ] `POST /api/scheduler/tick` dry-runs candidate batches.
- [ ] Local-only/private tasks never select cloud/free API lanes.
- [ ] Approval-required tasks never dispatch.
- [ ] Duplicate active assignment is prevented.
- [ ] Outbox stores idempotent `assign.*` events.
- [ ] README/HLD/LLD remain aligned with implemented API.
