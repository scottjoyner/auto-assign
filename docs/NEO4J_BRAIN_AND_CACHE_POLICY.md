# Neo4j Brain and SQLite Cache Policy

## Decision

Neo4j is the durable brain and source of truth for the AssistX / auto-assign / auto-router ecosystem.

SQLite may exist in `auto-assign`, but only as a local cache, idempotent outbox, retry buffer, or short-lived operational mirror. SQLite must never become the canonical assignment store, task store, memory store, policy store, or provenance graph.

## Why this matters

The system is designed around a graph-backed control plane. The graph is what knows:

- who created the task;
- why the task exists;
- which policy allowed or blocked it;
- what memory/context informed the decision;
- which node, worker, model, lane, or provider was selected;
- what assignment reasons were produced;
- what heartbeats, leases, retries, failures, and artifacts belong to the work;
- how the work relates to Sophia, AssistX, auto-router, Paperclip, agent runs, tools, and future workers.

A local relational cache cannot represent this whole memory and relationship fabric. It is useful for durability during outages, fast local reads, and replay safety, but it is not the brain.

## Storage roles

| Store | Role | Allowed data | Not allowed |
|---|---|---|---|
| Neo4j via AssistX | True brain / canonical graph | Tasks, assignment decisions, policy decisions, leases, worker heartbeats, router decisions, agent runs, tool calls, artifacts, memory/context links, provenance relationships | Nothing; this is the durable authority, subject to privacy rules. |
| SQLite in auto-assign | Cache / outbox / replay buffer | Pending outbound events, dedupe keys, transient scheduler summaries, last-seen dependency health, local mirror rows needed for retry/idempotency | Canonical task state, canonical assignment state, long-term memory, final provenance authority, policy source of truth. |
| Redis, if used | Ephemeral coordination | Locks, short TTL counters, transient scheduler locks, optional rate/lease helpers | Durable task, assignment, memory, or policy data. |

## Core rule

Every meaningful assignment fact should eventually be represented in Neo4j through AssistX events.

SQLite rows are acceptable only when they are:

1. replayable into Neo4j;
2. rebuildable from Neo4j and current dependency snapshots;
3. safe to delete without losing canonical system history;
4. clearly marked as cache/outbox/local mirror state;
5. never treated as the final answer when Neo4j disagrees.

## Canonical graph concepts

`auto-assign` should write or request AssistX to write these graph-backed concepts:

```text
(Task)-[:HAS_ASSIGNMENT_DECISION]->(AssignmentDecision)
(AssignmentDecision)-[:SELECTED_LANE]->(ExecutionLane)
(AssignmentDecision)-[:TARGETED]->(SwarmNode|RouterProvider|PaperclipAdapter|AgentWorker)
(AssignmentDecision)-[:HAS_REASON]->(AssignmentReason)
(AssignmentDecision)-[:BASED_ON_POLICY]->(PolicyDecision)
(AssignmentDecision)-[:USED_ROUTER_CONTEXT]->(RouterContextProjection)
(AssignmentDecision)-[:USED_ROUTER_DECISION]->(RouterDecision)
(AssignmentDecision)-[:CREATED_LEASE]->(AssignmentLease)
(AssignmentLease)-[:HEARTBEAT]->(WorkerHeartbeat)
(AssignmentDecision)-[:PRODUCED_RUN]->(AgentRun)
(AgentRun)-[:PRODUCED]->(Artifact)
```

Exact labels can evolve with AssistX, but the principle should not: the graph must preserve the relationships that explain why work went where.

## auto-assign behavior

`auto-assign` should:

- read task candidates and policy context from AssistX;
- read node/provider/quota/capability context from auto-router;
- score and explain assignment decisions;
- write idempotent `assign.*` events back to AssistX;
- cache pending events locally only until AssistX confirms delivery;
- reconcile local cache rows against Neo4j/AssistX on startup;
- prefer Neo4j/AssistX state when local cache and graph state disagree;
- expose cache health and outbox lag so operators can see when graph write-back is delayed.

`auto-assign` should not:

- answer historical/provenance questions from SQLite when Neo4j is available;
- mark work globally complete only because a local cache row says it is complete;
- treat a local assignment row as authoritative after AssistX returns different task state;
- use SQLite as a permanent analytics store;
- persist raw prompts, response bodies, secrets, voiceprints, or enrollment samples.

## Startup reconciliation

On startup, `auto-assign` should run a reconciliation pass:

1. Load pending local outbox events.
2. Ask AssistX/Neo4j which events are already applied by idempotency key.
3. Mark already-applied local events as delivered.
4. Retry events that are missing and still valid.
5. Dead-letter events that conflict with terminal graph state.
6. Refresh local mirrors from current AssistX/router snapshots.

## Failure behavior

When AssistX/Neo4j is unavailable:

- scheduler dry-run may continue;
- assignment recommendations may be cached as pending outbox events;
- dispatch should remain disabled unless explicitly allowed by a carefully reviewed emergency mode;
- no irreversible side effects should be performed based only on cache state;
- health should report degraded brain connectivity.

When SQLite is unavailable:

- the service may still perform read-only evaluation against AssistX/router;
- durable event write-back should fail closed rather than silently dropping events;
- no assignment dispatch should occur unless events can be durably recorded in Neo4j.

## Implementation implications

The next code pass should rename language and modules to make this clear:

- prefer `cache`, `outbox`, `local_mirror`, and `replay_buffer` terminology;
- avoid names like `primary_db`, `assignment_db`, or `task_store` for SQLite;
- add tests proving SQLite cache deletion does not define canonical history;
- add tests proving Neo4j/AssistX state wins conflicts;
- add operator metrics for outbox backlog and Neo4j write-back lag.
