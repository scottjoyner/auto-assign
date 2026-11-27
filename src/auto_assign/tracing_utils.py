from __future__ import annotations

import contextvars
import re

from fastapi import Request

trace_id_var: contextvars.ContextVar[str] = contextvars.ContextVar("trace_id", default="")

# Trace/correlation IDs are echoed into response headers, written into access
# logs and forwarded to downstream services.  Accept only a bounded printable
# token: a client-supplied CRLF, control character or unbounded string must not
# be allowed to inject headers, forge log records or flood every hop (r34).
TRACE_ID_MAX_LEN = 128
_TRACE_ID_RE = re.compile(r"[A-Za-z0-9][A-Za-z0-9._:-]*\Z")


def valid_trace_id(value: str | None) -> str | None:
    """Return the token only when it is safe to propagate, else ``None``."""
    if not value or len(value) > TRACE_ID_MAX_LEN:
        return None
    return value if _TRACE_ID_RE.fullmatch(value) else None


def set_trace_id(tid: str | None) -> None:
    """Set the validated outbound trace id (fail closed on unsafe input)."""
    trace_id_var.set(valid_trace_id(tid) or "")


def set_trace_context(request: Request) -> None:
    tid = valid_trace_id(request.headers.get("X-Trace-ID")) or valid_trace_id(
        request.headers.get("X-Correlation-ID")
    )
    trace_id_var.set(tid or "")


def inject_trace_headers(headers: dict[str, str] | None = None) -> dict[str, str]:
    result = dict(headers or {})
    tid = valid_trace_id(trace_id_var.get())
    if tid:
        result.setdefault("X-Trace-ID", tid)
    return result
