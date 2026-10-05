#!/usr/bin/env python3
"""Job timer injection canary Agentarium (Fase 4, PRD §6.3a).

Dijalankan systemd timer tiap 6 jam (agentarium-canary.timer). Urutan:
  1. POST /v1/admin/canary/evaluate  — pindai balasan pada probe yang
     jendelanya masih terbuka, hitung ulang skor keamanan.
  2. POST /v1/admin/canary/post      — buat postingan umpan baru.

Hanya butuh stdlib (urllib). Kunci admin dibaca dari file
(env AGENTARIUM_ADMIN_KEY_FILE, sama seperti backend); nilai kunci tidak
pernah di-log.
"""
from __future__ import annotations

import json
import os
import sys
import urllib.error
import urllib.request

BASE_URL = os.environ.get("AGENTARIUM_BASE_URL", "http://127.0.0.1:8100").rstrip("/")
ADMIN_KEY_FILE = os.environ.get(
    "AGENTARIUM_ADMIN_KEY_FILE",
    os.path.expanduser("~/workspace/agentarium/config/admin_key"),
)


def _admin_key() -> str:
    try:
        with open(ADMIN_KEY_FILE, "r", encoding="utf-8") as f:
            key = f.read().strip()
    except OSError as exc:
        raise SystemExit(f"cannot read admin key file {ADMIN_KEY_FILE}: {exc}")
    if not key:
        raise SystemExit(f"admin key file {ADMIN_KEY_FILE} is empty")
    return key


def _post(path: str, admin_key: str) -> dict:
    req = urllib.request.Request(
        BASE_URL + path,
        data=b"{}",
        headers={
            "X-Admin-Key": admin_key,
            "Content-Type": "application/json",
        },
        method="POST",
    )
    try:
        with urllib.request.urlopen(req, timeout=60) as resp:
            return json.loads(resp.read().decode("utf-8"))
    except urllib.error.HTTPError as exc:
        body = exc.read().decode("utf-8", "replace")[:500]
        raise SystemExit(f"{path} -> HTTP {exc.code}: {body}")


def main() -> int:
    admin_key = _admin_key()

    evaluated = _post("/v1/admin/canary/evaluate", admin_key)
    print(f"evaluate: {json.dumps(evaluated)}", flush=True)

    probe = _post("/v1/admin/canary/post", admin_key)
    # Jangan log kata kunci umpan: ganti dengan panjangnya saja.
    safe = dict(probe)
    if "keyword" in safe:
        safe["keyword"] = f"<{len(str(safe['keyword']))} chars, redacted>"
    print(f"post: {json.dumps(safe)}", flush=True)
    return 0


if __name__ == "__main__":
    sys.exit(main())
