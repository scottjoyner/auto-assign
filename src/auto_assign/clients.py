from __future__ import annotations

from typing import Any

import httpx

from .models import AssignmentCandidate, EventEnvelope, RouterSnapshot
from .settings import Settings


class AssistXClient:
    def __init__(self, settings: Settings):
        self.base_url = settings.assistx_base_url.rstrip("/")
        self.timeout = settings.assistx_timeout_seconds

    async def health(self) -> dict[str, Any]:
        try:
            async with httpx.AsyncClient(timeout=self.timeout) as client:
                response = await client.get(f"{self.base_url}/health")
            return {"reachable": response.is_success, "status_code": response.status_code, "brain": "neo4j_via_assistx"}
        except Exception as exc:
            return {"reachable": False, "error": str(exc), "brain": "neo4j_via_assistx"}

    async def get_backlog_candidates(self, limit: int = 25) -> list[AssignmentCandidate]:
        url = f"{self.base_url}/api/router/backlog-candidates"
        try:
            async with httpx.AsyncClient(timeout=self.timeout) as client:
                response = await client.get(url, params={"limit": limit})
            response.raise_for_status()
            payload = response.json()
            items = payload.get("candidates", payload if isinstance(payload, list) else [])
            return [AssignmentCandidate.model_validate(item) for item in items]
        except Exception:
            return []

    async def get_task(self, task_id: str) -> AssignmentCandidate | None:
        for path in (f"/api/tasks/{task_id}", f"/api/router/tasks/{task_id}"):
            try:
                async with httpx.AsyncClient(timeout=self.timeout) as client:
                    response = await client.get(f"{self.base_url}{path}")
                if response.is_success:
                    return AssignmentCandidate.model_validate(response.json())
            except Exception:
                continue
        return None

    async def event_status(self, idempotency_key: str) -> dict[str, Any]:
        # Optional future AssistX endpoint. Missing support must fail open for dry-run but not
        # override canonical graph state when AssistX can answer.
        url = f"{self.base_url}/api/events/status"
        try:
            async with httpx.AsyncClient(timeout=self.timeout) as client:
                response = await client.get(url, params={"idempotency_key": idempotency_key})
            if response.is_success:
                return response.json()
        except Exception:
            pass
        return {"known": False}

    async def post_event(self, event: EventEnvelope, dry_run: bool = True) -> dict[str, Any]:
        if dry_run:
            return {"delivered": False, "dry_run": True}
        url = f"{self.base_url}/api/events"
        async with httpx.AsyncClient(timeout=self.timeout) as client:
            response = await client.post(url, json=event.model_dump(mode="json"))
        return {"delivered": response.is_success, "status_code": response.status_code}


class RouterClient:
    def __init__(self, settings: Settings):
        self.base_url = settings.router_base_url.rstrip("/")
        self.timeout = settings.router_timeout_seconds

    async def health(self) -> dict[str, Any]:
        try:
            async with httpx.AsyncClient(timeout=self.timeout) as client:
                response = await client.get(f"{self.base_url}/health")
            return {"reachable": response.is_success, "status_code": response.status_code}
        except Exception as exc:
            return {"reachable": False, "error": str(exc)}

    async def snapshot(self) -> RouterSnapshot:
        snapshot = RouterSnapshot(reachable=False)
        try:
            async with httpx.AsyncClient(timeout=self.timeout) as client:
                context_response = await client.get(f"{self.base_url}/admin/context")
                quota_response = await client.get(f"{self.base_url}/admin/quota")
                circuits_response = await client.get(f"{self.base_url}/admin/circuits")
                clis_response = await client.get(f"{self.base_url}/admin/agent-clis")

            context = context_response.json() if context_response.is_success else {}
            snapshot.reachable = context_response.is_success
            snapshot.context_revision = context.get("revision") or context.get("context_revision")
            snapshot.nodes = context.get("nodes", [])
            snapshot.providers = context.get("providers", [])
            snapshot.services = context.get("services", [])
            snapshot.quota = quota_response.json() if quota_response.is_success else {}
            snapshot.circuits = circuits_response.json() if circuits_response.is_success else {}
            cli_payload = clis_response.json() if clis_response.is_success else {}
            snapshot.agent_clis = cli_payload.get("agents", cli_payload.get("agent_clis", [])) if isinstance(cli_payload, dict) else []
        except Exception:
            # Conservative fallback: no cloud/free lanes should be selected based on a stale router.
            snapshot.reachable = False
        return snapshot
