#!/usr/bin/env python3
"""Purge expired Agentarium stories from the DB and delete their media files.

Safe to run any time: selects rows with expires_at <= now (UTC), deletes
their image files (confined to MEDIA_ROOT), then deletes the rows.

Intended to run every 15 minutes via agentarium-story-cleaner.timer.
Can also be run manually:

    cd ~/workspace/agentarium/backend && ./.venv/bin/python scripts/story_cleaner.py
"""
from __future__ import annotations

import sys
from datetime import datetime
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from database import SessionLocal  # noqa: E402

import models  # noqa: E402,F401  (register agents/rate tables on Base)
import stories  # noqa: E402,F401  (register Story on Base)

MEDIA_ROOT = stories.MEDIA_ROOT


def clean_expired() -> tuple[int, int]:
    """Return (stories_deleted, files_deleted)."""
    db = SessionLocal()
    try:
        now = datetime.utcnow()
        expired = db.query(stories.Story).filter(stories.Story.expires_at <= now).all()
        stories_deleted = 0
        files_deleted = 0
        for s in expired:
            if s.media_path:
                try:
                    candidate = (MEDIA_ROOT / s.media_path).resolve()
                    # Never delete outside the media tree.
                    if str(candidate).startswith(str(MEDIA_ROOT.resolve())) and candidate.is_file():
                        candidate.unlink()
                        files_deleted += 1
                except OSError as e:
                    print(f"[story_cleaner] gagal hapus file {s.media_path}: {e}")
            db.delete(s)
            stories_deleted += 1
        db.commit()
        return stories_deleted, files_deleted
    finally:
        db.close()


def main() -> None:
    stories_deleted, files_deleted = clean_expired()
    print(
        f"[story_cleaner] {datetime.utcnow().isoformat()}Z "
        f"stories_terhapus={stories_deleted} file_terhapus={files_deleted}"
    )


if __name__ == "__main__":
    main()
