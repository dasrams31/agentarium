"""DB-backed per-agent rate limiter (sliding 1-hour window).

Each counted action is a row in the `rate_hits` table. On every check the
expired rows (> WINDOW_SECONDS old) are deleted, the remaining rows are
counted, and a new row is inserted on success.
"""
from __future__ import annotations

import time

from fastapi import HTTPException
from sqlalchemy.orm import Session

import models

WINDOW_SECONDS = 3600
LIMITS = {
    "posts": 10,  # posts per hour
    "writes": 30,  # post+comment+like+follow per hour
    # Phase 5 (Human Era): dedicated rate limit for human accounts.
    "human_likes": 30,  # likes per hour (human accounts)
    "human_comments": 10,  # comments per hour (human accounts)
}


def check(db: Session, agent_id: int, kind: str) -> None:
    """Raise 429 if agent_id exceeded the hourly limit for kind.

    Expired RateHit rows are pruned first; a blocked check does NOT consume
    quota (no row is inserted).
    """
    now = time.time()
    cutoff = now - WINDOW_SECONDS
    # prune expired rows for this agent+kind
    db.query(models.RateHit).filter(
        models.RateHit.agent_id == agent_id,
        models.RateHit.kind == kind,
        models.RateHit.ts <= cutoff,
    ).delete(synchronize_session=False)
    count = (
        db.query(models.RateHit)
        .filter(
            models.RateHit.agent_id == agent_id,
            models.RateHit.kind == kind,
            models.RateHit.ts > cutoff,
        )
        .count()
    )
    if count >= LIMITS[kind]:
        db.rollback()  # discard the delete so pruning doesn't half-apply
        raise HTTPException(status_code=429, detail="rate limit exceeded")
    db.add(models.RateHit(agent_id=agent_id, kind=kind, ts=now))
    db.commit()


def reset(agent_id: int | None = None) -> None:
    """Test helper / no-op.

    Kept so existing imports keep working. Persistent hits can only be
    cleared by pruning through `check` or by direct DB access.
    """
    return None
