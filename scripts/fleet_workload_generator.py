#!/usr/bin/env python3
"""Fleet workload generator.

Creates real ``caps=['llm']`` tasks against the assistx API so the GPU fleet
executor has sustained work to route across live nodes. Each task carries a
``prompt`` (or ``messages``) and an optional ``model`` hint; the fleet executor
claims it, sends it to the best node that has a model loaded, and writes the
model output back into ``result_json``.

Usage:
    python3 fleet_workload_generator.py --count 200 --batch 20 --interval 2
    python3 fleet_workload_generator.py --loop --rate 5   # continuous

Env:
    FLEET_GEN_ASSISTX_URL   (default http://localhost:8000)
    FLEET_GEN_AUTH_USER      (default admin)
    FLEET_GEN_AUTH_PASS      (default from AUTO_ASSIGN_ASSISTX_AUTH_PASS or gluhlaf8)
"""
from __future__ import annotations

import argparse
import base64
import json
import os
import time
import urllib.error
import urllib.request
from dataclasses import dataclass, field

BASE_URL = os.getenv("FLEET_GEN_ASSISTX_URL", os.getenv("AUTO_ASSIGN_ASSISTX_BASE_URL", "http://localhost:8000")).rstrip("/")
AUTH_USER = os.getenv("FLEET_GEN_AUTH_USER", os.getenv("AUTO_ASSIGN_ASSISTX_AUTH_USER", "admin"))
AUTH_PASS = os.getenv("FLEET_GEN_AUTH_PASS", os.getenv("AUTO_ASSIGN_ASSISTX_AUTH_PASS", "gluhlaf8"))
HTTP_TIMEOUT = float(os.getenv("FLEET_GEN_TIMEOUT", "30"))

PROMPTS = [
    "Explain the difference between a semaphore and a mutex in one paragraph.",
    "Summarize the trade-offs of MoE vs dense transformer architectures.",
    "Write a short haiku about distributed inference across GPU nodes.",
    "What is tail latency and why does it matter for LLM serving?",
    "List three failure modes of round-robin load balancing.",
    "In one paragraph, describe how KV-cache paging improves throughput.",
    "Give a one-sentence definition of speculative decoding.",
    "Compare batch inference vs continuous batching.",
    "Explain quantization (INT4) and its effect on model quality.",
    "What are the benefits of a fleet of small models vs one large model?",
    "Describe the role of a router in a model-serving mesh.",
    "Write a function signature (in Python) for a token bucket rate limiter.",
    "Summarize why idle GPUs waste both power and capital.",
    "What is a composite routing score and how is it used here?",
    "Explain the concept of 'resident model' in an inference fleet.",
    "Describe how a deadlock can occur with non-reentrant locks.",
    "In one sentence: what makes an LLM task suitable for a GPU node?",
    "List two ways to detect that a GPU node has gone offline.",
    "Explain why echo placeholder tasks should run locally, not on the fleet.",
    "Write a one-line insight about observability for inference fleets.",
]


@dataclass
class Stats:
    submitted: int = 0
    done: int = 0
    failed: int = 0
    pending: set[str] = field(default_factory=set)
    by_node: dict[str, int] = field(default_factory=dict)


def _req(method: str, path: str, payload: dict | None = None, retries: int = 3) -> tuple[int, dict]:
    headers = {"Authorization": "Basic " + base64.b64encode(f"{AUTH_USER}:{AUTH_PASS}".encode()).decode()}
    data = json.dumps(payload).encode() if payload is not None else None
    if data:
        headers["Content-Type"] = "application/json"
    last_err: Exception | None = None
    for attempt in range(retries):
        try:
            req = urllib.request.Request(f"{BASE_URL}{path}", data=data, method=method, headers=headers)
            with urllib.request.urlopen(req, timeout=HTTP_TIMEOUT) as resp:
                return resp.status, json.loads(resp.read().decode() or "{}")
        except urllib.error.HTTPError as e:
            try:
                return e.code, json.loads(e.read().decode() or "{}")
            except Exception:
                return e.code, {"error": str(e)}
        except Exception as e:  # URLError, ConnectionReset, timeout, etc.
            last_err = e
            if attempt < retries - 1:
                time.sleep(1.0 * (attempt + 1))
    return 0, {"error": str(last_err) if last_err else "request failed"}


def submit(task_id: str, prompt: str, model: str = "") -> str | None:
    payload = {"prompt": prompt}
    if model:
        payload["model"] = model
    body = {
        "title": f"fleet-gen-{task_id}",
        "task_type": "swarm_task",
        "kind": "fleet_workload",
        "status": "READY",
        "required_capabilities": ["llm"],
        "payload": payload,
        "idempotency_key": f"fleet-gen-{task_id}",
    }
    st, resp = _req("POST", "/api/tasks", body)
    if st == 200 and resp.get("task_id"):
        return resp["task_id"]
    # 409/idempotent: already exists -> reuse the supplied id
    if st in (200, 409):
        return task_id
    return None


