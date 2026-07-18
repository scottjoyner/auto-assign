from __future__ import annotations

import asyncio
import logging
import random
from collections.abc import Mapping, Sequence
from typing import Any

import httpx

from .models import AssignmentCandidate, EventEnvelope, RouterSnapshot
from .settings import Settings
from .tracing_utils import inject_trace_headers

logger = logging.getLogger(__name__)


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
        self._auth = (settings.assistx_auth_user, settings.assistx_auth_pass) if settings.assistx_auth_user and settings.assistx_auth_pass else None
        self._client = httpx.AsyncClient(timeout=self.timeout)

    async def close(self):
        await self._client.aclose()

    def _default_headers(self) -> dict[str, str]:
        return inject_trace_headers()

    async def health(self) -> dict[str, Any]:
        try:
            response = await self._client.get(f"{self.base_url}/health", headers=self._default_headers(), auth=self._auth)
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
            response = await self._client.get(
                url,
                params={"limit": limit, "queue": "backlog", "dry_run": "true"},
                headers=self._default_headers(),
                auth=self._auth,
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
                response = await self._client.get(f"{self.base_url}{path}", headers=self._default_headers(), auth=self._auth)
                if response.is_success:
                    return self._candidate_from_item(response.json())
            except Exception as exc:
                logger.debug("get_task %s failed on %s: %s", task_id, path, exc)
                continue
        return None

    async def event_status(self, idempotency_key: str) -> dict[str, Any]:
        url = f"{self.base_url}/api/events/status"
        try:
            response = await self._client.get(url, params={"idempotency_key": idempotency_key}, headers=self._default_headers(), auth=self._auth)
            if response.is_success:
                return response.json()
        except Exception:
            pass
        return {"known": False}

    async def _request_with_retry(
        self, method: str, url: str, max_retries: int = 3, **kwargs: Any
    ) -> httpx.Response:
        last_exc: Exception | None = None
        headers = dict(kwargs.pop("headers", {}))
        headers.update(self._default_headers())
        for attempt in range(max_retries + 1):
            try:
                response = await self._client.request(method, url, headers=headers, auth=self._auth, **kwargs)
                if response.is_success or response.status_code in (409, 404):
                    return response
                if response.status_code < 500:
                    return response
            except (httpx.ConnectError, httpx.TimeoutException, httpx.RemoteProtocolError) as exc:
                last_exc = exc
                if attempt == max_retries:
                    raise
            if attempt < max_retries:
                delay = (2 ** attempt) + random.uniform(0, 1)
                await asyncio.sleep(delay)
        raise last_exc  # type: ignore[misc]

    async def post_event(self, event: EventEnvelope, dry_run: bool = True) -> dict[str, Any]:
        if dry_run:
            return {"delivered": False, "dry_run": True}
        url = f"{self.base_url}/api/events"
        try:
            response = await self._request_with_retry(
                "POST", url, json=event.model_dump(mode="json")
            )
            return {"delivered": response.is_success or response.status_code == 409, "status_code": response.status_code}
        except Exception as exc:
            logger.warning("post_event failed after retries: %s", exc)
            return {"delivered": False, "error": str(exc)}

    async def create_task(
        self,
        task_id: str,
        title: str,
        required_capabilities: list[str],
        payload: dict[str, Any] | None = None,
        priority: str = "background",
        correlation_id: str | None = None,
    ) -> dict[str, Any]:
        """Materialize an executable :Task in AssistX from a recommended decision.

        auto-assign's scheduler only scores + emits decision *events*; this
        closes the loop by creating the actual Task node a fleet worker can
        claim + execute. Idempotent on task_id.
        """
        url = f"{self.base_url}/api/tasks"
        body: dict[str, Any] = {
            "task_id": task_id,
            "title": title,
            "task_type": "swarm_task",
            "status": "READY",
            "required_capabilities": required_capabilities,
            "priority": priority,
            "payload": payload or {},
        }
        if correlation_id:
            body["correlation_id"] = correlation_id
        try:
            response = await self._request_with_retry("POST", url, json=body)
            if response.status_code in (200, 201, 409):
                return {"created": response.status_code != 409, "status_code": response.status_code}
            return {"created": False, "status_code": response.status_code}
        except Exception as exc:
            logger.warning("create_task failed for %s: %s", task_id, exc)
            return {"created": False, "error": str(exc)}

    async def enqueue_task(
        self, task_id: str, dry_run: bool = False
    ) -> dict[str, Any]:
        """Push a READY task into the RQ execution queue so a fleet worker can
        claim + run it. Without this step the task sits in Neo4j forever (the
        rq worker is a passive consumer and only runs jobs already enqueued).

        This is the missing link in the dispatch chain: ``create_task``
        materializes the :Task node but nothing enqueues it, so the swarm
        never executes. Call this immediately after a successful create_task.
        """
        if dry_run:
            return {"enqueued": False, "dry_run": True}
        url = f"{self.base_url}/api/tasks/{task_id}/enqueue"
        try:
            response = await self._request_with_retry("POST", url)
            if response.status_code in (200, 201, 409):
                return {"enqueued": response.status_code != 409, "status_code": response.status_code}
            return {"enqueued": False, "status_code": response.status_code}
        except Exception as exc:
            logger.warning("enqueue_task failed for %s: %s", task_id, exc)
            return {"enqueued": False, "error": str(exc)}

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
        self._client = httpx.AsyncClient(timeout=self.timeout)

    async def close(self):
        await self._client.aclose()

    def _default_headers(self) -> dict[str, str]:
        return inject_trace_headers()

    async def health(self) -> dict[str, Any]:
        try:
            response = await self._client.get(f"{self.base_url}/health", headers=self._default_headers())
            return {"reachable": response.is_success, "status_code": response.status_code}
        except Exception as exc:
            return {"reachable": False, "error": str(exc)}

    async def snapshot(self) -> RouterSnapshot:
        snapshot = RouterSnapshot(reachable=False)
        try:
            hdrs = self._default_headers()
            context_resp, quota_resp, circuits_resp, clis_resp, ops_resp = await asyncio.gather(
                self._client.get(f"{self.base_url}/admin/context", headers=hdrs),
                self._client.get(f"{self.base_url}/admin/quota", headers=hdrs),
                self._client.get(f"{self.base_url}/admin/circuits", headers=hdrs),
                self._client.get(f"{self.base_url}/admin/agent-clis", headers=hdrs),
                self._client.get(f"{self.base_url}/admin/ops/summary", headers=hdrs),
            )

            self._apply_snapshot_payloads(
                snapshot=snapshot,
                context_payload=context_resp.json() if context_resp.is_success else {},
                quota_payload=quota_resp.json() if quota_resp.is_success else {},
                circuits_payload=circuits_resp.json() if circuits_resp.is_success else {},
                clis_payload=clis_resp.json() if clis_resp.is_success else {},
                ops_payload=ops_resp.json() if ops_resp.is_success else {},
                reachable=context_resp.is_success,
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
