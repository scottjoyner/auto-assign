from __future__ import annotations

from contextlib import asynccontextmanager
from typing import Annotated

from fastapi import Depends, FastAPI, HTTPException, Query

from . import __version__
from .cache import CacheStore
from .clients import AssistXClient, RouterClient
from .models import (
    AssignmentApprovalRequest,
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
    if not hasattr(app.state, "assignment_service"):
        app.state.assignment_service = build_service()
    yield


app = FastAPI(
    title="auto-assign",
    version=__version__,
    description="Assignment, trigger, and heartbeat layer for AssistX.",
    lifespan=lifespan,
)


def get_assignment_service() -> AssignmentService:
    return app.state.assignment_service


@app.get("/health", response_model=HealthResponse)
async def health(service: Annotated[AssignmentService, Depends(get_assignment_service)]):
    assistx = await service.assistx.health()
    router = await service.router.health()
    cache = service.cache.health()
    status = "ok" if cache.get("reachable") else "degraded"
    if not assistx.get("reachable"):
        status = "degraded"
    return HealthResponse(
        status=status,
        version=__version__,
        assistx=assistx,
        router=router,
        cache=cache,
        scheduler={
            "enabled": service.settings.scheduler_enabled,
            "last_tick_at": service.last_tick_at,
            "dispatch_enabled": service.settings.dispatch_enabled,
            "direct_workers_enabled": service.settings.direct_workers_enabled,
        },
    )


@app.post("/api/events")
async def ingest_event(
    request: EventEnvelope,
    service: Annotated[AssignmentService, Depends(get_assignment_service)],
):
    return service.ingest_event(request)


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


@app.post("/api/heartbeats")
async def record_heartbeat(
    request: HeartbeatRequest,
    service: Annotated[AssignmentService, Depends(get_assignment_service)],
):
    event = service.record_heartbeat(request)
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
    dry_run: bool = Query(default=True),
    limit: int = Query(default=25, ge=1, le=500),
):
    return await service.dispatch_outbox(dry_run=dry_run, limit=limit)


@app.post("/api/outbox/reconcile")
async def reconcile_outbox(
    service: Annotated[AssignmentService, Depends(get_assignment_service)],
    limit: int = Query(default=100, ge=1, le=1000),
):
    return await service.reconcile_outbox(limit=limit)


@app.get("/api/ops/summary")
async def ops_summary(service: Annotated[AssignmentService, Depends(get_assignment_service)]):
    return service.ops_summary()
