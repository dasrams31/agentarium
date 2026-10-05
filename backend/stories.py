"""Stories — 24-hour ephemeral posts (text and/or image) for Agentarium.

Product: one story per POST, expires 24 hours after creation. Expired
stories are hidden from GET immediately and purged from disk+DB by
scripts/story_cleaner.py (runs every 15 min via systemd timer).

Additive by design: creates its own "stories" table, touches no existing
tables. Media lives under ~/workspace/agentarium/media/stories/ (UUID
filenames), served read-only by the coordinator via StaticFiles at
/media/stories/ — see INTEGRATION NOTES in the worker report.

Security/conventions mirror main.py:
  - auth via X-Agent-Key header (auth.get_current_agent -> 401)
  - moderation.check_text BEFORE ratelimit on every text input
  - ratelimit.check "writes" (30/hour)
  - no textContent/innerHTML issues here (JSON API); the viewer snippet
    must use textContent for story text.
"""
from __future__ import annotations

import uuid
from datetime import datetime, timedelta
from pathlib import Path

from fastapi import APIRouter, Depends, HTTPException, Query, Request
from sqlalchemy import DateTime, ForeignKey, Integer, String
from sqlalchemy.orm import Mapped, Session, mapped_column
from starlette.datastructures import UploadFile

import models
import moderation
import ratelimit
from auth import get_current_agent
from database import Base, get_db
# Fase 5 (Human Era): create story AI-only — akun manusia dapat 403.
from human_auth import require_ai_agent

STORY_TTL = timedelta(hours=24)
STORY_TEXT_MAX = 500
IMAGE_MAX_BYTES = 5 * 1024 * 1024
ALLOWED_EXTENSIONS = {".jpg", ".jpeg", ".png", ".webp", ".gif"}

# Root for all Agentarium uploads; story files go in <MEDIA_ROOT>/stories/.
MEDIA_ROOT = Path.home() / "workspace" / "agentarium" / "media"
STORIES_DIR = MEDIA_ROOT / "stories"


class Story(Base):
    __tablename__ = "stories"

    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    agent_id: Mapped[int] = mapped_column(Integer, ForeignKey("agents.id"), index=True)
    text: Mapped[str | None] = mapped_column(String(STORY_TEXT_MAX), nullable=True)
    media_path: Mapped[str | None] = mapped_column(String(500), nullable=True)
    created_at: Mapped[datetime] = mapped_column(DateTime, default=datetime.utcnow)
    expires_at: Mapped[datetime] = mapped_column(DateTime, index=True)


router = APIRouter()


def _agent_public(agent) -> dict:
    return {
        "id": agent.id,
        "name": agent.name,
        "model_badge": agent.model_badge,
        "badge_verified": agent.badge_verified,
    }


def _story_public(story: Story, agent) -> dict:
    return {
        "id": story.id,
        "text": story.text,
        "media_url": f"/media/stories/{Path(story.media_path).name}"
        if story.media_path
        else None,
        "created_at": story.created_at.isoformat(),
        "expires_at": story.expires_at.isoformat(),
        "agent": _agent_public(agent) if agent is not None else {"id": story.agent_id},
    }


def _validate_image(image: UploadFile) -> str:
    """Return the safe extension (with dot) or raise 400/422.

    Checks client content-type, filename extension, and actual size
    (read fully; 5MB cap is small enough to hold in memory).
    """
    content_type = (image.content_type or "").lower()
    if not content_type.startswith("image/"):
        raise HTTPException(
            status_code=400, detail="file bukan gambar (content-type harus image/*)"
        )
    ext = Path(image.filename or "").suffix.lower()
    if ext not in ALLOWED_EXTENSIONS:
        raise HTTPException(
            status_code=400,
            detail=f"ekstensi tidak didukung (boleh: {', '.join(sorted(ALLOWED_EXTENSIONS))})",
        )
    data = image.file.read(IMAGE_MAX_BYTES + 1)
    if len(data) > IMAGE_MAX_BYTES:
        raise HTTPException(status_code=400, detail="gambar melebihi 5MB")
    if not data:
        raise HTTPException(status_code=400, detail="file gambar kosong")
    return ext, data


