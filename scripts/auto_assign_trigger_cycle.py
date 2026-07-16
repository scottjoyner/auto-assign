#!/usr/bin/env python3
from __future__ import annotations

import fcntl
import json
import os
import sys
from pathlib import Path
from urllib.error import HTTPError, URLError
from urllib.request import Request, urlopen

BASE_URL = os.getenv("AUTO_ASSIGN_BASE_URL", "http://127.0.0.1:8090").rstrip("/")
TIMEOUT_SECONDS = float(os.getenv("AUTO_ASSIGN_TRIGGER_TIMEOUT_SECONDS", "10"))
LOCKFILE = Path(os.getenv("AUTO_ASSIGN_TRIGGER_LOCKFILE", "/tmp/auto_assign_trigger_cycle.lock"))
PROCESS_LIMIT = int(os.getenv("AUTO_ASSIGN_TRIGGER_PROCESS_LIMIT", "25"))
TICK_LIMIT = int(os.getenv("AUTO_ASSIGN_TRIGGER_TICK_LIMIT", "25"))
OUTBOX_LIMIT = int(os.getenv("AUTO_ASSIGN_TRIGGER_OUTBOX_LIMIT", "50"))


def request_json(method: str, path: str, payload: dict | None = None) -> dict:
    headers = {}
    data = None
    if payload is not None:
        data = json.dumps(payload).encode("utf-8")
        headers["Content-Type"] = "application/json"
    req = Request(f"{BASE_URL}{path}", data=data, method=method, headers=headers)
    with urlopen(req, timeout=TIMEOUT_SECONDS) as resp:
        body = resp.read().decode("utf-8").strip()
        return json.loads(body) if body else {}


def load_health() -> dict:
    return request_json("GET", "/health")


def main() -> int:
    LOCKFILE.parent.mkdir(parents=True, exist_ok=True)
    with LOCKFILE.open("w") as lock:
        try:
            fcntl.flock(lock, fcntl.LOCK_EX | fcntl.LOCK_NB)
        except BlockingIOError:
            return 0

        try:
            health = load_health()
        except (HTTPError, URLError, TimeoutError, json.JSONDecodeError) as exc:
            print(json.dumps({"status": "error", "stage": "health", "error": str(exc), "base_url": BASE_URL}, sort_keys=True))
            return 1

        if not health.get("ok", False):
            print(json.dumps({"status": "error", "stage": "health", "health": health, "base_url": BASE_URL}, sort_keys=True))
            return 1

        summary: dict[str, object] = {
            "status": "ok",
            "base_url": BASE_URL,
            "health": {
                "service": health.get("service"),
                "status": health.get("status"),
                "deps": health.get("deps", {}),
            },
        }

        try:
            expired = request_json("POST", "/api/assignments/expire-stale")
            processed = request_json(
                "POST",
                f"/api/events/process?dry_run=false&include_processed=false&limit={PROCESS_LIMIT}",
            )
            tick = request_json(
                "POST",
                "/api/scheduler/tick",
                payload={
                    "dry_run": False,
                    "limit": TICK_LIMIT,
                    "reason": "watchdog:auto-assign-trigger-cycle",
                    "include_blocked": False,
                },
            )
            outbox = request_json(
                "POST",
                f"/api/outbox/dispatch?dry_run=false&limit={OUTBOX_LIMIT}",
            )
        except (HTTPError, URLError, TimeoutError, json.JSONDecodeError) as exc:
            print(json.dumps({"status": "error", "stage": "cycle", "error": str(exc), "base_url": BASE_URL}, sort_keys=True))
            return 1

        released_expired = int(expired.get("released_expired", 0) or expired.get("expired_count", 0) or expired.get("expired", 0) or 0)
        processed_count = int(processed.get("processed", 0) or 0)
        tick_evaluated = int(tick.get("evaluated", 0) or 0)
        tick_recommended = int(tick.get("recommended", 0) or 0)
        tick_approval_required = int(tick.get("approval_required", 0) or 0)
        tick_skipped = int(tick.get("skipped", 0) or 0)
        delivered = int(outbox.get("delivered", 0) or 0)
        failed = int(outbox.get("failed", 0) or 0)
        considered = int(outbox.get("considered", 0) or 0)

        summary.update(
            {
                "expired": released_expired,
                "processed": processed_count,
                "scheduler": {
                    "scheduler_run_id": tick.get("scheduler_run_id"),
                    "evaluated": tick_evaluated,
                    "recommended": tick_recommended,
                    "approval_required": tick_approval_required,
                    "skipped": tick_skipped,
                    "dry_run": bool(tick.get("dry_run", False)),
                },
                "outbox": {
                    "considered": considered,
                    "delivered": delivered,
                    "failed": failed,
                },
            }
        )

        if any((released_expired, processed_count, tick_evaluated, tick_recommended, tick_approval_required, tick_skipped, delivered, failed)):
            print(json.dumps(summary, sort_keys=True))
        return 0


if __name__ == "__main__":
    raise SystemExit(main())
