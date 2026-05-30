# High-Level Design: auto-assign

## 1. Executive summary

`auto-assign` is the trigger, scheduler, assignment, and heartbeat layer for the AssistX agent ecosystem.

It should evaluate eligible work from `auto-assist`, combine that work with node/model/quota/capability context from `auto-router`, and produce an explainable assignment decision. It does not own canonical task state and does not directly replace the current Paperclip cutover path.

The system exists because the stack needs a clear layer that understands **why** work should run, **where** it should run, **when** it should wait, **what approval is needed**, and **whether the assigned lane is still alive**.

## 2. Design goals

1. Keep `auto-assist` as the task-state and policy authority.
2. Keep `auto-router` as the model/provider/quota routing authority.
3. Make assignment decisions deterministic, explainable, and auditable.
4. Prevent duplicate dispatches through idempotency keys and leases.
5. Treat scheduler ticks and heartbeats as first-class control-plane events.
6. Preserve the current Paperclip / `hermes_local` execution lane until direct workers are explicitly promoted.
7. Keep sensitive, local-only, and voice-auth/enrollment data out of cloud and out of assignment event bodies.
8. Support future direct worker lanes without re-architecting the graph/event model.

## 3. Non-goals

`auto-assign` should not:

- store canonical tasks as the source of truth;
- perform raw model routing or quota reservation internally;
- mutate repos or external systems directly;
- bypass AssistX approval requirements;
- store raw prompts, response bodies, voiceprints, enrollment samples, or secrets;
- scrape arbitrary services outside the private network;
- introduce a second execution authority before the current cutover path is stable.

## 4. System context

```mermaid
flowchart TB
    subgraph Input[Input and control sources]
        Sophia[Sophia voice events]
        Operator[Operator dashboard / CLI]
        Timer[Scheduler tick]
        Heartbeat[Node + worker heartbeat]
    end

    subgraph AssistX[auto-assist]
        TaskState[Canonical Task state]
        Policy[Voice / risk / approval policy]
        Events[Signed event envelope]
        PaperclipDispatch[Paperclip dispatch sync]
    end

    subgraph Assign[auto-assign]
        Intake[Candidate intake]
        Scheduler[Trigger evaluator]
        Scorer[Assignment scorer]
        Lease[Lease + heartbeat monitor]
        Outbox[Assignment event outbox]
    end

    subgraph Router[auto-router]
        Context[Context projection]
        Quota[Quota snapshots]
        Capability[Provider / model / CLI capability]
        DryRun[Dry-run route planning]
    end

    subgraph Execution[Execution lanes]
        Paperclip[Paperclip / hermes_local]
        Local[Local LM Studio / local tools]
        FreeAPI[Free API providers]
        FutureWorkers[Future direct worker nodes]
    end

    Sophia --> AssistX
    Operator --> Assign
    Timer --> Assign
    Heartbeat --> Assign
    AssistX --> Assign
    Assign --> Router
    Router --> Assign
    Assign --> Paperclip
    Assign -.future gated.-> Local
    Assign -.future gated.-> FreeAPI
    Assign -.future gated.-> FutureWorkers
    Assign --> AssistX
    Router --> AssistX
```

## 5. Data ownership

| Data / decision | Owner | `auto-assign` behavior |
|---|---|---|
| Task lifecycle | `auto-assist` | Reads candidates; writes assignment/lease/progress events back. |
| Sophia voice/auth policy | `auto-assist` | Consumes policy decisions; never stores voiceprints or enrollment samples. |
| Paperclip cutover dispatch | `auto-assist` / Paperclip | Treats Paperclip as supported execution lane until replacement is approved. |
| Model/provider routing | `auto-router` | Uses route dry-runs and capability/quota snapshots; does not choose concrete model providers alone. |
| Quota counters/reservations | `auto-router` | Reads summaries and respects reserves; does not directly spend quota. |
| Assignment recommendation | `auto-assign` | Owns score, selected lane, skip reasons, idempotency, and lease metadata. |
| Local cache/outbox | `auto-assign` | Durable local recovery only; not canonical state. |
| Artifacts | AssistX/Paperclip/NAS later | Stores references only. |

## 6. Assignment lifecycle

```mermaid
stateDiagram-v2
    [*] --> Observed
    Observed --> Ineligible: privacy / blocked / missing policy
    Observed --> Candidate: eligible task seen
    Candidate --> Recommended: lane scored
    Recommended --> AwaitingApproval: approval_required
    Recommended --> Reserved: auto-approved low risk
    AwaitingApproval --> Reserved: approved
    AwaitingApproval --> Blocked: denied / expired
    Reserved --> Dispatched: sent to current approved lane
    Dispatched --> Running: worker heartbeat / run observed
    Running --> Done: completion received
    Running --> FailedRetryable: retryable failure
    Running --> FailedTerminal: terminal failure
    Running --> LeaseExpired: heartbeat missed
    LeaseExpired --> Candidate: released for retry
    FailedRetryable --> Candidate: retry budget remains
    Done --> [*]
    Blocked --> [*]
    FailedTerminal --> [*]
    Ineligible --> [*]
```

## 7. Trigger and heartbeat model

`auto-assign` should react to four trigger classes:

