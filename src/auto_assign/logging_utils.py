from __future__ import annotations

import json
import logging
import os
import sys
import time
import uuid
from collections.abc import Callable
from typing import Any

from fastapi import FastAPI, Request, Response

from .metrics import REQUESTS, REQUEST_LATENCY
from .tracing_utils import set_trace_context

LOG_FORMAT = os.getenv("LOG_FORMAT", "text")
LOG_LEVEL = os.getenv("LOG_LEVEL", "INFO").upper()


class CorrelationIdFilter(logging.Filter):
    def filter(self, record: logging.LogRecord) -> bool:
        if not hasattr(record, "correlation_id"):
            record.correlation_id = ""
        return True


class JsonFormatter(logging.Formatter):
    def format(self, record: logging.LogRecord) -> str:
        obj: dict[str, Any] = {
            "ts": record.created,
            "level": record.levelname,
            "logger": record.name,
            "message": record.getMessage(),
        }
        cid = getattr(record, "correlation_id", None)
        if cid:
            obj["correlation_id"] = cid
        if record.exc_info and record.exc_info[0]:
            obj["exception"] = self.formatException(record.exc_info)
        for key in ("path", "method", "status_code", "elapsed_ms", "task_id", "assignment_id"):
            val = getattr(record, key, None)
            if val is not None:
                obj[key] = val
        return json.dumps(obj, default=str)


def setup_logging() -> None:
    level = getattr(logging, LOG_LEVEL, logging.INFO)
    root = logging.getLogger()
    root.setLevel(level)
    for h in root.handlers[:]:
        root.removeHandler(h)
    handler = logging.StreamHandler(sys.stdout)
    handler.addFilter(CorrelationIdFilter())
    if LOG_FORMAT == "json":
        handler.setFormatter(JsonFormatter())
    else:
        handler.setFormatter(
            logging.Formatter("%(asctime)s | %(levelname)-8s | %(name)s | %(message)s")
        )
    root.addHandler(handler)


def install_logging_middleware(app: FastAPI) -> None:
    @app.middleware("http")
    async def _correlation_id_middleware(request: Request, call_next: Callable) -> Response:
        cid = request.headers.get("X-Correlation-ID") or str(uuid.uuid4())
        trace_id = request.headers.get("X-Trace-ID") or cid
        request.state.correlation_id = cid
        request.state.trace_id = trace_id
        set_trace_context(request)
        start = time.time()
        response: Response = await call_next(request)
        response.headers["X-Correlation-ID"] = cid
        response.headers["X-Trace-ID"] = trace_id
        elapsed = time.time() - start
        REQUESTS.labels(path=request.url.path, method=request.method, status=response.status_code).inc()
        REQUEST_LATENCY.labels(path=request.url.path, method=request.method).observe(elapsed)
        logging.getLogger("access").info(
            "%s %s -> %d (%.1fms)",
            request.method,
            request.url.path,
            response.status_code,
            elapsed * 1000,
            extra={"correlation_id": cid, "path": request.url.path, "method": request.method, "status_code": response.status_code, "elapsed_ms": round(elapsed * 1000, 1)},
        )
        return response


def trace_headers(request: Request) -> dict[str, str]:
    headers: dict[str, str] = {}
    cid = getattr(request.state, "correlation_id", None) or request.headers.get("X-Correlation-ID", "")
    tid = getattr(request.state, "trace_id", None) or request.headers.get("X-Trace-ID", "")
    if cid:
        headers["X-Correlation-ID"] = cid
    if tid:
        headers["X-Trace-ID"] = tid
    return headers
