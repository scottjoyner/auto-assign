# auto-assign

`auto-assign` is the assignment, trigger, and heartbeat service for the AssistX homelab agent stack.

It is intended to sit between `auto-assist` and `auto-router` and answer the operational question:

> What work is eligible to run next, why should it go to a specific lane/node/model, what approval is required, and how do we know the assigned worker is still alive?

This repo should not become a second task database, a model router, or a repo-mutating executor. Its purpose is to evaluate work, produce explainable assignment decisions, track leases/heartbeats, and write assignment provenance back to AssistX/Neo4j.

Neo4j is the true brain of this system. Any SQLite database in `auto-assign` is only a cache/outbox/replay buffer so the service can survive outages and safely retry event delivery. SQLite must be safe to delete and rebuild from Neo4j/AssistX plus current dependency snapshots.

## System role

```mermaid
flowchart LR
    Sophia[Sophia voice / operator input] --> AssistX[auto-assist\ncanonical task + policy authority]
    AssistX --> Brain[(Neo4j\ntrue brain + durable graph)]
    Brain --> AssistX
    AssistX --> Assign[auto-assign\ntrigger + scheduler + assignment governor]
    Assign --> Cache[(SQLite cache/outbox only)]
    Assign --> Router[auto-router\nmodel routing + quota + service discovery]
    Router --> Models[LM Studio / free APIs / code-agent CLIs]
    Assign --> Paperclip[Paperclip / hermes_local\ncurrent cutover execution lane]
    Assign --> AssistX
    Router --> AssistX
```

### Responsibilities

`auto-assign` owns:

- scheduler ticks and backlog trigger evaluation;
- worker/node heartbeat ingestion;
- assignment scoring and explainable routing recommendations;
- task lease awareness and stale assignment visibility;
- approval gating before any higher-risk work is dispatched;
- coordination between AssistX task state and router capability/quota data;
- idempotent assignment events back to AssistX/Neo4j.

`auto-assign` does **not** own:

- canonical task state: owned by `auto-assist` and persisted in Neo4j;
- canonical assignment history: represented in Neo4j from `assign.*` events;
- raw voice/Sophia enrollment or auth records: owned by `auto-assist`;
- provider/model request routing: owned by `auto-router`;
- quota reservation internals: owned by `auto-router`;
- code mutation, commit, push, or shell execution: future worker adapters only, gated by approval/sandbox policy;
- prompt/response body history: should not be persisted by default.

## Integration boundaries

