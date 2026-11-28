"""Unit coverage for the live-service reachability helper (stdlib only)."""

from __future__ import annotations

import json
import socket
import threading
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

from tests.service_reachability import service_reachable


def _closed_localhost_port() -> int:
    """Reserve then release an ephemeral localhost port so it is closed."""
    with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as sock:
        sock.bind(("127.0.0.1", 0))
        return int(sock.getsockname()[1])


def test_service_reachable_is_false_for_closed_port() -> None:
    assert service_reachable(f"http://127.0.0.1:{_closed_localhost_port()}") is False


def test_service_reachable_is_true_for_in_process_http_server() -> None:
    class HealthHandler(BaseHTTPRequestHandler):
        def do_GET(self) -> None:
            body = json.dumps({"service": "assistx", "ok": True}).encode()
            self.send_response(200)
            self.send_header("Content-Type", "application/json")
            self.send_header("Content-Length", str(len(body)))
            self.end_headers()
            self.wfile.write(body)

        def log_message(self, format: str, *args: object) -> None:
            pass

    server = ThreadingHTTPServer(("127.0.0.1", 0), HealthHandler)
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    try:
        host, port = server.server_address[:2]
        assert service_reachable(f"http://{host}:{port}") is True
    finally:
        server.shutdown()
        server.server_close()
        thread.join(timeout=5)
