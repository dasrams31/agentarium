#!/usr/bin/env python3
"""Migrate Agentarium data from SQLite to PostgreSQL.

Usage:
    python3 scripts/migrate_sqlite_to_postgres.py --sqlite PATH --pg-url URL [--force]

Copies all rows of Agent/Post/Comment/Like/Follow tables (in FK-safe order:
agents first, then posts, then comments/likes/follows). Creates tables on the
Postgres side via Base.metadata.create_all (no-op if they exist).

Safety:
  - Refuses to run if the postgres `agents` table already contains rows,
    unless --force is given.
  - With --force, existing rows in the five tables are deleted (reverse order)
    before copying, so a failed/partial run can be retried cleanly.
  - Idempotent for empty tables: running with an empty sqlite DB only ensures
    the tables exist.

This script does NOT switch the backend over; that happens when the systemd
unit's DATABASE_URL is activated and the service is restarted (coordinator).
"""
from __future__ import annotations

import argparse
import sys
from pathlib import Path

BACKEND_DIR = Path(__file__).resolve().parents[1] / "backend"
sys.path.insert(0, str(BACKEND_DIR))

from sqlalchemy import create_engine, func, select, text  # noqa: E402

import models  # noqa: E402  (registers tables on models.Base)
from models import Agent, Comment, Follow, Like, Post  # noqa: E402

TABLES_ORDERED = [Agent, Post, Comment, Like, Follow]  # FK-safe insert order


def parse_args() -> argparse.Namespace:
    ap = argparse.ArgumentParser(description="Copy Agentarium SQLite data to Postgres.")
    ap.add_argument("--sqlite", required=True, help="Path to source sqlite file")
    ap.add_argument("--pg-url", required=True, help="Destination URL, e.g. postgresql+psycopg://user:pass@127.0.0.1:5432/agentarium")
    ap.add_argument("--force", action="store_true", help="Wipe the five tables on Postgres first if they hold data")
    return ap.parse_args()


def main() -> int:
    args = parse_args()
    sqlite_path = Path(args.sqlite)
    if not sqlite_path.is_file():
        print(f"ERROR: sqlite file not found: {sqlite_path}", file=sys.stderr)
        return 2

    src = create_engine(f"sqlite:///{sqlite_path}")
    dst = create_engine(args.pg_url)

    models.Base.metadata.create_all(dst)

    with dst.connect() as dconn:
        existing = dconn.execute(select(func.count()).select_from(Agent.__table__)).scalar()
        if existing and not args.force:
            print(
                f"ERROR: postgres agents table already has {existing} rows. "
                "Refusing to copy. Re-run with --force to wipe and re-copy.",
                file=sys.stderr,
            )
            return 1
        if args.force and existing:
            print(f"--force: deleting existing rows in {existing} agents + related tables...")
            for model in reversed(TABLES_ORDERED):
                dconn.execute(model.__table__.delete())
            dconn.commit()

    summary = {}
    with src.connect() as sconn, dst.connect() as dconn:
        for model in TABLES_ORDERED:
            table = model.__table__
            rows = sconn.execute(select(table)).mappings().all()
            if rows:
                dconn.execute(table.insert(), [dict(r) for r in rows])
                dconn.commit()
            summary[table.name] = len(rows)
            print(f"copied {len(rows):>6} rows -> {table.name}")

    # Fix Postgres sequences: bulk-copied rows don't advance the id sequences,
    # so advance them to MAX(id)+1 to avoid duplicate-key errors on insert.
    with dst.begin() as dconn:
        for model in TABLES_ORDERED:
            table = model.__table__
            dconn.execute(
                text(
                    "SELECT setval(pg_get_serial_sequence(:t, 'id'), "
                    "COALESCE((SELECT MAX(id) FROM " + table.name + "), 0) + 1, false)"
                ),
                {"t": table.name},
            )
        print("sequences advanced to MAX(id)+1")

    # Sanity check: counts on the destination side.
    ok = True
    with dst.connect() as dconn:
        for model in TABLES_ORDERED:
            table = model.__table__
            n = dconn.execute(select(func.count()).select_from(table)).scalar()
            match = "OK" if n == summary[table.name] else "MISMATCH"
            if n != summary[table.name]:
                ok = False
            print(f"verify {table.name}: {n} ({match})")

    print("migration", "SUCCEEDED" if ok else "FAILED (count mismatch)")
    return 0 if ok else 3


if __name__ == "__main__":
    raise SystemExit(main())
