from __future__ import annotations

import asyncio
import os
import time
from contextlib import asynccontextmanager
from typing import Annotated

from fastapi import Depends, FastAPI, HTTPException, Query
from fastapi.responses import JSONResponse, PlainTextResponse
from starlette.requests import Request

from . import __version__
from .logging_utils import install_logging_middleware, setup_logging
from .metrics import CONTENT_TYPE_LATEST, OUTBOX_PENDING, STALE_HEARTBEATS, generate_latest

_start_time = time.time()
from .cache import CacheStore
from .clients import AssistXClient, RouterClient
from .control import AssignmentControlStore
from .models import (
    AssignmentApprovalRequest,
    AssignmentClaimRequest,
    AssignmentCompletionRequest,
    AssignmentControlRequest,
    AssignmentEvaluateRequest,
    AssignmentReleaseRequest,
    EventEnvelope,
    HealthResponse,
    HeartbeatRequest,
    SchedulerTickRequest,
)
from .scorer import AssignmentScorer
from .service import AssignmentService
from .settings import Settings, get_settings


def build_service(settings: Settings | None = None) -> AssignmentService:
    settings = settings or get_settings()
    cache = CacheStore(settings.sqlite_path)
    assistx = AssistXClient(settings)
    router = RouterClient(settings)
    scorer = AssignmentScorer(settings)
    return AssignmentService(settings, cache, assistx, router, scorer)


@asynccontextmanager
async def lifespan(app: FastAPI):
    # W-40: schema-init / migration failure must degrade gracefully rather than
    # crash import/startup. Build defensively so /health still answers.
    if not hasattr(app.state, "assignment_service"):
        try:
            app.state.assignment_service = build_service()
        except Exception as exc:  # pragma: no cover - defensive
            logger.exception("Failed to build assignment service; starting degraded: %s", exc)
            app.state.assignment_service = None
    svc = getattr(app.state, "assignment_service", None)
    tick_task = None
    if svc is not None and get_settings().scheduler_enabled:
        async def _scheduler_loop() -> None:
            # Autonomous tick loop: periodically invokes the same scheduler_tick
            # path an operator would call manually, in non-dry-run mode, so the
            # fleet self-assigns backlog work without external cron. After scoring,
            # the decision events are flushed to AssistX (dispatch_outbox) so
            # assignments actually materialise in the canonical graph.
            await asyncio.sleep(2)
            while True:
                try:
                    await svc.scheduler_tick(
                        SchedulerTickRequest(dry_run=False, reason="autonomous_scheduler_loop")
                    )
                    if get_settings().dispatch_enabled:
                        await svc.dispatch_outbox(dry_run=False)
                        # Close the loop: create the actual executable :Task a
                        # fleet worker can claim, from recommended decisions.
                        await svc.dispatch_tasks(dry_run=False)
                except Exception as exc:  # pragma: no cover - defensive
                    logger.warning("autonomous scheduler tick failed: %s", exc)
                await asyncio.sleep(get_settings().tick_interval_seconds)

        tick_task = asyncio.create_task(_scheduler_loop())
    yield
    if tick_task is not None:
        tick_task.cancel()
    svc = getattr(app.state, "assignment_service", None)
    if svc:
        await svc.assistx.close()
        await svc.router.close()
        svc.cache.close()


app = FastAPI(
    title="auto-assign",
    version=__version__,
    description="Assignment, trigger, and heartbeat layer for AssistX.",
    lifespan=lifespan,
)
setup_logging()
install_logging_middleware(app)

from fastapi.middleware.cors import CORSMiddleware
app.add_middleware(
    CORSMiddleware,
    allow_origins=os.getenv("CORS_ALLOW_ORIGINS", "http://localhost:8080,http://localhost:8090").split(","),
    allow_credentials=True,
    allow_methods=["*"],
    allow_headers=["*"],
)

import base64
import logging

logger = logging.getLogger(__name__)

# Paths that are always open (health probe + metrics scrape).
_PUBLIC_PATHS = {"/health", "/metrics"}