| System | Role | `auto-assign` integration |
|---|---|---|
| [`auto-assist`](https://github.com/scottjoyner/auto-assist) | Canonical task, policy, Sophia, Paperclip dispatch, graph authority | Read eligible tasks and policy context; write assignment decisions, lease transitions, trigger outcomes, and heartbeat summaries back as events. |
| Neo4j | True brain and durable graph memory | Stores task relationships, assignment decisions, policy decisions, heartbeats, leases, router decisions, agent runs, artifacts, and provenance relationships through AssistX. |
| [`auto-router`](https://github.com/scottjoyner/auto-router) | OpenAI-compatible router, quota manager, service/model/CLI discovery, route provenance | Query route/capability/quota summaries; request dry-run plans for assignment scoring; never bypass privacy/local-only rules. |
| SQLite cache in `auto-assign` | Local resilience only | Pending outbox events, dedupe keys, transient scheduler summaries, inbound event mirrors, inbound processing state, heartbeat mirrors, and rebuildable local views. Not a system of record. |
| Paperclip / `hermes_local` | Current cutover execution path | Treat as the supported execution lane until direct worker claiming is explicitly promoted. |
| Future direct workers | Deferred execution lanes | Use only after approval, sandboxing, leases, and write-back contracts are implemented. |

## Operating model

```mermaid
sequenceDiagram
    participant AX as auto-assist
    participant N as Neo4j brain
    participant AA as auto-assign
    participant C as SQLite cache/outbox
    participant AR as auto-router
    participant W as worker lane

    AX->>AA: task candidate / scheduler tick / event
    AA->>C: cache inbound event mirror
    AA->>AA: explicitly process unprocessed inbound event
    AA->>AX: load task + policy + approval context
    AX->>N: read canonical graph state
    AA->>AR: load node/provider/quota/capability snapshot
    AA->>AA: score lane, risk, privacy, age, retry, capability
    AA->>C: cache pending assign.* event for retry/idempotency
    AA->>C: mark inbound event processing result
    AA->>AX: write assignment recommendation + reasons
    AX->>N: persist canonical assignment graph
    alt approval required
        AX->>AA: approval decision
    end
    AA->>W: dispatch or reserve through approved lane
    W->>AA: heartbeat / progress / completion
    AA->>AX: lease, heartbeat, completion, failure, artifact refs
    AX->>N: persist durable provenance
```

## Documentation

- [`docs/HLD.md`](docs/HLD.md) — high-level architecture, system context, trigger/heartbeat role, and integration boundaries.
- [`docs/LLD.md`](docs/LLD.md) — low-level modules, API contracts, event payloads, scoring model, persistence model, and sequence flows.
- [`docs/NEO4J_BRAIN_AND_CACHE_POLICY.md`](docs/NEO4J_BRAIN_AND_CACHE_POLICY.md) — canonical decision that Neo4j is the true brain and SQLite is only cache/outbox/replay state.
- [`docs/IMPLEMENTATION_PLAN.md`](docs/IMPLEMENTATION_PLAN.md) — prioritized implementation plan for the next cycle.
- [`docs/LOCAL_VALIDATION.md`](docs/LOCAL_VALIDATION.md) — local smoke-test and dry-run validation guide.

## API surface

Initial service endpoints should be private-network/Tailscale only:

| Method | Path | Purpose |
|---|---|---|
| `GET` | `/health` | Service, dependency, Neo4j/AssistX brain connectivity, router, cache, and scheduler health. |
| `POST` | `/api/events` | Accept an internal event envelope into the local inbound-event mirror. Does not add to outbound outbox. |
| `GET` | `/api/events` | List local inbound event mirror rows, optionally filtered by event type. |
| `POST` | `/api/events/process` | Explicitly process unprocessed inbound events. Defaults to dry-run and skips already processed events unless `include_processed=true`. |
| `GET` | `/api/events/processing` | List local inbound processing history and last action per idempotency key. |
| `POST` | `/api/scheduler/tick` | Manually run one assignment evaluation cycle. |
| `GET` | `/api/scheduler/runs` | List local scheduler run summaries. |
| `POST` | `/api/assignments/evaluate` | Evaluate one task or candidate batch without dispatching. |
| `GET` | `/api/assignments` | List recent local mirror/cache rows with graph reconciliation status. |
| `GET` | `/api/assignments/{assignment_id}` | Read one local assignment mirror row. |
| `POST` | `/api/assignments/{assignment_id}/approve` | Approve a gated assignment through AssistX/Neo4j policy flow. |
| `POST` | `/api/assignments/{assignment_id}/release` | Release an expired or blocked assignment through AssistX/Neo4j event write-back. |
| `POST` | `/api/heartbeats` | Record node/worker heartbeat payloads. |
| `GET` | `/api/heartbeats` | List recent local heartbeat mirror rows. |
| `GET` | `/api/heartbeats/stale` | Read-only view of locally stale heartbeat rows. |
| `GET` | `/api/outbox/summary` | Summarize local outbox event status counts. |
| `GET` | `/api/outbox/events` | List local outbox events, optionally filtered by status. |
| `POST` | `/api/outbox/dispatch` | Dry-run or write pending outbox events to AssistX. |
| `POST` | `/api/outbox/reconcile` | Reconcile pending events against AssistX/Neo4j idempotency status. |
| `GET` | `/api/ops/summary` | Compact operator view of cache, outbox, heartbeat, scheduler, inbound event, processing, and safety status. |

## Inbound trigger processing

Inbound events are cached separately from outbound `assign.*` events. This prevents `auto-assign` from echoing router or AssistX events back to AssistX as if it authored them.

Current inbound processors:

| Inbound event type | Processing action |
|---|---|
| `task.candidate.created` | Evaluates the task and queues an outbound `assign.assignment.recommended` or `assign.assignment.skipped` event. |
| `router.quota_snapshot.recorded` | Runs a dry-run scheduler tick. |
| `router.service_snapshot.recorded` | Runs a dry-run scheduler tick. |
| Unknown event type | Records an ignored processing action. |

Processing is protected by `inbound_event_processing.idempotency_key`. By default, once an inbound event is processed, later processor calls skip it. Explicit replay requires:

```bash
curl -X POST 'http://localhost:8090/api/events/process?include_processed=true&dry_run=true'
```

## First implementation targets

1. Bootstrap a FastAPI service with `health`, `scheduler/tick`, and `assignments/evaluate`.
2. Add AssistX client for read-only candidate intake, graph-state reconciliation, and event write-back.
3. Add router client for context/quota/capability snapshots.
4. Add a deterministic assignment scorer with explainable skip/select reasons.
5. Add SQLite local cache/outbox/replay buffer so assignment events survive AssistX/Neo4j downtime.
6. Add lease and heartbeat state transitions, with Neo4j/AssistX as the canonical state.
7. Keep execution dispatch disabled by default until approval and sandbox controls are present.

## Safety defaults

- Neo4j/AssistX is the source of truth; SQLite is cache only.
- Private network only by default.
- Local-only and sensitive work must stay local.
- Unknown speaker or non-Scott-originated work requires approval.
- No repo write/commit/push without explicit operator approval.
- No secrets, raw prompts, voiceprints, or enrollment samples in assignment events.
- All write-back events must be idempotent.
