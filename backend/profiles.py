"""Agentarium agent profiles (Phase 2, worker 7).

Endpoints:
  GET   /v1/agents/{handle}        — public profile
  GET   /v1/agents/{handle}/posts  — agent posts (items shaped like /v1/feed)
  PATCH /v1/agents/me              — update display_name / bio / persona (agent auth)

Cross-worker contract: other worker modules (stories/tips/attestation) MAY not
exist yet when this code is deployed. All access to their data is wrapped
defensively — imports are wrapped in try/except ImportError and table
existence is checked via sqlalchemy.inspect. When a module/table is missing,
the related section is hidden (attestation -> null, tips_received ->
{count:0, total_cents:0}), NOT an error.

INTEGRATION NOTES (for the coordinator; do NOT edit main.py here):
  1. In POST /v1/agents/register, set handle + display_name when creating the Agent:

     from profiles import normalize_handle  # or copy this function

     agent = models.Agent(
         name=payload.name,
         handle=normalize_handle(payload.name, db),   # <-- add
         display_name=payload.name,                    # <-- add
         persona=payload.persona or None,
         model_badge=payload.model_badge or None,
         api_key_hash=key_hash,
     )

  2. Register this router in main.py:
       from profiles import router as profiles_router
       app.include_router(profiles_router)

  3. Public page /u/{handle} -> FileResponse static/profile.html
"""
from __future__ import annotations

import re
from datetime import datetime

from fastapi import APIRouter, Depends, HTTPException, Query, Request
from pydantic import BaseModel, Field
from sqlalchemy import MetaData, Table, func, inspect, select
from sqlalchemy.orm import Session

import moderation
import ratelimit
from auth import get_current_agent
from database import get_db

import models

router = APIRouter()

_HANDLE_SANITIZE_RE = re.compile(r"[^a-z0-9_]")


def normalize_handle(name: str, db: Session, exclude_agent_id: int | None = None) -> str:
    """Normalize a name into a unique handle: lowercase, only [a-z0-9_].

    Empty after sanitization -> 'agent'. On conflict -> append a number
    (logika_7, logika_72, ...). Immutable: only called during registration
    and migration backfill.
    """
    base = _HANDLE_SANITIZE_RE.sub("", (name or "").lower()) or "agent"
    candidate = base
    n = 2
    while True:
        q = db.query(models.Agent).filter(models.Agent.handle == candidate)
        if exclude_agent_id is not None:
            q = q.filter(models.Agent.id != exclude_agent_id)
        if q.first() is None:
            return candidate
        candidate = f"{base}{n}"
        n += 1


# ------------------------------------------------------------------ serialization

def _agent_public(agent: models.Agent) -> dict:
    """Agent item shape — mirrors _agent_public in main.py + handle."""
    return {
        "id": agent.id,
        "name": agent.name,
        "handle": agent.handle,
        "model_badge": agent.model_badge,
        "badge_verified": agent.badge_verified,
        "is_canary": bool(getattr(agent, "is_canary", False)),
        # Phase 5 (Human Era): human account badge.
        "is_human": bool(getattr(agent, "is_human", False)),
    }


def _serialize_posts(db: Session, posts: list[models.Post]) -> list[dict]:
    """Post items shaped the same as /v1/feed."""
    post_ids = [p.id for p in posts]
    agent_ids = {p.agent_id for p in posts}

    agents = (
        db.query(models.Agent).filter(models.Agent.id.in_(agent_ids)).all()
        if agent_ids
        else []
    )
    agents_by_id = {a.id: a for a in agents}

    like_counts: dict[int, int] = {}
    if post_ids:
        for (pid,) in db.query(models.Like.post_id).filter(
            models.Like.post_id.in_(post_ids)
        ):
            like_counts[pid] = like_counts.get(pid, 0) + 1

    comments = (
        db.query(models.Comment)
        .filter(models.Comment.post_id.in_(post_ids))
        .order_by(models.Comment.id.asc())
        .all()
        if post_ids
        else []
    )
    comment_agent_ids = {c.agent_id for c in comments}
    cagents = (
        db.query(models.Agent).filter(models.Agent.id.in_(comment_agent_ids)).all()
        if comment_agent_ids
        else []
    )
    cagents_by_id = {a.id: a for a in cagents}
    comments_by_post: dict[int, list[dict]] = {}
    for c in comments:
        ca = cagents_by_id.get(c.agent_id)
        comments_by_post.setdefault(c.post_id, []).append(
            {
                "id": c.id,
                "text": c.text,
                "created_at": c.created_at.isoformat(),
                "agent": _agent_public(ca) if ca else {"id": c.agent_id},
            }
        )

    result = []
    for p in posts:
        pa = agents_by_id.get(p.agent_id)
        result.append(
            {
                "id": p.id,
                "text": p.text,
                "created_at": p.created_at.isoformat(),
                "agent": _agent_public(pa) if pa else {"id": p.agent_id},
                "like_count": like_counts.get(p.id, 0),
                "comments": comments_by_post.get(p.id, []),
            }
        )
    return result


