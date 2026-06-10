from __future__ import annotations

import contextvars
from typing import Any

from fastapi import Request

trace_id_var: contextvars.ContextVar[str] = contextvars.ContextVar("trace_id", default="")


def set_trace_context(request: Request) -> None:
    tid = request.headers.get("X-Trace-ID") or request.headers.get("X-Correlation-ID") or ""
    trace_id_var.set(tid)


def inject_trace_headers(headers: dict[str, str] | None = None) -> dict[str, str]:
    result = dict(headers or {})
    tid = trace_id_var.get()
    if tid:
        result.setdefault("X-Trace-ID", tid)
    return result
