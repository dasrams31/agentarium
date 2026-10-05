"""Observability Agentarium (Fase 4).

Isi:
- Middleware metrik request in-memory (rolling window 5 menit) untuk
  menghitung error rate tanpa menyimpan data sensitif.
- GET /v1/health  — ringan, tanpa auth, untuk monitor internal.
- GET /v1/status  — JSON status ringkas: service systemd, DB, error rate,
  info backup terakhir, flag degraded.
- GET /status     — halaman HTML ringan (gaya jurnal naturalis) yang
  me-render data yang sama dengan /v1/status.

Alerting: bila kondisi "degraded" terdeteksi (service mati atau error rate
> 5% dalam 5 menit), status berubah jadi "degraded" di /v1/status dan satu
  baris WARNING ditulis ke log systemd (journal). Tidak ada notifikasi
  eksternal (email/SMS/webhook) — butuh persetujuan user.

Catatan jujur: /v1/status disajikan oleh backend itu sendiri, jadi bila
service backend mati total, endpoint ini tidak bisa menjawab — monitor
eksternal mendeteksi itu lewat connection timeout. Yang bisa dilaporkan
endpoint ini: service dependen (house agent, populasi, tunnel), kesehatan DB,
dan error rate request yang masuk.
"""
from __future__ import annotations

import json
import logging
import subprocess
import time
from collections import deque
from datetime import datetime, timezone
from pathlib import Path
from threading import Lock

from fastapi import APIRouter, Request
from fastapi.responses import HTMLResponse, JSONResponse
from sqlalchemy import text

from database import engine

logger = logging.getLogger("agentarium.observability")

router = APIRouter()

PROCESS_START_MONO = time.monotonic()
PROCESS_START_UTC = datetime.now(timezone.utc)

# --- Metrik request in-memory (rolling window 5 menit) ---------------------
# Hanya menyimpan (timestamp, is_error); tidak ada URL, body, atau identitas.
_WINDOW_SEC = 300
_MIN_SAMPLE = 10          # butuh >= 10 request dalam window sebelum ambang dipakai
_ERROR_THRESHOLD = 0.05   # > 5% error dalam 5 menit => degraded

_events: deque[tuple[float, bool]] = deque()
_events_lock = Lock()
_EXCLUDED_PATHS = ("/v1/health", "/v1/status", "/status")


def record_request(is_error: bool) -> None:
    now = time.monotonic()
    with _events_lock:
        _events.append((now, is_error))
        cutoff = now - _WINDOW_SEC
        while _events and _events[0][0] < cutoff:
            _events.popleft()


def error_rate_5m() -> tuple[float, int, int]:
    """Return (error_rate, error_count, total_count) dalam 5 menit terakhir."""
    now = time.monotonic()
    with _events_lock:
        cutoff = now - _WINDOW_SEC
        while _events and _events[0][0] < cutoff:
            _events.popleft()
        total = len(_events)
        errors = sum(1 for _, e in _events if e)
    rate = (errors / total) if total else 0.0
    return rate, errors, total


async def track_request(request: Request, call_next):
    """Middleware: catat tiap request (kecuali endpoint monitor itu sendiri)."""
    if request.url.path in _EXCLUDED_PATHS:
        return await call_next(request)
    try:
        response = await call_next(request)
    except Exception:
        record_request(True)
        raise
    record_request(response.status_code >= 500)
    return response


# --- Pemeriksaan DB ----------------------------------------------------------

def db_check() -> dict:
    """Ping DB + ukuran DB. Tidak membocorkan connection string / kredensial."""
    t0 = time.perf_counter()
    try:
        with engine.connect() as conn:
            conn.execute(text("SELECT 1"))
            try:
                size = conn.execute(
                    text("SELECT pg_database_size(current_database())")
                ).scalar()
            except Exception:
                size = None  # bukan Postgres (mis. fallback SQLite dev)
        latency_ms = round((time.perf_counter() - t0) * 1000, 1)
        return {"reachable": True, "latency_ms": latency_ms,
                "size_bytes": int(size) if size is not None else None}
    except Exception as exc:  # noqa: BLE001 — health check harus tahan error
        latency_ms = round((time.perf_counter() - t0) * 1000, 1)
        logger.warning("db health check failed after %.1f ms: %s",
                       latency_ms, type(exc).__name__)
        return {"reachable": False, "latency_ms": latency_ms,
                "size_bytes": None}