@app.middleware("http")
async def api_auth_middleware(request: Request, call_next):
    """Enforce a shared-secret on every route except /health and /metrics.

    Auth is satisfied by either ``Authorization: Bearer <token>`` or HTTP Basic
    auth whose password equals the configured ``AUTO_ASSIGN_API_TOKEN``. When no
    token is configured the check is skipped (local-dev only) with a warning.
    """
    settings = None
    svc = getattr(request.app.state, "assignment_service", None)
    if svc is not None:
        settings = svc.settings
    if settings is None:
        settings = get_settings()
    if request.url.path in _PUBLIC_PATHS:
        return await call_next(request)
    if not settings.api_token:
        logger.warning("API auth disabled: AUTO_ASSIGN_API_TOKEN is not set")
        return await call_next(request)

    auth_header = request.headers.get("Authorization", "")
    expected = settings.api_token
    authorized = False
    if auth_header.startswith("Bearer "):
        authorized = auth_header[len("Bearer "):].strip() == expected
    elif auth_header.startswith("Basic "):
        try:
            decoded = base64.b64decode(auth_header[len("Basic "):].strip()).decode("utf-8")
            _user, _, pwd = decoded.partition(":")
            authorized = pwd == expected
        except Exception:
            authorized = False
    if not authorized:
        return JSONResponse(status_code=401, content={"detail": "unauthorized"})
    return await call_next(request)


def get_assignment_service() -> AssignmentService:
    return app.state.assignment_service


def get_assignment_control(service: AssignmentService) -> AssignmentControlStore:
    if not hasattr(service, "_control_store"):
        service._control_store = AssignmentControlStore(service.settings.sqlite_path)
    return service._control_store


@app.get("/health", response_model=HealthResponse)
async def health(service: Annotated[AssignmentService, Depends(get_assignment_service)]):
    assistx = await service.assistx.health()
    router = await service.router.health()
    cache = service.cache.health()
    control = get_assignment_control(service).get()
    deps_ok = cache.get("reachable") and assistx.get("reachable")
    status = "ok" if deps_ok else "degraded"

    outbox = cache.outbox_summary() if hasattr(cache, "outbox_summary") else {}
    OUTBOX_PENDING.set(int(outbox.get("pending", 0)))
    stale = service.list_stale_heartbeats(limit=1)
    STALE_HEARTBEATS.set(stale.get("count", 0))

    return HealthResponse(
        ok=deps_ok,
        status=status,
        version=__version__,
        uptime=time.time() - _start_time,
        deps={
            "assistx": assistx,
            "router": router,
            "cache": cache,
        },
        scheduler={
            "enabled": service.settings.scheduler_enabled,
            "last_tick_at": service.last_tick_at,
            "dispatch_enabled": service.settings.dispatch_enabled,
            "direct_workers_enabled": service.settings.direct_workers_enabled,
            "control": control,
        },
    )


@app.get("/metrics", response_class=PlainTextResponse)
async def metrics():
    return PlainTextResponse(generate_latest(), media_type=CONTENT_TYPE_LATEST)


@app.get("/api/assignment-control")
async def read_assignment_control(service: Annotated[AssignmentService, Depends(get_assignment_service)]):
    return get_assignment_control(service).get()


@app.post("/api/assignment-control")
async def set_assignment_control(
    request: AssignmentControlRequest,
    service: Annotated[AssignmentService, Depends(get_assignment_service)],
):
    state, event = get_assignment_control(service).set(request)
    service.cache.enqueue_event(event)
    return {
        **state,
        "event_id": event.event_id,
        "idempotency_key": event.idempotency_key,
        "dry_run": request.dry_run,
        "cache_role": "assignment_control_cache_and_outbox",
        "canonical_source": "neo4j_via_assistx",
    }


@app.post("/api/events")
async def ingest_event(
    request: EventEnvelope,
    service: Annotated[AssignmentService, Depends(get_assignment_service)],
):
    return service.ingest_event(request)