| Trigger | Source | Purpose |
|---|---|---|
| `scheduler.tick` | cron/systemd/operator/API | Periodically evaluate eligible backlog. |
| `task.candidate.created` | AssistX event sink | Evaluate a newly eligible task immediately. |
| `worker.heartbeat` | node/worker/Paperclip/router | Refresh availability and detect stale assignments. |
| `quota.snapshot.changed` | auto-router event or poll | Re-score backlog when free quota, local models, or circuits change. |

Heartbeats should affect assignment state but should not alone authorize execution. A node can be online while still blocked by policy, privacy, capability, quota, or approval state.

## 8. Assignment scoring model

The scorer should produce a ranked decision with explicit reasons.

Inputs:

- AssistX task priority, age, retry count, risk level, approval state, privacy labels, and required capabilities;
- current Paperclip availability and cutover status;
- router context projection for nodes, providers, local LM Studio endpoints, free API lanes, and code-agent CLIs;
- quota summaries and reserve mode;
- heartbeat freshness;
- local-only and sensitivity flags;
- operator policy overrides.

Candidate lane categories:

| Lane | Description | Default posture |
|---|---|---|
| `paperclip` | Current supported non-realtime execution path through Paperclip / `hermes_local`. | Preferred for execution cutover. |
| `router_model` | LLM request routed through `auto-router`. | Allowed for planning/drafting when privacy/quota policy permits. |
| `local_only` | Local LM Studio or local tools. | Required for sensitive/private work. |
| `free_api` | Hosted free quota providers. | Allowed only for non-sensitive work and after reserve checks. |
| `direct_worker` | Future direct worker nodes. | Disabled until sandbox/approval/lease flow is complete. |
| `blocked` | No safe route available. | Emits skip reason. |

Example score components:

| Component | Meaning |
|---|---|
| `policy_fit` | Does policy allow this lane? |
| `capability_fit` | Does lane support required tools/model/task type? |
| `privacy_fit` | Can data leave local boundary? |
| `availability_fit` | Are node/service heartbeats fresh? |
| `quota_fit` | Is quota available without violating reserve? |
| `risk_fit` | Does task require approval or sandboxing? |
| `staleness_boost` | Older safe backlog gets priority. |
| `retry_penalty` | Repeated failures lower priority or force review. |

## 9. Event architecture

All cross-service mutation should happen through idempotent events. `auto-assign` can cache local state, but AssistX remains the graph write target.

Recommended event types:

| Event type | Meaning |
|---|---|
| `assign.scheduler.tick.started` | A scheduler pass began. |
| `assign.scheduler.tick.completed` | A scheduler pass ended with counts and decisions. |
| `assign.assignment.recommended` | A task was scored and a lane was recommended. |
| `assign.assignment.skipped` | A task was skipped with explicit reason. |
| `assign.assignment.approval_required` | A decision needs operator approval. |
| `assign.assignment.reserved` | A lane was reserved/leased for work. |
| `assign.assignment.dispatched` | Work was handed to an approved lane. |
| `assign.assignment.heartbeat` | Progress or liveness observed for an assignment. |
| `assign.assignment.released` | Assignment lease expired or was manually released. |
| `assign.assignment.completed` | Work reached terminal success. |
| `assign.assignment.failed` | Work failed with retryable/terminal classification. |
| `assign.worker.heartbeat.recorded` | Node or worker heartbeat was accepted. |

## 10. Deployment posture

```mermaid
flowchart LR
    subgraph PrivateNet[Private LAN / Tailscale]
        AA[auto-assign :8090]
        AX[auto-assist :8000]
        AR[auto-router :8088]
        DB[(SQLite local cache/outbox)]
        Redis[(optional Redis)]
        Neo[(Neo4j via AssistX)]
    end

    AA --> AX
    AA --> AR
    AA --> DB
    AA -.optional.-> Redis
    AX --> Neo
    AR --> AX
```

Recommended defaults:

- expose only on private LAN/Tailscale;
- require signed event envelopes or shared internal auth;
- persist SQLite cache/outbox;
- keep dispatch disabled in development mode;
- poll AssistX/router with backoff;
- do not log prompt bodies or secrets;
- provide a dry-run mode for every scheduler action.

## 11. Key risks and mitigations

| Risk | Mitigation |
|---|---|
| Duplicate task dispatch | Idempotency keys, AssistX task links, assignment leases, and terminal-state checks. |
| Competing task authority | Treat local DB as cache/outbox only; write canonical events to AssistX. |
| Privacy leak to cloud providers | Enforce local-only/privacy labels before router dry-run or provider selection. |
| Quota starvation for Sophia realtime | Respect router reserve mode and do not burn critical reserves. |
| Stale worker holds assignment | Lease expiration and heartbeat monitor. |
| Repo mutation without approval | Direct worker lane disabled by default; explicit approval required for write/commit/push. |
| Prompt/secret persistence | Store metadata, hashes, and refs only. |

## 12. MVP scope

The first implementation cycle should deliver:

1. FastAPI service shell and health endpoint.
2. AssistX client for task candidate intake and event write-back.
3. Router client for context/quota/capability snapshots.
4. Scheduler tick endpoint with dry-run mode.
5. Assignment scorer with skip/select reasons.
6. Local SQLite tables for assignments, heartbeats, scheduler runs, and outbox.
7. Heartbeat ingestion and stale lease release logic.
8. README, HLD, LLD, and implementation plan.
