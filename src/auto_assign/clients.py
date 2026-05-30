from __future__ import annotations

from collections.abc import Mapping, Sequence
from typing import Any

import httpx

from .models import AssignmentCandidate, EventEnvelope, RouterSnapshot
from .settings import Settings


class AssistXClient:
    """Client for AssistX, the canonical task/policy/Neo4j boundary.

    Expected AssistX endpoints:
    - GET  /health
    - GET  /api/router/backlog-candidates
    - GET  /api/tasks/{task_id} or /api/router/tasks/{task_id}
    - GET  /api/events/status?idempotency_key=...
    - POST /api/events

    Missing optional endpoints degrade conservatively. AssistX/Neo4j remains the
    source of truth whenever it can answer.
    """

    def __init__(self, settings: Settings):
        self.base_url = settings.assistx_base_url.rstrip("/")
        self.timeout = settings.assistx_timeout_seconds

    async def health(self) -> dict[str, Any]:
        try:
            async with httpx.AsyncClient(timeout=self.timeout) as client:
                response = await client.get(f"{self.base_url}/health")
            return {
                "reachable": response.is_success,
                "status_code": response.status_code,
                "brain": "neo4j_via_assistx",
            }
        except Exception as exc:
            return {"reachable": False, "error": str(exc), "brain": "neo4j_via_assistx"}

    async def get_backlog_candidates(self, limit: int = 25) -> list[AssignmentCandidate]:
        url = f"{self.base_url}/api/router/backlog-candidates"
        try:
            async with httpx.AsyncClient(timeout=self.timeout) as client:
                response = await client.get(
                    url,
                    params={"limit": limit, "queue": "backlog", "dry_run": "true"},
                )
            response.raise_for_status()
            payload = response.json()
            items = self._extract_items(payload)
            return [self._candidate_from_item(item) for item in items]
        except Exception:
            return []

    async def get_task(self, task_id: str) -> AssignmentCandidate | None:
        for path in (f"/api/tasks/{task_id}", f"/api/router/tasks/{task_id}"):
            try:
                async with httpx.AsyncClient(timeout=self.timeout) as client:
                    response = await client.get(f"{self.base_url}{path}")
                if response.is_success:
                    return self._candidate_from_item(response.json())
            except Exception:
                continue
        return None

    async def event_status(self, idempotency_key: str) -> dict[str, Any]:
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
        return {"delivered": response.is_success or response.status_code == 409, "status_code": response.status_code}

    def _extract_items(self, payload: Any) -> list[dict[str, Any]]:
        if isinstance(payload, list):
            return [item for item in payload if isinstance(item, dict)]
        if not isinstance(payload, dict):
            return []
        for key in ("tasks", "candidates", "items", "results", "backlog"):
            value = payload.get(key)
            if isinstance(value, list):
                return [item for item in value if isinstance(item, dict)]
        return []

    def _candidate_from_item(self, item: dict[str, Any]) -> AssignmentCandidate:
        raw = dict(item)
        if "task_id" not in raw:
            raw["task_id"] = raw.get("id") or raw.get("uuid") or raw.get("title") or "unknown-task"

        metadata = raw.get("metadata") if isinstance(raw.get("metadata"), dict) else {}
        labels = self._label_set(raw.get("privacy_labels"))

        privacy = str(raw.get("privacy") or raw.get("privacy_label") or "").strip().lower()
        if privacy:
            labels.add(privacy)

        meta_privacy = str(metadata.get("privacy") or "").strip().lower()
        if meta_privacy:
            labels.add(meta_privacy)

        sensitive_labels = {
            "local_only",
            "private",
            "private_data",
            "secret",
            "secrets",
            "voice_auth",
            "enrollment",
            "enrollment_sample",
        }
        raw["privacy_labels"] = sorted(labels)
        raw["local_only"] = self._as_bool(raw.get("local_only")) or bool(
            labels & {"local_only", "private", "secret"}
        )
        raw["sensitive"] = self._as_bool(raw.get("sensitive")) or bool(labels & sensitive_labels)
        allow_cloud_default = not raw["local_only"]
        raw["allow_cloud"] = self._as_bool(raw.get("allow_cloud"), default=allow_cloud_default) and not raw[
            "local_only"
        ]
        raw.setdefault("summary", raw.get("title") or raw.get("prompt"))
        return AssignmentCandidate.model_validate(raw)

    def _label_set(self, value: Any) -> set[str]:
        if value is None:
            return set()
        if isinstance(value, str):
            raw_values = value.replace(";", ",").split(",")
        elif isinstance(value, list | tuple | set):
            raw_values = value
        else:
            raw_values = [value]
        return {str(item).strip().lower() for item in raw_values if str(item).strip()}

    def _as_bool(self, value: Any, default: bool = False) -> bool:
        if value is None:
            return default
        if isinstance(value, bool):
            return value
        if isinstance(value, int | float):
            return bool(value)
        if isinstance(value, str):
            normalized = value.strip().lower()
            if normalized in {"1", "true", "yes", "y", "on"}:
                return True
            if normalized in {"0", "false", "no", "n", "off", ""}:
                return False
        return bool(value)


