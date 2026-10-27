"""Tiny webhook sink for local alert tests (standard library only).

POST /<anything>  stores the JSON body in /data/received.jsonl and answers 204
GET  /received    returns every stored payload as a JSON array
GET  /health      liveness
"""

from __future__ import annotations

import json
import os
import threading
from datetime import UTC, datetime
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path

DATA_FILE = Path(os.environ.get("RECEIVER_DATA", "/data/received.jsonl"))
LOCK = threading.Lock()


class Handler(BaseHTTPRequestHandler):
    def _send(self, status: int, body: bytes = b"", content_type: str = "application/json") -> None:
        self.send_response(status)
        self.send_header("Content-Type", content_type)
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def do_POST(self) -> None:  # noqa: N802 - http.server API
        length = int(self.headers.get("Content-Length", 0))
        raw = self.rfile.read(length)
        try:
            payload = json.loads(raw or b"{}")
        except ValueError:
            payload = {"unparsed": raw.decode("utf-8", "replace")}
        entry = {
            "received_at": datetime.now(UTC).isoformat(timespec="seconds"),
            "path": self.path,
            "payload": payload,
        }
        with LOCK:
            DATA_FILE.parent.mkdir(parents=True, exist_ok=True)
            with DATA_FILE.open("a", encoding="utf-8") as handle:
                handle.write(json.dumps(entry) + "\n")
        self._send(204)

    def do_GET(self) -> None:  # noqa: N802 - http.server API
        if self.path == "/health":
            self._send(200, b'{"status": "ok"}')
        elif self.path == "/received":
            with LOCK:
                lines = (
                    DATA_FILE.read_text(encoding="utf-8").splitlines() if DATA_FILE.exists() else []
                )
            self._send(200, ("[" + ",".join(lines) + "]").encode())
        else:
            self._send(404, b'{"error": "not found"}')

    def log_message(self, fmt: str, *args: object) -> None:
        print(f"{self.address_string()} {fmt % args}", flush=True)


if __name__ == "__main__":
    port = int(os.environ.get("RECEIVER_PORT", "9099"))
    print(f"webhook receiver listening on :{port}", flush=True)
    ThreadingHTTPServer(("0.0.0.0", port), Handler).serve_forever()