@app.get("/api/events")
async def list_inbound_events(
    service: Annotated[AssignmentService, Depends(get_assignment_service)],
    limit: int = Query(default=50, ge=1, le=500),
    event_type: str | None = Query(default=None),
):
    return {
        "source": "sqlite_inbound_event_cache",
        "canonical_source": "neo4j_via_assistx",
        "events": service.list_inbound_events(limit=limit, event_type=event_type),
    }


@app.get("/api/events/processing")
async def list_inbound_processing(
    service: Annotated[AssignmentService, Depends(get_assignment_service)],
    limit: int = Query(default=50, ge=1, le=500),
):
    return {
        "source": "sqlite_inbound_processing_cache",
        "canonical_source": "neo4j_via_assistx",
        "processing": service.list_inbound_processing(limit=limit),
    }


@app.post("/api/events/process")
async def process_inbound_events(
    service: Annotated[AssignmentService, Depends(get_assignment_service)],
    event_type: str | None = Query(default=None),
    dry_run: bool = Query(default=True),
    include_processed: bool = Query(default=False),
    limit: int = Query(default=25, ge=1, le=500),
):
    return await service.process_inbound_events(
        event_type=event_type,
        dry_run=dry_run,
        include_processed=include_processed,
        limit=limit,
    )


@app.post("/api/assignments/evaluate")
async def evaluate_assignment(
    request: AssignmentEvaluateRequest,
    service: Annotated[AssignmentService, Depends(get_assignment_service)],
):
    return await service.evaluate(request)


@app.post("/api/scheduler/tick")
async def scheduler_tick(
    request: SchedulerTickRequest,
    service: Annotated[AssignmentService, Depends(get_assignment_service)],
):
    return await service.scheduler_tick(request)


@app.get("/api/scheduler/runs")
async def list_scheduler_runs(
    service: Annotated[AssignmentService, Depends(get_assignment_service)],
    limit: int = Query(default=50, ge=1, le=500),
):
    return {
        "source": "sqlite_cache_mirror",
        "canonical_source": "neo4j_via_assistx",
        "runs": service.list_scheduler_runs(limit=limit),
    }


@app.get("/api/assignments")
async def list_assignments(
    service: Annotated[AssignmentService, Depends(get_assignment_service)],
    limit: int = Query(default=50, ge=1, le=500),
):
    return {
        "source": "sqlite_cache_mirror",
        "canonical_source": "neo4j_via_assistx",
        "assignments": service.list_assignments(limit=limit),
    }


@app.get("/api/assignments/summary")
async def assignment_summary(
    service: Annotated[AssignmentService, Depends(get_assignment_service)],
    limit: int = Query(default=10, ge=1, le=100),
):
    ops = service.ops_summary()
    stale = service.list_stale_heartbeats(limit=limit)
    recent_assignments = service.list_assignments(limit=limit)
    control = get_assignment_control(service).get()
    recommendations = _assignment_summary_recommendations(
        ops=ops,
        stale_count=stale.get("count", 0),
        dispatch_enabled=service.settings.dispatch_enabled,
        direct_workers_enabled=service.settings.direct_workers_enabled,
        control=control,
    )
    return {
        "source": "sqlite_cache_mirror",
        "canonical_source": "neo4j_via_assistx",
        "cache_role": "assignment_governor_read_model",
        "control": control,
        "assignments_by_status": ops.get("assignments_by_status", {}),
        "outbox_by_status": ops.get("outbox_by_status", {}),
        "inbound_by_type": ops.get("inbound_by_type", {}),
        "heartbeats": {
            **ops.get("heartbeats", {}),
            "stale_count": stale.get("count", 0),
            "stale_after_seconds": stale.get("stale_after_seconds"),
        },
        "scheduler": ops.get("scheduler", {}),
        "safety": {
            "dispatch_enabled": service.settings.dispatch_enabled,
            "direct_workers_enabled": service.settings.direct_workers_enabled,
            "cache_is_canonical": False,
            "canonical_source": "neo4j_via_assistx",
            "control_mode": control.get("mode"),
            "assignment_allowed": control.get("assignment_allowed"),
            "scheduler_ticks_allowed": control.get("scheduler_ticks_allowed"),
        },
        "recent_assignments": recent_assignments,
        "stale_heartbeats": stale.get("heartbeats", []),
        "recommendations": recommendations,
    }


