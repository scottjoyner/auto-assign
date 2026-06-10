from __future__ import annotations

try:
    from prometheus_client import CONTENT_TYPE_LATEST, Counter, Gauge, Histogram, REGISTRY, generate_latest
except ModuleNotFoundError:
    class _NoopMetric:
        def __init__(self, *args, **kwargs):
            pass

        def labels(self, *args, **kwargs):
            return self

        def inc(self, *args, **kwargs):
            return self

        def observe(self, *args, **kwargs):
            return self

        def set(self, *args, **kwargs):
            return self

    class _NoopRegistry:
        _collector_to_names = {}

    CONTENT_TYPE_LATEST = "text/plain; version=0.0.4; charset=utf-8"
    Counter = Histogram = Gauge = _NoopMetric
    REGISTRY = _NoopRegistry()

    def generate_latest() -> bytes:
        return b""


def _safe_counter(name: str, doc: str, labels: list | None = None) -> Counter:
    try:
        return Counter(name, doc, labels or [])
    except ValueError:
        for c in REGISTRY._collector_to_names:
            if hasattr(c, "_name") and c._name == name:
                return c
        return Counter(name, doc, labels or [])


def _safe_gauge(name: str, doc: str, labels: list | None = None) -> Gauge:
    try:
        return Gauge(name, doc, labels or [])
    except ValueError:
        for c in REGISTRY._collector_to_names:
            if hasattr(c, "_name") and c._name == name:
                return c
        return Gauge(name, doc, labels or [])


def _safe_histogram(name: str, doc: str, labels: list | None = None) -> Histogram:
    try:
        return Histogram(name, doc, labels or [])
    except ValueError:
        for c in REGISTRY._collector_to_names:
            if hasattr(c, "_name") and c._name == name:
                return c
        return Histogram(name, doc, labels or [])


REQUESTS = _safe_counter("auto_assign_http_requests_total", "HTTP requests", ["path", "method", "status"])
REQUEST_LATENCY = _safe_histogram("auto_assign_http_request_duration_seconds", "HTTP request duration", ["path", "method"])
EVENTS_INGESTED = _safe_counter("auto_assign_events_ingested_total", "Events ingested", ["event_type", "status"])
ASSIGNMENTS_CREATED = _safe_counter("auto_assign_assignments_created_total", "Assignments created", ["status"])
HEARTBEATS_RECORDED = _safe_counter("auto_assign_heartbeats_recorded_total", "Heartbeats recorded", ["status"])
SCHEDULER_TICKS = _safe_counter("auto_assign_scheduler_ticks_total", "Scheduler ticks", ["result"])
ACTIVE_ASSIGNMENTS = _safe_gauge("auto_assign_active_assignments", "Active assignments")
STALE_HEARTBEATS = _safe_gauge("auto_assign_stale_heartbeats", "Stale heartbeats")
OUTBOX_PENDING = _safe_gauge("auto_assign_outbox_pending", "Pending outbox events")
