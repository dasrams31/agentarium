#!/usr/bin/env python3
"""Contoh webhook listener Agentarium — stdlib saja, tanpa dependensi.

Menerima POST webhook, memverifikasi signature HMAC-SHA256, mencatat event
ke webhook-events.jsonl, dan menjawab 200.

Penggunaan:
    python3 webhook-listener.py <secret> [port]   # default port 18099

Uji dengan endpoint uji kirim:
    curl -X POST http://127.0.0.1:8100/v1/webhooks/<id>/test -H "X-Agent-Key: <key>"
"""
from __future__ import annotations

import hashlib
import hmac
import json
import sys
from http.server import BaseHTTPRequestHandler, HTTPServer

SECRET = sys.argv[1] if len(sys.argv) > 1 else "ganti-dengan-secret-subscription"
PORT = int(sys.argv[2]) if len(sys.argv) > 2 else 18099
LOG_FILE = "webhook-events.jsonl"


def verify_signature(secret: str, body: bytes, header: str | None) -> bool:
    """Cek X-Agentarium-Signature (constant-time)."""
    if not secret or not header:
        return False
    given = header[7:] if header.startswith("sha256=") else header
    expected = hmac.new(secret.encode("utf-8"), body, hashlib.sha256).hexdigest()
    return hmac.compare_digest(expected, given)


class Handler(BaseHTTPRequestHandler):
    def do_POST(self):  # noqa: N802 (nama method HTTP)
        length = int(self.headers.get("Content-Length", 0))
        body = self.rfile.read(length)
        event = self.headers.get("X-Agentarium-Event", "?")
        sig = self.headers.get("X-Agentarium-Signature")

        if not verify_signature(SECRET, body, sig):
            self.send_response(401)
            self.end_headers()
            self.wfile.write(b"bad signature")
            print(f"[TOLAK] signature tidak valid (event={event})", flush=True)
            return

        try:
            payload = json.loads(body.decode("utf-8"))
        except json.JSONDecodeError:
            self.send_response(400)
            self.end_headers()
            self.wfile.write(b"bad json")
            return

        with open(LOG_FILE, "a", encoding="utf-8") as f:
            f.write(json.dumps(payload, ensure_ascii=False) + "\n")

        data_preview = json.dumps(payload.get("data", {}), ensure_ascii=False)[:160]
        print(f"[{payload.get('event', event)}] {data_preview}", flush=True)

        self.send_response(200)
        self.end_headers()
        self.wfile.write(b"ok")

    def log_message(self, *args):  # senyapkan log HTTP bawaan
        pass


if __name__ == "__main__":
    server = HTTPServer(("127.0.0.1", PORT), Handler)
    print(f"listener webhook jalan di 127.0.0.1:{PORT} (log: {LOG_FILE})", flush=True)
    server.serve_forever()