def _human_bytes(n: int | None) -> str | None:
    if n is None:
        return None
    v = float(n)
    for unit in ("B", "KB", "MB", "GB"):
        if v < 1024 or unit == "GB":
            return f"{v:.1f} {unit}"
        v /= 1024
    return f"{v:.1f} GB"


# --- Service systemd ----------------------------------------------------------

WATCHED_SERVICES = [
    "agentarium",               # backend API (service ini sendiri)
    "agentarium-logika7",
    "agentarium-kacaubalau",
    "agentarium-dataneng",
    "agentarium-populasi",
    "agentarium-tunnel",
]


def service_states() -> dict[str, str]:
    states: dict[str, str] = {}
    for svc in WATCHED_SERVICES:
        try:
            out = subprocess.run(
                ["systemctl", "is-active", f"{svc}.service"],
                capture_output=True, text=True, timeout=5,
            )
            states[svc] = out.stdout.strip() or "unknown"
        except Exception:  # noqa: BLE001
            states[svc] = "unknown"
    return states


# --- Info backup terakhir ------------------------------------------------------

# NOTE: jangan pakai Path.home() — service jalan sebagai root sehingga
# home = /root, bukan /home/hatch. Path absolut eksplisit.
BACKUP_DIR = Path("/home/hatch/workspace/agentarium/backups")


def last_backup_info() -> dict | None:
    meta = BACKUP_DIR / "last_backup.json"
    try:
        if meta.exists():
            return json.loads(meta.read_text())
    except Exception:  # noqa: BLE001
        pass
    return None


# --- Agregasi status -----------------------------------------------------------

_degraded_alerted = False  # agar log warning hanya saat transisi -> degraded


def compute_status() -> dict:
    global _degraded_alerted
    db = db_check()
    services = service_states()
    rate, err_count, total = error_rate_5m()
    backup = last_backup_info()

    reasons: list[str] = []
    down = [s for s, st in services.items() if st != "active"]
    if down:
        reasons.append("service tidak aktif: " + ", ".join(down))
    if not db["reachable"]:
        reasons.append("database tidak bisa dijangkau")
    if total >= _MIN_SAMPLE and rate > _ERROR_THRESHOLD:
        reasons.append(
            f"error rate {rate * 100:.1f}% (> 5%) dari {total} request / 5 mnt"
        )

    degraded = bool(reasons)
    if degraded and not _degraded_alerted:
        logger.warning("agentarium DEGRADED: %s", "; ".join(reasons))
    elif not degraded and _degraded_alerted:
        logger.info("agentarium pulih: status kembali ok")
    _degraded_alerted = degraded

    return {
        "status": "degraded" if degraded else "ok",
        "degraded_reasons": reasons,
        "checked_at": datetime.now(timezone.utc).isoformat(),
        "uptime_seconds": int(time.monotonic() - PROCESS_START_MONO),
        "db": {
            "reachable": db["reachable"],
            "latency_ms": db["latency_ms"],
            "size_bytes": db["size_bytes"],
            "size_human": _human_bytes(db["size_bytes"]),
        },
        "error_rate_5m": {
            "rate": round(rate, 4),
            "errors": err_count,
            "total": total,
            "threshold": _ERROR_THRESHOLD,
            "min_sample": _MIN_SAMPLE,
        },
        "services": services,
        "last_backup": backup,
    }


# --- Endpoint -------------------------------------------------------------------

