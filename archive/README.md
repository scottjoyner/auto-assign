# Archive — auto-assign remediation (2026-07-16)

Files moved here are orphaned / contradictory with the current assignment
service and the repo's Neo4j-via-AssistX policy, or they pulled heavy ML
deps (PyTorch/transformers) into the image. They are kept for reference and
are fully reversible (git mv history preserved).

## W-31 — orphan Flask `app.py` (speaker-clustering UI)
- `app.py` — 488-LOC Flask UI doing PyTorch/Neo4j speaker clustering. Contradicts
  the FastAPI `src/auto_assign/` assignment service and the AssistX/Neo4j policy.
- `templates/` — Flask Jinja UI templates for that app.
- Removed from `Dockerfile`: `COPY app.py` + `EXPOSE 8080` (lines 10, 18).
- The FastAPI service remains the only app (uvicorn auto_assign.main:app :8090).

## W-33 — unused deps
- Flask/torch/transformers runtime deps moved out of base `requirements.txt` /
  `pyproject.toml` into `[project.optional-dependencies].flask` (kept, not deleted).

## W-34 — CacheStore concurrency hazard
`src/auto_assign/cache.py` uses a single shared `sqlite3` connection
(`check_same_thread=False`) on the async event loop. Documented as a hazard with
a TODO to migrate to `aiosqlite` (removed from deps in W-33). No behavior change.

## W-40 — startup guard + committed sqlite
`src/auto_assign/main.py` lifespan now builds the service defensively so a
schema-init/migration failure degrades gracefully (logs + starts without the
service) instead of crashing import/startup.
`data/auto_assign.sqlite3` is already covered by `.gitignore` (`*.sqlite3`, `data/`)
and is NOT tracked, so nothing to `git rm`. On this machine the file is owned by
root and left in place; a real cleanup deletes it from the runtime volume.