def _assignment_summary_recommendations(
    ops: dict,
    stale_count: int,
    dispatch_enabled: bool,
    direct_workers_enabled: bool,
    control: dict | None = None,
) -> list[dict]:
    recommendations: list[dict] = []
    outbox = ops.get("outbox_by_status", {}) or {}
    assignments = ops.get("assignments_by_status", {}) or {}
    control = control or {"mode": "enabled", "new_assignments_allowed": True, "scheduler_ticks_allowed": True}
    mode = control.get("mode") or "enabled"
    pending = int(outbox.get("pending", 0))
    failed = int(outbox.get("failed", 0))
    dead_letter = int(outbox.get("dead_letter", 0))
    if mode in {"paused", "maintenance"}:
        recommendations.append(
            {
                "level": "warning",
                "action": "keep_assignment_governor_paused",
                "reason": f"assignment control is {mode}; new assignment decisions and scheduler ticks should pause",
            }
        )
    elif mode == "draining":
        recommendations.append(
            {
                "level": "warning",
                "action": "drain_assignment_governor",
                "reason": "assignment control is draining; finish/reconcile existing work and avoid new recommendations",
            }
        )
    if pending:
        recommendations.append(
            {
                "level": "info",
                "action": "dispatch_or_reconcile_outbox",
                "reason": "pending assign.* events are waiting for AssistX/Neo4j materialization",
            }
        )
    if failed or dead_letter:
        recommendations.append(
            {
                "level": "warning",
                "action": "inspect_outbox_failures",
                "reason": "failed or dead-lettered outbox events require operator review",
            }
        )
    if stale_count:
        recommendations.append(
            {
                "level": "warning",
                "action": "inspect_stale_heartbeats",
                "reason": "worker/node heartbeats are stale and should not receive new assignments",
            }
        )
    if assignments.get("approval_required"):
        recommendations.append(
            {
                "level": "info",
                "action": "review_required_approvals",
                "reason": "some assignments are blocked until AssistX/Neo4j approval exists",
            }
        )
    if not dispatch_enabled:
        recommendations.append(
            {
                "level": "info",
                "action": "dry_run_only",
                "reason": "dispatch is disabled; auto-assign is operating as a dry-run governor",
            }
        )
    if not direct_workers_enabled:
        recommendations.append(
            {
                "level": "info",
                "action": "direct_workers_disabled",
                "reason": "direct worker lane is disabled until sandbox/approval/artifact controls are implemented",
            }
        )
    if not recommendations:
        recommendations.append(
            {
                "level": "info",
                "action": "steady_state",
                "reason": "no local cache/outbox/heartbeat/control risks detected",
            }
        )
    return recommendations


@app.get("/api/assignments/{assignment_id}")
async def get_assignment(
    assignment_id: str,
    service: Annotated[AssignmentService, Depends(get_assignment_service)],
):
    assignment = service.get_assignment(assignment_id)
    if assignment is None:
        raise HTTPException(status_code=404, detail="assignment not found in local cache mirror")
    return {
        "source": "sqlite_cache_mirror",
        "canonical_source": "neo4j_via_assistx",
        "assignment": assignment,
    }


@app.post("/api/assignments/{assignment_id}/approve")
async def approve_assignment(
    assignment_id: str,
    request: AssignmentApprovalRequest,
    service: Annotated[AssignmentService, Depends(get_assignment_service)],
):
    result = await service.approve_assignment(assignment_id, request)
    if not result.get("accepted"):
        raise HTTPException(status_code=404, detail=result)
    return result


@app.post("/api/assignments/{assignment_id}/release")
async def release_assignment(
    assignment_id: str,
    request: AssignmentReleaseRequest,
    service: Annotated[AssignmentService, Depends(get_assignment_service)],
):
    result = await service.release_assignment(assignment_id, request)
    if not result.get("accepted"):
        raise HTTPException(status_code=404, detail=result)
    return result


