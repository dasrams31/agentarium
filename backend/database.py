"""Database setup — SQLAlchemy 2.0 style, portable to Postgres.

DB URL resolution order:
  1. env DATABASE_URL (used as-is, e.g. postgresql+psycopg://user:pass@host/db)
  2. env AGENTARIUM_DB (sqlite file path)
  3. default ~/workspace/agentarium/data/agentarium.db (sqlite)
Uses only generic column types so the same models work on Postgres later.
"""
from __future__ import annotations

import os
from pathlib import Path

from sqlalchemy import create_engine
from sqlalchemy.orm import DeclarativeBase, sessionmaker

DEFAULT_DB_PATH = Path.home() / "workspace" / "agentarium" / "data" / "agentarium.db"


def _db_url() -> str:
    # If DATABASE_URL is set, use it as-is (e.g. postgresql+psycopg://...).
    pg = os.environ.get("DATABASE_URL")
    if pg:
        return pg
    raw = os.environ.get("AGENTARIUM_DB")
    if raw:
        p = Path(raw).expanduser()
    else:
        p = DEFAULT_DB_PATH
    p.parent.mkdir(parents=True, exist_ok=True)
    return f"sqlite:///{p}"


engine = create_engine(_db_url(), future=True)

SessionLocal = sessionmaker(bind=engine, autoflush=False, expire_on_commit=False)


class Base(DeclarativeBase):
    pass


def get_db():
    """FastAPI dependency: yields a session, closes it afterwards."""
    db = SessionLocal()
    try:
        yield db
    finally:
        db.close()


def init_db() -> None:
    """Create all tables (no-op if they exist)."""
    # Import models so Base.metadata knows every table before create_all.
    import models  # noqa: F401

    Base.metadata.create_all(engine)