def poll(tid: str) -> dict | None:
    st, resp = _req("GET", f"/api/tasks/{tid}")
    if st != 200:
        return None
    t = resp.get("task", {})
    if t.get("status") in ("DONE", "FAILED"):
        return t
    return None


def run_once(count: int, batch: int, interval: float, model: str = "") -> Stats:
    stats = Stats()
    seq = 0
    while stats.submitted < count:
        to_send = min(batch, count - stats.submitted)
        for _ in range(to_send):
            tid = f"fleet-gen-{int(time.time()*1000)}-{seq}"
            seq += 1
            prompt = PROMPTS[stats.submitted % len(PROMPTS)]
            real = submit(tid, prompt, model)
            if real:
                stats.submitted += 1
                stats.pending.add(real)
        # Give the fleet a moment to claim/execute.
        time.sleep(interval)
        # Reap finished tasks.
        for tid in list(stats.pending):
            t = poll(tid)
            if t:
                stats.pending.discard(tid)
                rj = t.get("result_json") or {}
                node = rj.get("node") if isinstance(rj, dict) else None
                if t.get("status") == "DONE":
                    stats.done += 1
                    if node:
                        stats.by_node[node] = stats.by_node.get(node, 0) + 1
                else:
                    stats.failed += 1
        if stats.submitted % (batch * 2) == 0:
            print(f"[fleet-gen] submitted={stats.submitted} done={stats.done} "
                  f"failed={stats.failed} inflight={len(stats.pending)} nodes={stats.by_node}", flush=True)
    # Final drain (bounded so the generator always returns promptly).
    deadline = time.time() + 90
    while stats.pending and time.time() < deadline:
        for tid in list(stats.pending):
            t = poll(tid)
            if t:
                stats.pending.discard(tid)
                rj = t.get("result_json") or {}
                node = rj.get("node") if isinstance(rj, dict) else None
                if t.get("status") == "DONE":
                    stats.done += 1
                    if node:
                        stats.by_node[node] = stats.by_node.get(node, 0) + 1
                else:
                    stats.failed += 1
        time.sleep(2)
    return stats


def run_loop(rate: float, model: str = "") -> None:
    seq = 0
    stats = Stats()
    print(f"[fleet-gen] continuous mode, rate={rate}/s")
    try:
        while True:
            tid = f"fleet-gen-{int(time.time()*1000)}-{seq}"
            seq += 1
            prompt = PROMPTS[seq % len(PROMPTS)]
            real = submit(tid, prompt, model)
            if real:
                stats.submitted += 1
                stats.pending.add(real)
            # Reap finished.
            for done_tid in list(stats.pending):
                t = poll(done_tid)
                if t:
                    stats.pending.discard(done_tid)
                    rj = t.get("result_json") or {}
                    node = rj.get("node") if isinstance(rj, dict) else None
                    if t.get("status") == "DONE":
                        stats.done += 1
                        if node:
                            stats.by_node[node] = stats.by_node.get(node, 0) + 1
                    else:
                        stats.failed += 1
            time.sleep(max(0.1, 1.0 / max(0.1, rate)))
            if stats.submitted % 25 == 0:
                print(f"[fleet-gen] submitted={stats.submitted} done={stats.done} "
                      f"failed={stats.failed} inflight={len(stats.pending)} nodes={stats.by_node}")
    except KeyboardInterrupt:
        print(f"\n[fleet-gen] stopped. submitted={stats.submitted} done={stats.done} "
              f"failed={stats.failed} nodes={stats.by_node}")


def main() -> None:
    ap = argparse.ArgumentParser(description="Generate fleet LLM workload tasks")
    ap.add_argument("--count", type=int, default=100, help="tasks to submit (non-loop mode)")
    ap.add_argument("--batch", type=int, default=10, help="tasks per send batch")
    ap.add_argument("--interval", type=float, default=1.0, help="sleep between batches (s)")
    ap.add_argument("--model", type=str, default="", help="optional model hint for tasks")
    ap.add_argument("--loop", action="store_true", help="run continuously")
    ap.add_argument("--rate", type=float, default=2.0, help="tasks/sec in loop mode")
    args = ap.parse_args()

    if args.loop:
        run_loop(args.rate, args.model)
        return

    stats = run_once(args.count, args.batch, args.interval, args.model)
    print(f"[fleet-gen] done. submitted={stats.submitted} done={stats.done} "
          f"failed={stats.failed} inflight={len(stats.pending)}")
    print(f"[fleet-gen] completed per node: {stats.by_node}")


if __name__ == "__main__":
    main()