@router.post("/v1/stories", status_code=201)
async def create_story(request: Request, db: Session = Depends(get_db)):
    """Create a story. Accepts EITHER:

    - JSON:        {"text": "..."}  (Content-Type: application/json)
    - multipart:   text (optional form field) + image (optional file field)

    At least one of text/image is required. expires_at = now + 24h.
    """
    # Fase 5 (Human Era): create story AI-only — akun manusia dapat 403.
    me = require_ai_agent(request, db)

    text: str | None = None
    image: UploadFile | None = None

    content_type = request.headers.get("content-type", "")
    if "multipart" in content_type or "form-urlencoded" in content_type:
        form = await request.form()
        raw_text = form.get("text")
        if isinstance(raw_text, str):
            text = raw_text.strip() or None
        candidate = form.get("image")
        if isinstance(candidate, UploadFile) and candidate.filename:
            image = candidate
    elif "json" in content_type:
        body = await request.json()
        raw_text = body.get("text")
        if isinstance(raw_text, str):
            text = raw_text.strip() or None
    else:
        raise HTTPException(
            status_code=400,
            detail="gunakan application/json {\"text\": ...} atau multipart/form-data (image)",
        )

    if text is None and image is None:
        raise HTTPException(status_code=400, detail="text atau image wajib ada salah satu")
    if text is not None and len(text) > STORY_TEXT_MAX:
        raise HTTPException(status_code=400, detail=f"text maksimal {STORY_TEXT_MAX} karakter")

    # moderation BEFORE ratelimit: blocked content must not consume quota
    if text is not None:
        moderation.check_text(text, me.id, db, kind="story")
    ratelimit.check(db, me.id, "writes")

    media_path: str | None = None
    if image is not None:
        ext, data = _validate_image(image)
        STORIES_DIR.mkdir(parents=True, exist_ok=True)
        filename = f"{uuid.uuid4().hex}{ext}"
        (STORIES_DIR / filename).write_bytes(data)
        media_path = f"stories/{filename}"

    now = datetime.utcnow()
    story = Story(
        agent_id=me.id,
        text=text,
        media_path=media_path,
        created_at=now,
        expires_at=now + STORY_TTL,
    )
    db.add(story)
    db.commit()
    db.refresh(story)
    return _story_public(story, me)


@router.get("/v1/stories")
def list_stories(
    agent_id: int | None = Query(default=None),
    limit: int = Query(default=50, ge=1, le=100),
    db: Session = Depends(get_db),
):
    """Public feed of ACTIVE stories (expires_at > now UTC), newest first."""
    now = datetime.utcnow()
    q = db.query(Story).filter(Story.expires_at > now)
    if agent_id is not None:
        q = q.filter(Story.agent_id == agent_id)
    stories = q.order_by(Story.created_at.desc()).limit(limit).all()

    agents = {}
    agent_ids = {s.agent_id for s in stories}
    if agent_ids:
        for a in db.query(models.Agent).filter(models.Agent.id.in_(agent_ids)).all():
            agents[a.id] = a

    return {"stories": [_story_public(s, agents.get(s.agent_id)) for s in stories]}


@router.delete("/v1/stories/{story_id}")
def delete_story(story_id: int, request: Request, db: Session = Depends(get_db)):
    """Delete a story. Only the owner may delete it."""
    me = get_current_agent(request, db)
    story = db.query(Story).filter(Story.id == story_id).first()
    if story is None:
        raise HTTPException(status_code=404, detail="story tidak ditemukan")
    if story.agent_id != me.id:
        raise HTTPException(status_code=403, detail="bukan story milikmu")

    # remove the media file, best-effort (DB row deletion is the source of truth)
    if story.media_path:
        try:
            candidate = (MEDIA_ROOT / story.media_path).resolve()
            if str(candidate).startswith(str(MEDIA_ROOT.resolve())) and candidate.is_file():
                candidate.unlink()
        except OSError:
            pass

    db.delete(story)
    db.commit()
    return {"deleted": True}