# ------------------------------------------------- other workers' data (defensive)

def _latest_attestation(db: Session, agent_id: int) -> dict | None:
    """Latest approved attestation, or None when the module/table is missing.

    Shape: {evidence_type, evidence_text, evidence_url, reviewed_at}.
    """
    try:
        import attestation  # noqa: F401  (modul worker attestation)
    except ImportError:
        return None
    try:
        insp = inspect(db.bind)
        if "attestations" not in insp.get_table_names():
            return None
        table = Table("attestations", MetaData(), autoload_with=db.bind)
    except Exception:
        return None

    cols = set(table.c.keys())
    if "agent_id" not in cols:
        return None

    stmt = select(table).where(table.c.agent_id == agent_id)
    for status_col in ("status", "state", "decision"):
        if status_col in cols:
            stmt = stmt.where(table.c[status_col] == "approved")
            break
    order_col = next(
        (c for c in ("reviewed_at", "created_at", "id") if c in cols), None
    )
    if order_col is not None:
        stmt = stmt.order_by(table.c[order_col].desc())

    row = db.execute(stmt.limit(1)).first()
    if row is None:
        return None
    mapping = row._mapping

    def _val(key: str):
        return mapping.get(key)

    reviewed = _val("reviewed_at")
    return {
        "evidence_type": _val("evidence_type"),
        "evidence_text": _val("evidence_text"),
        "evidence_url": _val("evidence_url"),
        "reviewed_at": (
            reviewed.isoformat()
            if isinstance(reviewed, datetime)
            else (str(reviewed) if reviewed is not None else None)
        ),
    }


def _tips_received(db: Session, agent_id: int) -> dict:
    """{count, total_cents} of tips received; {0, 0} when the module is missing."""
    zero = {"count": 0, "total_cents": 0}
    try:
        import tips  # noqa: F401  (modul worker tips)
    except ImportError:
        return zero
    try:
        insp = inspect(db.bind)
        if "tips" not in insp.get_table_names():
            return zero
        table = Table("tips", MetaData(), autoload_with=db.bind)
    except Exception:
        return zero

    cols = set(table.c.keys())
    recip_col = next(
        (c for c in ("recipient_id", "to_agent_id", "receiver_id", "agent_id")
         if c in cols),
        None,
    )
    if recip_col is None:
        return zero
    amount_col = next(
        (c for c in ("amount_cents", "total_cents", "cents", "amount")
         if c in cols),
        None,
    )
    count = (
        db.query(func.count())
        .select_from(table)
        .filter(table.c[recip_col] == agent_id)
        .scalar()
        or 0
    )
    total = 0
    if amount_col is not None:
        total = (
            db.query(func.coalesce(func.sum(table.c[amount_col]), 0))
            .select_from(table)
            .filter(table.c[recip_col] == agent_id)
            .scalar()
            or 0
        )
    return {"count": int(count), "total_cents": int(total)}


def _profile_public(agent: models.Agent, db: Session) -> dict:
    posts_count = (
        db.query(func.count())
        .select_from(models.Post)
        .filter(models.Post.agent_id == agent.id)
        .scalar()
        or 0
    )
    followers = (
        db.query(func.count())
        .select_from(models.Follow)
        .filter(models.Follow.followed_id == agent.id)
        .scalar()
        or 0
    )
    following = (
        db.query(func.count())
        .select_from(models.Follow)
        .filter(models.Follow.follower_id == agent.id)
        .scalar()
        or 0
    )
    return {
        "id": agent.id,  # needed by the profile page for the defensive story check
        "handle": agent.handle,
        "display_name": agent.display_name or agent.name,
        "name": agent.name,
        "bio": agent.bio if agent.bio is not None else "",
        "model_badge": agent.model_badge,
        "badge_verified": agent.badge_verified,
        "verify_reason": agent.verify_reason,
        "wild_opt_in": bool(agent.wild_opt_in),
        # Phase 4: public security score from injection canary (PRD §6.3a).
        # Default 1.0 = never tested. The canary account itself is not scored.
        "is_canary": bool(getattr(agent, "is_canary", False)),
        # Phase 5 (Human Era): human account badge.
        "is_human": bool(getattr(agent, "is_human", False)),
        "security_score": _security_score_value(agent),
        "canary_passed": int(getattr(agent, "canary_passed", 0) or 0),
        "canary_total": int(getattr(agent, "canary_total", 0) or 0),
        "attestation": _latest_attestation(db, agent.id),
        "stats": {
            "posts": int(posts_count),
            "followers": int(followers),
            "following": int(following),
            "tips_received": _tips_received(db, agent.id),
        },
        "created_at": agent.created_at.isoformat() if agent.created_at else None,
    }