@router.get("/v1/health")
def health():
    """Health check ringan untuk monitor internal. Tanpa auth, tanpa info sensitif."""
    db = db_check()
    payload = {
        "status": "ok" if db["reachable"] else "unhealthy",
        "service": "agentarium",
        "uptime_seconds": int(time.monotonic() - PROCESS_START_MONO),
        "db": {
            "reachable": db["reachable"],
            "latency_ms": db["latency_ms"],
            "size_bytes": db["size_bytes"],
            "size_human": _human_bytes(db["size_bytes"]),
        },
    }
    if db["reachable"]:
        return payload
    return JSONResponse(status_code=503, content=payload)


@router.get("/v1/status")
def status_json():
    return compute_status()


_STATUS_CSS = """
body{background:#f4f1ea;color:#14160f;font-family:Fraunces,Georgia,serif;
margin:0;padding:2rem 1rem}
.wrap{max-width:720px;margin:0 auto}
h1{font-size:1.6rem;border-bottom:2px solid #e86a33;padding-bottom:.4rem}
.badge{display:inline-block;padding:.2rem .8rem;border-radius:999px;
font-family:ui-monospace,Menlo,monospace;font-size:.9rem}
.ok{background:#2e6b34;color:#f4f1ea}.deg{background:#b3401e;color:#f4f1ea}
table{width:100%;border-collapse:collapse;margin:1rem 0;font-size:.95rem}
td,th{border:1px solid #6f6a5c;padding:.4rem .6rem;text-align:left}
th{background:#e7e0d0}.mono{font-family:ui-monospace,Menlo,monospace;font-size:.85rem}
.note{color:#6f6a5c;font-size:.85rem;margin-top:1.5rem}
ul{margin:.3rem 0}
"""


@router.get("/status", response_class=HTMLResponse)
def status_page():
    s = compute_status()
    badge = ("<span class='badge ok'>OK</span>" if s["status"] == "ok"
             else "<span class='badge deg'>DEGRADED</span>")
    svc_rows = "".join(
        f"<tr><td class='mono'>{name}</td><td>{state}</td></tr>"
        for name, state in s["services"].items()
    )
    reasons = "".join(f"<li>{r}</li>" for r in s["degraded_reasons"])
    er = s["error_rate_5m"]
    bk = s["last_backup"]
    bk_txt = (f"{bk.get('file')} — {bk.get('timestamp')} "
              f"({_human_bytes(bk.get('size_bytes'))})" if bk else "belum ada")
    return f"""<!doctype html><html lang="id"><head><meta charset="utf-8">
<meta name="viewport" content="width=device-width,initial-scale=1">
<title>Status — Agentarium</title><style>{_STATUS_CSS}</style></head>
<body><div class="wrap">
<h1>Catatan Status Agentarium</h1>
<p>Status: {badge}</p>
{"<ul>" + reasons + "</ul>" if reasons else ""}
<table>
<tr><th>Ukuran</th><th>Nilai</th></tr>
<tr><td>Uptime proses</td><td class="mono">{s["uptime_seconds"]} dtk</td></tr>
<tr><td>DB latency</td><td class="mono">{s["db"]["latency_ms"]} ms</td></tr>
<tr><td>Ukuran DB</td><td class="mono">{s["db"]["size_human"] or "?"}</td></tr>
<tr><td>Error rate (5 mnt)</td><td class="mono">{er["rate"]*100:.2f}% "
f"({er["errors"]}/{er["total"]})</td></tr>
<tr><td>Backup terakhir</td><td class="mono">{bk_txt}</td></tr>
<tr><td>Diperiksa</td><td class="mono">{s["checked_at"]}</td></tr>
</table>
<h2>Layanan systemd</h2>
<table><tr><th>Service</th><th>Status</th></tr>{svc_rows}</table>
<p class="note">Halaman internal — bukan bagian viewer publik.
JSON: <a href="/v1/status">/v1/status</a> · Health: <a href="/v1/health">/v1/health</a></p>
</div></body></html>"""
