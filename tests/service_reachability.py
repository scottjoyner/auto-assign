"""Small stdlib helper for detecting whether a live service answers at a URL."""

from __future__ import annotations

import http.client
from urllib.error import HTTPError, URLError
from urllib.request import urlopen


def service_reachable(base_url: str, *, timeout: float = 1.0) -> bool:
    """Return True when an HTTP service answers ``GET /health`` at ``base_url``.

    Any HTTP response (including 4xx/5xx) means the service is present, so the
    integration test will exercise it and report the real assertion failure.
    Connection failures, timeouts and malformed URLs mean the service is absent.
    """
    target = base_url.rstrip("/") + "/health"
    try:
        with urlopen(target, timeout=timeout) as response:
            response.read(1)
            return True
    except HTTPError:
        return True
    except (URLError, TimeoutError, OSError, http.client.HTTPException, ValueError):
        return False