class RouterClient:
    """Client for auto-router model/service/quota context snapshots.

    Expected auto-router endpoints:
    - GET /health
    - GET /admin/context
    - GET /admin/quota
    - GET /admin/circuits
    - GET /admin/agent-clis
    - GET /admin/ops/summary

    If context cannot be loaded, the snapshot is marked unreachable so hosted and
    router lanes are conservatively blocked by the scorer.
    """

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
                ops_response = await client.get(f"{self.base_url}/admin/ops/summary")

            self._apply_snapshot_payloads(
                snapshot=snapshot,
                context_payload=context_response.json() if context_response.is_success else {},
                quota_payload=quota_response.json() if quota_response.is_success else {},
                circuits_payload=circuits_response.json() if circuits_response.is_success else {},
                clis_payload=clis_response.json() if clis_response.is_success else {},
                ops_payload=ops_response.json() if ops_response.is_success else {},
                reachable=context_response.is_success,
            )
        except Exception:
            snapshot.reachable = False
        return snapshot

    def _apply_snapshot_payloads(
        self,
        snapshot: RouterSnapshot,
        context_payload: Any,
        quota_payload: Any,
        circuits_payload: Any,
        clis_payload: Any,
        ops_payload: Any,
        reachable: bool = True,
    ) -> RouterSnapshot:
        context = self._unwrap_context(context_payload)
        snapshot.reachable = bool(reachable and context)
        snapshot.context_revision = self._context_revision(context)
        snapshot.nodes = self._payload_list(context, ("nodes", "workers", "worker_nodes", "hosts"))
        snapshot.providers = self._payload_list(context, ("providers", "models", "model_providers"))
        snapshot.services = self._payload_list(context, ("services", "endpoints", "routes"))
        snapshot.quota = self._payload_object(quota_payload)
        snapshot.circuits = self._payload_object(circuits_payload)
        snapshot.agent_clis = self._payload_list(clis_payload, ("agents", "agent_clis", "clis", "items", "results"))
        snapshot.ops_summary = self._payload_object(ops_payload)
        return snapshot

    def _unwrap_context(self, payload: Any) -> dict[str, Any]:
        obj = self._payload_object(payload)
        for key in ("context", "snapshot", "router_context", "data"):
            nested = obj.get(key)
            if isinstance(nested, Mapping):
                return dict(nested)
        return obj

    def _context_revision(self, context: dict[str, Any]) -> str | None:
        metadata = context.get("metadata") if isinstance(context.get("metadata"), Mapping) else {}
        return (
            context.get("revision")
            or context.get("context_revision")
            or context.get("version")
            or metadata.get("revision")
            or metadata.get("context_revision")
        )

    def _payload_object(self, payload: Any) -> dict[str, Any]:
        if isinstance(payload, Mapping):
            return dict(payload)
        return {}

    def _payload_list(self, payload: Any, keys: Sequence[str]) -> list[dict[str, Any]]:
        if isinstance(payload, list):
            return [item for item in payload if isinstance(item, dict)]
        if not isinstance(payload, Mapping):
            return []
        for key in keys:
            value = payload.get(key)
            if isinstance(value, list):
                return [item for item in value if isinstance(item, dict)]
            if isinstance(value, Mapping):
                nested = self._payload_list(value, ("items", "results", "data", "nodes", "services"))
                if nested:
                    return nested
        return []
