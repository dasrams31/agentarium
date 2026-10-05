"""Migrasi Fase 2 — kolom profil agen (worker 7).

JANGAN dijalankan terhadap DB produksi tanpa koordinasi — koordinator yang
menjalankan, sekali, saat deploy. Idempoten: aman dijalankan ulang.

Urutan langkah (PENTING):
  1. ALTER TABLE agents ADD COLUMN untuk keempat kolom (hanya yang belum ada).
  2. Backfill: handle (normalisasi name + angka bila konflik), display_name = name,
     bio = "", wild_opt_in = False.
  3. CREATE UNIQUE INDEX pada handle — SETELAH backfill, agar tidak ada NULL
     ganda/konflik yang menggagalkan index.

Berjalan di PostgreSQL (DATABASE_URL) maupun SQLite (AGENTARIUM_DB).

Contoh:
    DATABASE_URL=postgresql+psycopg://... python scripts/migrate_phase2_profiles.py
    AGENTARIUM_DB=/path/ke/agentarium.db python scripts/migrate_phase2_profiles.py
"""
from __future__ import annotations

import re
import sys
from pathlib import Path

# backend/ harus di sys.path agar `import database` / `import profiles` bekerja
# saat script dijalankan dari mana saja.
BACKEND_DIR = Path(__file__).resolve().parent.parent
if str(BACKEND_DIR) not in sys.path:
    sys.path.insert(0, str(BACKEND_DIR))

from sqlalchemy import inspect, text  # noqa: E402

from database import engine  # noqa: E402

_HANDLE_SANITIZE_RE = re.compile(r"[^a-z0-9_]")


def _normalize_base(name: str) -> str:
    return _HANDLE_SANITIZE_RE.sub("", (name or "").lower()) or "agent"


# DDL per kolom, per dialek.
COLUMNS: dict[str, dict[str, str]] = {
    "handle": {
        "postgresql": "VARCHAR(40)",
        "sqlite": "VARCHAR(40)",
    },
    "display_name": {
        "postgresql": "VARCHAR(40)",
        "sqlite": "VARCHAR(40)",
    },
    "bio": {
        "postgresql": "VARCHAR(300)",
        "sqlite": "VARCHAR(300)",
    },
    "wild_opt_in": {
        "postgresql": "BOOLEAN DEFAULT FALSE",
        "sqlite": "BOOLEAN DEFAULT 0",
    },
}

UNIQUE_INDEX_SQL = {
    "postgresql": (
        "CREATE UNIQUE INDEX IF NOT EXISTS ux_agents_handle "
        "ON agents (handle)"
    ),
    "sqlite": (
        "CREATE UNIQUE INDEX IF NOT EXISTS ux_agents_handle "
        "ON agents (handle)"
    ),
}


def migrate() -> dict:
    dialect = engine.dialect.name
    if dialect not in ("postgresql", "sqlite"):
        raise RuntimeError(f"dialek tidak didukung: {dialect}")

    report = {"added_columns": [], "backfilled_handles": 0, "index": False}

    with engine.begin() as conn:
        existing_cols = {
            c["name"] for c in inspect(conn).get_columns("agents")
        }

        # 1. ALTER — hanya kolom yang belum ada (idempoten di kedua dialek;
        #    SQLite tidak mendukung IF NOT EXISTS pada ADD COLUMN).
        for col, ddls in COLUMNS.items():
            if col in existing_cols:
                continue
            conn.execute(
                text(f"ALTER TABLE agents ADD COLUMN {col} {ddls[dialect]}")
            )
            report["added_columns"].append(col)

        # 2. Backfill. Kumpulkan handle yang sudah terisi untuk deteksi konflik.
        rows = conn.execute(
            text("SELECT id, name, handle FROM agents ORDER BY id")
        ).all()
        used: set[str] = set()
        for _id, _name, handle in rows:
            if handle:
                used.add(str(handle))

        for _id, _name, handle in rows:
            display_name = _name  # display_name = name bila null
            bio = ""              # bio = "" bila null
            new_handle = handle
            if not handle:
                base = _normalize_base(_name or "")
                candidate = base
                n = 2
                while candidate in used:
                    candidate = f"{base}{n}"
                    n += 1
                new_handle = candidate
                used.add(candidate)
                report["backfilled_handles"] += 1
            conn.execute(
                text(
                    "UPDATE agents SET handle = :handle, "
                    "display_name = COALESCE(display_name, :display_name), "
                    "bio = COALESCE(bio, :bio), "
                    "wild_opt_in = COALESCE(wild_opt_in, :wild) "
                    "WHERE id = :id"
                ),
                {
                    "handle": new_handle,
                    "display_name": display_name,
                    "bio": bio,
                    "wild": False,
                    "id": _id,
                },
            )

        # 3. Unique index pada handle — SETELAH backfill.
        conn.execute(text(UNIQUE_INDEX_SQL[dialect]))
        report["index"] = True

    return report


if __name__ == "__main__":
    result = migrate()
    print("migrasi profil fase 2 selesai:")
    for key, value in result.items():
        print(f"  {key}: {value}")
