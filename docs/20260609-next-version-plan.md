# Next Version Plan — 2026-06-09

Cross-repo plan covering auto-assist, auto-router, and auto-assign.

---

## ✅ Completed (Sessions 1–5)

| Priority | Area | Item |
|----------|------|------|
| P0 | All repos | **Standardise health checks** — Unified `{ok, service, version, uptime, deps}` format across all 3 health endpoints |
| P0 | auto-assist | **Neo4j service in base compose** — Added `neo4j:5-community` under `:neo4j` profile in `docker-compose.yml` |
| P0 | auto-assist | **SSE disconnect handling** — Both `/api/answers/events` and `/api/answers/{id}/events` check `request.is_disconnected()` |
| P0 | auto-assist | **Worker health monitoring** — Added HTTP health server on port 8100; Docker healthcheck updated |
| P0 | auto-assist | **`_workflow_control_state` thread safety** — Wrapped in `threading.Lock` with `_get_workflow_control()` / `_set_workflow_control()` helpers |
| P0 | auto-assign | **`CacheStore` connection pooling** — Single persistent SQLite connection with WAL mode, `check_same_thread=False` |
| P0 | auto-assign | **`CacheStore` connection pooling** — Single persistent SQLite connection with WAL mode, `check_same_thread=False` (NOTE: documented concurrency hazard; async `aiosqlite` migration NOT done — see W-34 in LLD_UNIFIED_FLEET) |
| P0 | auto-router | **Config validation at startup** — Warns on missing config files and empty provider registry |
| P0 | auto-router | **Agentgateway env vars in compose** — All 10 `AUTO_ROUTER_AGENTGATEWAY_*` vars added to `docker-compose.yml` |
| P1 | auto-assist | **Neo4j connection pooling** — `GraphDatabase.driver()` now accepts `NEO4J_MAX_CONNECTION_POOL_SIZE`, `NEO4J_CONNECTION_ACQUISITION_TIMEOUT`, `NEO4J_MAX_TRANSACTION_RETRY_TIME` env vars |
| P1 | auto-assist | **Module-level imports** — `Neo4jClient`/`PaperclipClient` imports deferred inside function bodies in all 3 scheduler modules |
| P1 | auto-assign | **AssistXClient retry with backoff** — `post_event()` uses exponential backoff (2^N + jitter, 3 retries) for transient failures |
| P1 | auto-assign | **Database migrations** — Created `migration.py` framework with `_register` decorator, auto-apply, `_schema_version` tracking; v1 migration with all tables |
| P1 | All repos | **Unified structured logging** — `logging_utils.py` in each repo with JSON formatter (`LOG_FORMAT=json`), correlation ID filter, and `CorrelationIdMiddleware` |
| P1 | auto-assist | **API Token strategy** — Removed weak default credentials (`BASIC_AUTH_USER`/`BASIC_AUTH_PASS` no longer fall back); warns if auth not configured |
| P1 | auto-assign | **Flask app hardening** — `app.py` now has opt-in HTTP Basic auth via `BASIC_AUTH_USER`/`BASIC_AUTH_PASS` env vars |
| P1 | auto-assign | **CORS hardening** — Changed `allow_origins=["*"]` to `os.getenv("CORS_ALLOW_ORIGINS")` |
| P1 | auto-router | **Secret rotation** — Verified no weak defaults in auto-router; all API keys default to empty |
| P1 | All repos | **Prometheus metrics** — auto-assist already had full metrics with `/metrics` endpoint; auto-router already had `/metrics` in OpenMetrics format; added `metrics.py` + `/metrics` endpoint to auto-assign with request counters, latency, events, heartbeats, scheduler gauges. Added `prometheus-client` dep to auto-assign |
| P1 | All repos | **Cross-service tracing** — Created `tracing_utils.py` in all 3 repos with `contextvars`-based trace context. Middleware in all 3 propagates `X-Trace-ID`/`X-Correlation-ID` headers on ingress/egress. auto-assign clients inject trace headers on all outgoing requests |
| P2 | auto-assist | **compose.override.yml intent documented** — Added comment explaining `depends_on: []` is for local dev with host infra |
| P2 | auto-router | **Duplicate `_provider_node_id`** — Removed duplicate in `route_event_patch.py`, imported from `route_events.py` instead |
| P2 | auto-router | **Groq RPM fix** — Changed `providers.yaml` / `providers.example.yaml` from 30 → 5 (matches free tier limit) |
| P2 | auto-assign | **Dual dependency files consolidated** — `pyproject.toml` has all deps with `flask`/`neo4j` extras; `requirements.txt` flattened for Docker |
| P2 | auto-assign | **sentence-transformers lazy-loaded** — `AutoTokenizer.from_pretrained` / `AutoModel.from_pretrained` moved to `_get_embed_model()` lazy getter |
| P2 | auto-assign | **Hardcoded lane scores configurable** — `base_by_lane`, `local_only_boost`, `priority_boost`, `retry_penalty` moved to `Settings` env vars |
| P2 | auto-assist | **`schemas.py` dead code** — Inspected; `ExtractedTasks` is actively used by `pipeline_summarize.py`. No action needed |
| P2 | All repos | **Unified Makefile** — Standard targets (`install`, `dev`, `test`, `lint`, `format`, `smoke`, `build`, `docker-up`, `docker-down`) consistent across all 3 repos. Preserved repo-specific targets (gateway, go-live) |
| P2 | All repos | **Pre-commit hooks** — `.pre-commit-config.yaml` added to all 3 repos with `ruff` + `mypy` hooks. `[tool.mypy]` section added to pyproject.toml for auto-router and auto-assign. `mypy`/`pre-commit` added to dev deps |
| P2 | All repos | **docker-compose unified** — Created `docker-compose.unified.yml` at repo root that starts all 3 services + Redis + Neo4j, with healthcheck-ordered dependencies and a `:test` profile for integration tests |
| P2 | All repos | **Integration test suite foundation** — Created `tests/integration/` in all 3 repos with conftest providing httpx clients for each service URL, and `test_health.py` covering cross-service health checks, correlation/trace ID propagation |
| P2 | All repos | **Plan docs updated** — All 3 repos synced after each session |

---

## 🔲 Remaining Work

| Priority | Area | Item |
|----------|------|------|
| P2 | auto-assist | **Test coverage** — Increase coverage with unit tests for uncovered modules |
| P2 | auto-router | **Provider mock tests** — Unit tests for provider selection, quota management, and circuit breaker logic |
| P2 | auto-assign | **Scheduler/lease tests** — Test `expire_stale_leases`, `scheduler_tick`, and heartbeat renewals |
