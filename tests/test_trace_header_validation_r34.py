"""Regression: unvalidated client trace/correlation headers must fail closed (r34).

A request-supplied ``X-Trace-ID`` / ``X-Correlation-ID`` is echoed into the HTTP
response, emitted into access logs and forwarded to downstream services via
``inject_trace_headers()``.  Accepting it verbatim lets a client inject header
lines, forge log records and smuggle unbounded strings into every hop.  These
tests are hermetic: no network, no live services, no credentials.
"""

from __future__ import annotations

import pytest
from starlette.requests import Request

from auto_assign.tracing_utils import inject_trace_headers, set_trace_context, trace_id_var


def _request(headers: dict[str, str]) -> Request:
    scope = {
        "type": "http",
        "method": "GET",
        "path": "/health",
        "scheme": "http",
        "server": ("testserver", 80),
        "query_string": b"",
        "headers": [(k.lower().encode(), v.encode("latin-1")) for k, v in headers.items()],
    }
    return Request(scope)


@pytest.fixture(autouse=True)
def _isolate_context():
    token = trace_id_var.set("")
    try:
        yield
    finally:
        trace_id_var.reset(token)


def test_crlf_trace_header_is_not_propagated() -> None:
    set_trace_context(_request({"X-Trace-ID": "ok\r\nX-Injected: yes"}))
    assert "X-Trace-ID" not in inject_trace_headers()


def test_embedded_control_char_correlation_header_is_not_propagated() -> None:
    set_trace_context(_request({"X-Correlation-ID": "abc\x1fdef"}))
    assert "X-Trace-ID" not in inject_trace_headers()


def test_whitespace_only_header_is_not_propagated() -> None:
    set_trace_context(_request({"X-Trace-ID": "   "}))
    assert "X-Trace-ID" not in inject_trace_headers()


def test_oversized_header_is_not_propagated() -> None:
    set_trace_context(_request({"X-Trace-ID": "a" * 4096}))
    assert "X-Trace-ID" not in inject_trace_headers()


def test_poisoned_context_var_does_not_leak_outbound() -> None:
    # Defence in depth: even if some other code sets the context var directly,
    # the outbound header builder must refuse to emit an unsafe token.
    trace_id_var.set("bad\r\nX-Leak: 1")
    assert "X-Trace-ID" not in inject_trace_headers()


def test_valid_trace_header_still_propagates() -> None:
    set_trace_context(_request({"X-Trace-ID": "test-tid-integration"}))
    assert inject_trace_headers()["X-Trace-ID"] == "test-tid-integration"


def test_valid_correlation_header_fallback_still_propagates() -> None:
    set_trace_context(_request({"X-Correlation-ID": "test-cid-integration"}))
    assert inject_trace_headers()["X-Trace-ID"] == "test-cid-integration"


def test_caller_headers_are_preserved_and_not_overwritten() -> None:
    set_trace_context(_request({"X-Trace-ID": "trace-1"}))
    out = inject_trace_headers({"X-Trace-ID": "preexisting", "Accept": "application/json"})
    assert out["X-Trace-ID"] == "preexisting"
    assert out["Accept"] == "application/json"
