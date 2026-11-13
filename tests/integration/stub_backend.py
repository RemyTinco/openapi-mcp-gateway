"""Stdlib-only HTTP backend for end-to-end tests.

Every request is answered with a JSON echo of its method, path, query and
headers (header names lower-cased). A path whose last segment is ``404``
answers 404 instead, so tests can exercise upstream error mapping.
"""

from __future__ import annotations

import json
import threading
from contextlib import contextmanager
from dataclasses import dataclass, field
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from typing import TYPE_CHECKING, Any
from urllib.parse import parse_qs, urlsplit

if TYPE_CHECKING:
    from collections.abc import Iterator


@dataclass
class StubBackend:
    url: str
    requests: list[dict[str, Any]] = field(default_factory=list)

    @property
    def paths(self) -> list[str]:
        return [r["path"] for r in self.requests]


def _handler(stub: StubBackend) -> type[BaseHTTPRequestHandler]:
    class Handler(BaseHTTPRequestHandler):
        protocol_version = "HTTP/1.1"

        def _respond(self) -> None:
            length = int(self.headers.get("content-length") or 0)
            if length:
                self.rfile.read(length)
            parts = urlsplit(self.path)
            echo = {
                "method": self.command,
                "path": parts.path,
                "query": {k: v if len(v) > 1 else v[0] for k, v in parse_qs(parts.query).items()},
                "headers": {k.lower(): v for k, v in self.headers.items()},
            }
            stub.requests.append(echo)
            status = 404 if parts.path.rstrip("/").endswith("/404") else 200
            payload = echo if status == 200 else {"detail": "Not found."}
            body = json.dumps(payload).encode()
            self.send_response(status)
            self.send_header("Content-Type", "application/json")
            self.send_header("Content-Length", str(len(body)))
            self.end_headers()
            self.wfile.write(body)

        do_GET = do_POST = do_PUT = do_PATCH = do_DELETE = _respond  # noqa: N815 - http.server API

        # Keep test output clean. The signature is http.server's.
        def log_message(self, format: str, *args: Any) -> None:  # noqa: A002
            pass

    return Handler


@contextmanager
def stub_backend() -> Iterator[StubBackend]:
    """Serve a ``StubBackend`` on an ephemeral 127.0.0.1 port in a daemon thread."""
    stub = StubBackend(url="")
    server = ThreadingHTTPServer(("127.0.0.1", 0), _handler(stub))
    server.daemon_threads = True
    stub.url = f"http://127.0.0.1:{server.server_address[1]}"
    thread = threading.Thread(target=server.serve_forever, name="stub-backend", daemon=True)
    thread.start()
    try:
        yield stub
    finally:
        server.shutdown()
        server.server_close()
        thread.join(timeout=5)
