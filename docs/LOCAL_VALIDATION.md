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

## 2. Run the standard validation target

```bash
make validate
```

This runs the same command sequence expected for CI:

```bash
ruff check src tests
python -m compileall src
pytest -q
```

Expected result:

- scorer tests pass;
- cache/outbox tests pass;
- API tests pass;
- client normalization tests pass;
- assignment event-type tests pass;
- ruff reports no lint failures;
- Python compilation succeeds.

You can also run pieces individually:

```bash
make test
make lint
make smoke
```

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

## 5. Dry-run inbound trigger processing

Post a task candidate event:

```bash
curl -X POST http://localhost:8090/api/events \
  -H 'Content-Type: application/json' \
  -d '{
    "event_type": "task.candidate.created",
    "idempotency_key": "task.candidate.created:ASS-trigger-smoke",
    "subject": "ASS-trigger-smoke",
    "payload": {
      "task_id": "ASS-trigger-smoke"
    }
  }' | jq
```

Process unprocessed inbound events:

```bash
curl -X POST 'http://localhost:8090/api/events/process?event_type=task.candidate.created&dry_run=true' | jq
```

Expected behavior:

- the inbound event is mirrored in `inbound_events`;
- the processor evaluates the task once;
- a local processing marker is written;
- an outbound `assign.assignment.recommended`, `assign.assignment.approval_required`, or `assign.assignment.skipped` event is queued;
- a second call without `include_processed=true` skips the already-processed inbound event.

Check processing history:

```bash
curl http://localhost:8090/api/events/processing | jq
```

Explicit replay:

```bash
curl -X POST 'http://localhost:8090/api/events/process?event_type=task.candidate.created&dry_run=true&include_processed=true' | jq
```

## 6. Dry-run outbox dispatch

```bash
curl -X POST 'http://localhost:8090/api/outbox/dispatch?dry_run=true&limit=25' | jq
```

Expected behavior:

- pending events are shown as dry-run events;
- events remain pending;
- nothing is written to AssistX/Neo4j.

## 7. Reconcile outbox

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

## 8. Docker smoke test

```bash
docker compose up --build
```

Then:

```bash
curl http://localhost:8090/health | jq
```

## 9. Intended GitHub Actions workflow

The intended CI workflow should run the same commands as `make validate` across Python 3.11 and 3.12.

If the workflow file is not present, create `.github/workflows/ci.yml` with:

```yaml
name: CI

on:
  push:
    branches: [main]
  pull_request:
    branches: [main]
  workflow_dispatch:

jobs:
  lint-test:
    name: Lint and test
    runs-on: ubuntu-latest
    strategy:
      fail-fast: false
      matrix:
        python-version: ["3.11", "3.12"]

    steps:
      - name: Checkout
        uses: actions/checkout@v4

      - name: Set up Python
        uses: actions/setup-python@v5
        with:
          python-version: ${{ matrix.python-version }}
          cache: pip

      - name: Install package and dev dependencies
        run: python -m pip install -e '.[dev]'

      - name: Validate
        run: make validate
```

## 10. Important validation rules

- SQLite is only cache/outbox/replay state.
- Deleting `data/auto_assign.sqlite3` must not be interpreted as deleting canonical assignment history.
- Neo4j via AssistX is the true brain.
- Any real dispatch path must remain disabled until dry-run scoring, outbox write-back, reconciliation, and approval gates are verified.
