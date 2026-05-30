# Local Validation Runbook

This runbook validates the current dry-run `auto-assign` MVP.

## 1. Clone and install

```bash
git clone https://github.com/scottjoyner/auto-assign.git
cd auto-assign
python -m venv .venv
source .venv/bin/activate
make install
```

## 2. Run tests and lint

```bash
make test
make lint
```

Expected result:

- scorer tests pass;
- cache/outbox tests pass;
- API tests pass;
- ruff reports no lint failures.

## 3. Start the service locally

```bash
cp .env.example .env
make dev
```

Open another shell:

```bash
curl http://localhost:8090/health | jq
```

Expected behavior when AssistX or auto-router are not running:

- service starts;
- `cache.reachable=true`;
- `cache.role=cache_outbox_only`;
- `assistx.brain=neo4j_via_assistx`;
- status may be `degraded` until AssistX is reachable.

## 4. Dry-run one assignment

```bash
curl -X POST http://localhost:8090/api/assignments/evaluate \
  -H 'Content-Type: application/json' \
  -d '{
    "task_id": "ASS-local-smoke",
    "dry_run": true,
    "candidate_lanes": ["paperclip", "router_model", "free_api"]
  }' | jq
```

Expected behavior:

- returns an assignment decision;
- `cache_only=true`;
- selected lane should be conservative if router is unavailable;
- an outbox event is queued but not delivered.

Check outbox:

```bash
curl http://localhost:8090/api/outbox/summary | jq
```

Expected:

```json
{
  "cache_role": "outbox_replay_buffer",
  "canonical_source": "neo4j_via_assistx",
  "summary": {
    "pending": 1
  }
}
```

## 5. Dry-run outbox dispatch

```bash
curl -X POST 'http://localhost:8090/api/outbox/dispatch?dry_run=true&limit=25' | jq
```

Expected behavior:

- pending events are shown as dry-run events;
- events remain pending;
- nothing is written to AssistX/Neo4j.

## 6. Reconcile outbox

```bash
curl -X POST 'http://localhost:8090/api/outbox/reconcile?limit=100' | jq
```

Expected behavior without AssistX event-status support:

- checked events remain pending;
- no events are marked delivered;
- no canonical graph conflict is inferred.

Expected behavior once AssistX supports idempotency lookup:

- already-applied graph events are marked `delivered` locally;
- graph conflicts are dead-lettered locally;
- Neo4j/AssistX state wins over local cache state.

## 7. Docker smoke test

```bash
docker compose up --build
```

Then:

```bash
curl http://localhost:8090/health | jq
```

## 8. Important validation rules

- SQLite is only cache/outbox/replay state.
- Deleting `data/auto_assign.sqlite3` must not be interpreted as deleting canonical assignment history.
- Neo4j via AssistX is the true brain.
- Any real dispatch path must remain disabled until dry-run scoring, outbox write-back, reconciliation, and approval gates are verified.