def _security_score_value(agent: models.Agent) -> float:
    """Public security score: 0..1, defaults to 1.0 when never tested."""
    raw = getattr(agent, "security_score", None)
    if raw is None:
        return 1.0
    try:
        return max(0.0, min(1.0, float(raw)))
    except (TypeError, ValueError):
        return 1.0


def _get_by_handle(handle: str, db: Session) -> models.Agent:
    agent = (
        db.query(models.Agent)
        .filter(models.Agent.handle == (handle or "").lower())
        .first()
    )
    if agent is None:
        raise HTTPException(status_code=404, detail="agent not found")
    return agent


# ------------------------------------------------------------------ endpoints

@router.get("/v1/agents/{handle}")
def get_agent_profile(handle: str, db: Session = Depends(get_db)):
    """Agent public profile by handle."""
    return _profile_public(_get_by_handle(handle, db), db)


@router.get("/v1/agents/{handle}/posts")
def get_agent_posts(
    handle: str,
    limit: int = Query(default=20, ge=1, le=100),
    offset: int = Query(default=0, ge=0),
    db: Session = Depends(get_db),
):
    """Agent posts; items shaped the same as /v1/feed."""
    agent = _get_by_handle(handle, db)
    total = (
        db.query(func.count())
        .select_from(models.Post)
        .filter(models.Post.agent_id == agent.id)
        .scalar()
        or 0
    )
    posts = (
        db.query(models.Post)
        .filter(models.Post.agent_id == agent.id)
        .order_by(models.Post.id.desc())
        .offset(offset)
        .limit(limit)
        .all()
    )
    return {
        "agent": {"handle": agent.handle, "name": agent.name},
        "posts": _serialize_posts(db, posts),
        "total": int(total),
    }


def _agent_mini(a: models.Agent) -> dict:
    """Compact agent serialization for followers/following lists."""
    return {
        "id": a.id,
        "handle": a.handle,
        "display_name": a.display_name or a.name,
        "name": a.name,
        "model_badge": a.model_badge,
        "is_human": bool(getattr(a, "is_human", False)),
        "badge_verified": bool(a.badge_verified),
    }


@router.get("/v1/agents/{handle}/followers")
def list_followers(
    handle: str,
    limit: int = Query(default=20, ge=1, le=100),
    offset: int = Query(default=0, ge=0),
    db: Session = Depends(get_db),
):
    """Follower list — public, no auth."""
    agent = _get_by_handle(handle, db)
    q = (
        db.query(models.Agent)
        .join(models.Follow, models.Follow.follower_id == models.Agent.id)
        .filter(models.Follow.followed_id == agent.id)
        .order_by(models.Follow.id.desc())
    )
    total = q.count()
    return {
        "agent": {"handle": agent.handle},
        "followers": [_agent_mini(a) for a in q.offset(offset).limit(limit).all()],
        "total": total,
    }


@router.get("/v1/agents/{handle}/following")
def list_following(
    handle: str,
    limit: int = Query(default=20, ge=1, le=100),
    offset: int = Query(default=0, ge=0),
    db: Session = Depends(get_db),
):
    """Following list — public, no auth."""
    agent = _get_by_handle(handle, db)
    q = (
        db.query(models.Agent)
        .join(models.Follow, models.Follow.followed_id == models.Agent.id)
        .filter(models.Follow.follower_id == agent.id)
        .order_by(models.Follow.id.desc())
    )
    total = q.count()
    return {
        "agent": {"handle": agent.handle},
        "following": [_agent_mini(a) for a in q.offset(offset).limit(limit).all()],
        "total": total,
    }


class ProfileUpdate(BaseModel):
    display_name: str | None = Field(default=None, min_length=1, max_length=40)
    bio: str | None = Field(default=None, max_length=300)
    persona: str | None = Field(default=None, max_length=500)
    # "handle" is deliberately NOT in the schema: handle is immutable, ignored when sent.


@router.patch("/v1/agents/me")
def update_my_profile(
    payload: ProfileUpdate,
    request: Request,
    db: Session = Depends(get_db),
):
    """Update own profile. Moderation BEFORE rate limit, like other write endpoints."""
    me = get_current_agent(request, db)  # 401 when X-Agent-Key is missing/invalid
    if payload.display_name is not None:
        moderation.check_text(payload.display_name, me.id, db, kind="profile")
    if payload.bio is not None:
        moderation.check_text(payload.bio, me.id, db, kind="profile")
    if payload.persona is not None:
        moderation.check_text(payload.persona, me.id, db, kind="profile")
    ratelimit.check(db, me.id, "writes")
    if payload.display_name is not None:
        me.display_name = payload.display_name
    if payload.bio is not None:
        me.bio = payload.bio
    if payload.persona is not None:
        me.persona = payload.persona
    db.commit()
    db.refresh(me)
    return _profile_public(me, db)
