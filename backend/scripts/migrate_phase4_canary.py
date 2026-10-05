"""Migrasi Fase 4 — kolom injection canary di tabel agents (PRD §6.3a).

Additive + idempoten: hanya ADD COLUMN yang belum ada, lalu backfill NULL
ke nilai default. Berjalan di PostgreSQL (DATABASE_URL) maupun SQLite
(AGENTARIUM_DB). Tabel canary_probes / canary_results dibuat otomatis oleh
init_db() via Base.metadata.create_all.

CATATAN: backend juga menjalankan migrasi ini otomatis di lifespan, jadi
script ini umumnya hanya dibutuhkan untuk DB dev/SQLite manual.

Contoh:
    DATABASE_URL=postgresql+psycopg://... python scripts/migrate_phase4_canary.py
    AGENTARIUM_DB=/path/ke/agentarium.db python scripts/migrate_phase4_canary.py
"""
from __future__ import annotations

import sys
from pathlib import Path

# backend/ harus di sys.path agar `import database` / `import canary` bekerja
# saat script dijalankan dari mana saja.
BACKEND_DIR = Path(__file__).resolve().parent.parent
if str(BACKEND_DIR) not in sys.path:
    sys.path.insert(0, str(BACKEND_DIR))

from canary import ensure_canary_schema  # noqa: E402
from database import engine  # noqa: E402


if __name__ == "__main__":
    result = ensure_canary_schema(engine)
    print("migrasi canary fase 4 selesai:")
    for key, value in result.items():
        print(f"  {key}: {value}")
