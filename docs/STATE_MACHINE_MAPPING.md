# State Machine Mapping

Canonical source of truth: **AssistX Neo4j**.

| AssistX (canonical) | auto-assign (cache) | kanban (scratchpad) |
|---------------------|---------------------|---------------------|
| REVIEW | BLOCKED | triage |
| READY | RECOMMENDED | ready |
| CLAIMED | DISPATCHED | running |
| RUNNING | RUNNING | running |
| DONE | DONE | done |
| FAILED | FAILED | blocked |
| CANCELLED | RELEASED | archived |

## Sync Direction

**Always** AssistX → auto-assign → kanban. Read from AssistX `event_status`; never write back to AssistX from the lower layers.

## Rationale

- auto-assign SQLite is a read-through cache — never canonical.
- kanban SQLite is a per-profile scratchpad — tasks reference their AssistX `task_id` via `assistx_task_id`.
- kanban status transitions (complete/block) should notify AssistX when `assistx_task_id` is set, but AssistX remains the final arbiter of task lifecycle.