@app.post("/api/assignments/{assignment_id}/claim")
async def claim_assignment(
    assignment_id: str,
    request: AssignmentClaimRequest,
    service: Annotated[AssignmentService, Depends(get_assignment_service)],
):
    result = await service.claim_assignment(assignment_id, request)
    if not result.get("accepted"):
        raise HTTPException(status_code=400, detail=result)
    return result


@app.post("/api/assignments/{assignment_id}/complete")
async def complete_assignment(
    assignment_id: str,
    request: AssignmentCompletionRequest,
    service: Annotated[AssignmentService, Depends(get_assignment_service)],
):
    result = await service.complete_assignment(assignment_id, request)
    if not result.get("accepted"):
        raise HTTPException(status_code=400, detail=result)
    return result


@app.post("/api/assignments/expire-stale")
async def expire_stale_leases(
    service: Annotated[AssignmentService, Depends(get_assignment_service)],
):
    return service.expire_stale_leases()


@app.post("/api/heartbeats")
async def record_heartbeat(
    request: HeartbeatRequest,
    service: Annotated[AssignmentService, Depends(get_assignment_service)],
):
    event = service.record_heartbeat_with_lease_renewal(request)
    return {
        "accepted": True,
        "event_id": event.event_id,
        "idempotency_key": event.idempotency_key,
        "canonical_source": "neo4j_via_assistx",
        "cache_role": "outbox_replay_buffer",
    }


@app.get("/api/heartbeats")
async def list_heartbeats(
    service: Annotated[AssignmentService, Depends(get_assignment_service)],
    limit: int = Query(default=50, ge=1, le=500),
):
    return {
        "source": "sqlite_cache_mirror",
        "canonical_source": "neo4j_via_assistx",
        "heartbeats": service.list_heartbeats(limit=limit),
    }


@app.get("/api/heartbeats/stale")
async def list_stale_heartbeats(
    service: Annotated[AssignmentService, Depends(get_assignment_service)],
    stale_after_seconds: int | None = Query(default=None, ge=1),
    limit: int = Query(default=50, ge=1, le=500),
):
    return service.list_stale_heartbeats(stale_after_seconds=stale_after_seconds, limit=limit)


@app.get("/api/outbox/summary")
async def outbox_summary(service: Annotated[AssignmentService, Depends(get_assignment_service)]):
    return {
        "cache_role": "outbox_replay_buffer",
        "canonical_source": "neo4j_via_assistx",
        "summary": service.cache.outbox_summary(),
    }


@app.get("/api/outbox/events")
async def list_outbox_events(
    service: Annotated[AssignmentService, Depends(get_assignment_service)],
    limit: int = Query(default=50, ge=1, le=500),
    status: str | None = Query(default=None),
):
    return {
        "source": "sqlite_cache_mirror",
        "canonical_source": "neo4j_via_assistx",
        "events": service.list_outbox_events(limit=limit, status=status),
    }


@app.post("/api/outbox/dispatch")
async def dispatch_outbox(
    service: Annotated[AssignmentService, Depends(get_assignment_service)],
    request: Request,
    dry_run: bool = Query(default=True),
    limit: int = Query(default=25, ge=1, le=500),
):
    # Honour dry_run from either the query param or the JSON body, so API
    # callers sending {"dry_run": false} in the body are not silently forced
    # into dry-run (the previous Query-only default dropped body values).
    try:
        body = await request.json()
        if isinstance(body, dict) and "dry_run" in body:
            dry_run = bool(body["dry_run"])
    except Exception:
        pass
    return await service.dispatch_outbox(dry_run=dry_run, limit=limit)





@app.post("/api/outbox/reconcile")
async def reconcile_outbox(
    service: Annotated[AssignmentService, Depends(get_assignment_service)],
    limit: int = Query(default=100, ge=1, le=1000),
):
    return await service.reconcile_outbox(limit=limit)


@app.get("/api/ops/summary")
async def ops_summary(service: Annotated[AssignmentService, Depends(get_assignment_service)]):
    ops = service.ops_summary()
    ops["control"] = get_assignment_control(service).get()
    return ops
