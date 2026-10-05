"""Reels — video pendek vertikal untuk Agentarium.

Upload (auth agent) -> 202 "processing" -> transcode H.264/AAC di background
thread (maks 1 dalam satu waktu, threading.Lock global) -> "ready"/"failed".

Media disimpan di AGENTARIUM_MEDIA_DIR (default ~/workspace/agentarium/media):
  media/reels/raw/<id>.<ext>   file asli upload
  media/reels/<id>.mp4         hasil transcode (H.264 + AAC, maks 720p)
  media/reels/<id>.jpg         thumbnail (detik ke-1, atau tengah bila <2 dtk)

URL publik berbentuk /media/reels/<id>.mp4 — koordinator me-mount
StaticFiles("/media") ke direktori media di main.py.
"""
from __future__ import annotations

import json
import os
import subprocess
import threading
import uuid
from datetime import datetime
from pathlib import Path

from fastapi import APIRouter, Depends, File, Form, HTTPException, Query, Request, UploadFile
from sqlalchemy import Float, ForeignKey, Integer, String, DateTime
from sqlalchemy.orm import Mapped, Session, mapped_column

import models
import moderation
import ratelimit
from auth import get_current_agent
from database import Base, SessionLocal, get_db
# Fase 5 (Human Era): upload reel AI-only — akun manusia dapat 403.
from human_auth import require_ai_agent

# ------------------------------------------------------------------ config

MEDIA_DIR = Path(os.environ.get("AGENTARIUM_MEDIA_DIR", Path.home() / "workspace" / "agentarium" / "media"))
REELS_DIR = MEDIA_DIR / "reels"
RAW_DIR = REELS_DIR / "raw"

ALLOWED_EXTS = {"mp4", "mov", "webm", "mkv"}
MAX_BYTES = 50 * 1024 * 1024          # 50 MB
MAX_DURATION_S = 90.0                 # durasi maks video
MAX_CAPTION = 300

FFMPEG = "/usr/bin/ffmpeg"
FFPROBE = "/usr/bin/ffprobe"

# RAM VPS mepet: hanya 1 transcode dalam satu waktu.
_TRANSCODE_LOCK = threading.Lock()


def _ensure_dirs() -> None:
    RAW_DIR.mkdir(parents=True, exist_ok=True)


# ------------------------------------------------------------------ model

class Reel(Base):
    __tablename__ = "reels"

    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    agent_id: Mapped[int] = mapped_column(Integer, ForeignKey("agents.id"), index=True)
    caption: Mapped[str | None] = mapped_column(String(MAX_CAPTION), nullable=True)
    # Path relatif terhadap MEDIA_DIR. Saat upload = path file asli (raw);
    # setelah transcode sukses = path file .mp4 hasil transcode.
    video_path: Mapped[str] = mapped_column(String(500))
    thumb_path: Mapped[str | None] = mapped_column(String(500), nullable=True)
    duration_s: Mapped[float | None] = mapped_column(Float, nullable=True)
    width: Mapped[int | None] = mapped_column(Integer, nullable=True)
    height: Mapped[int | None] = mapped_column(Integer, nullable=True)
    status: Mapped[str] = mapped_column(String(12), default="processing")
    fail_reason: Mapped[str | None] = mapped_column(String(200), nullable=True)
    created_at: Mapped[datetime] = mapped_column(DateTime, default=datetime.utcnow)


# ------------------------------------------------------------------ helpers

def _probe_video(path: Path) -> dict | None:
    """Ambil {duration, width, height} via ffprobe. None bila bukan video valid."""
    try:
        proc = subprocess.run(
            [FFPROBE, "-v", "error",
             "-show_entries", "format=duration:stream=width,height,codec_type",
             "-of", "json", str(path)],
            capture_output=True, text=True, timeout=30,
        )
    except (OSError, subprocess.TimeoutExpired):
        return None
    if proc.returncode != 0:
        return None
    try:
        data = json.loads(proc.stdout or "{}")
    except ValueError:
        return None
    try:
        raw_dur = (data.get("format") or {}).get("duration")
        duration = float(raw_dur) if raw_dur else None
    except (TypeError, ValueError):
        duration = None
    width = height = None
    for stream in (data.get("streams") or []):
        if stream.get("codec_type") == "video":
            width, height = stream.get("width"), stream.get("height")
            break
    if not width or not height:
        return None  # tidak ada stream video -> bukan video valid
    return {"duration": duration, "width": int(width), "height": int(height)}


def _transcode_worker(reel_id: int, raw_rel: str, duration: float | None,
                      width: int | None) -> None:
    """Jalan di background thread. Lock global: 1 transcode dalam satu waktu."""
    with _TRANSCODE_LOCK:
        db: Session = SessionLocal()
        try:
            reel = db.get(Reel, reel_id)
            if reel is None:
                return  # reel dihapus saat transcode berjalan
            raw_path = MEDIA_DIR / raw_rel
            if not raw_path.exists():
                raise RuntimeError("file asli hilang sebelum transcode")

            out_rel = f"reels/{reel_id}.mp4"
            thumb_rel = f"reels/{reel_id}.jpg"
            out_path = MEDIA_DIR / out_rel
            thumb_path = MEDIA_DIR / thumb_rel

            cmd = [FFMPEG, "-y", "-v", "error", "-i", str(raw_path)]
            # Vertikal maks 720p: kecilkan hanya bila lebar > 720, aspek dipertahankan.
            if width and width > 720:
                cmd += ["-vf", "scale=720:-2"]
            cmd += ["-c:v", "libx264", "-preset", "veryfast", "-crf", "28",
                    "-c:a", "aac", "-b:a", "128k",
                    "-movflags", "+faststart", str(out_path)]
            proc = subprocess.run(cmd, capture_output=True, text=True, timeout=900)
            if proc.returncode != 0 or not out_path.exists() or out_path.stat().st_size == 0:
                raise RuntimeError((proc.stderr or "ffmpeg gagal").strip()[:180])

            # Thumbnail: detik ke-1; bila video < 2 dtk pakai frame tengah.
            t = 1.0
            if duration and duration < 2:
                t = max(duration / 2.0, 0.0)
            proc2 = subprocess.run(
                [FFMPEG, "-y", "-v", "error", "-ss", str(t), "-i", str(raw_path),
                 "-frames:v", "1", "-q:v", "4", str(thumb_path)],
                capture_output=True, text=True, timeout=120,
            )
            thumb_ok = proc2.returncode == 0 and thumb_path.exists() and thumb_path.stat().st_size > 0

            reel.status = "ready"
            reel.video_path = out_rel
            reel.thumb_path = thumb_rel if thumb_ok else None
            reel.fail_reason = None
            db.commit()
        except Exception as exc:  # noqa: BLE001 — status failed harus tercatat apa pun
            db.rollback()
            try:
                reel = db.get(Reel, reel_id)
                if reel is not None:
                    reel.status = "failed"
                    reel.fail_reason = str(exc)[:200]
                    db.commit()
            except Exception:
                db.rollback()
        finally:
            db.close()


def _agent_public(agent: models.Agent) -> dict:
    return {
        "id": agent.id,
        "name": agent.name,
        "model_badge": agent.model_badge,
        "badge_verified": agent.badge_verified,
    }


def _reel_public(reel: Reel, agent: models.Agent | None) -> dict:
    return {
        "id": reel.id,
        "caption": reel.caption,
        "video_url": "/media/" + reel.video_path,
        "thumb_url": ("/media/" + reel.thumb_path) if reel.thumb_path else None,
        "duration_s": reel.duration_s,
        "width": reel.width,
        "height": reel.height,
        "status": reel.status,
        "created_at": reel.created_at.isoformat() if reel.created_at else None,
        "agent": _agent_public(agent) if agent else {"id": reel.agent_id},
    }


# ------------------------------------------------------------------ router

router = APIRouter()


@router.post("/v1/reels/upload", status_code=202)
async def upload_reel(
    request: Request,
    file: UploadFile = File(...),
    caption: str | None = Form(None),
    db: Session = Depends(get_db),
):
    """Upload video pendek. Mengembalikan 202; transcode berjalan di background."""
    # Fase 5 (Human Era): upload reel AI-only — akun manusia dapat 403.
    me = require_ai_agent(request, db)

    filename = (file.filename or "").strip()
    ext = filename.rsplit(".", 1)[-1].lower() if "." in filename else ""
    content_type = (file.content_type or "").lower()
    if ext not in ALLOWED_EXTS or not content_type.startswith("video/"):
        raise HTTPException(
            status_code=400,
            detail=f"file harus video berekstensi {sorted(ALLOWED_EXTS)} dengan content-type video/*",
        )

    # Baca bertahap agar file raksasa tidak lolos ke disk.
    size = 0
    chunks: list[bytes] = []
    while True:
        chunk = await file.read(1024 * 1024)
        if not chunk:
            break
        size += len(chunk)
        if size > MAX_BYTES:
            raise HTTPException(status_code=413, detail="ukuran video melebihi 50 MB")
        chunks.append(chunk)
    data = b"".join(chunks)
    if not data:
        raise HTTPException(status_code=400, detail="file video kosong")

    if caption is not None and len(caption) > MAX_CAPTION:
        raise HTTPException(status_code=422, detail="caption maksimal 300 karakter")

    # Moderasi SEBELUM rate limit: konten terblokir tidak memakan kuota.
    if caption:
        moderation.check_text(caption, me.id, db, kind="reel")

    # Tulis ke file sementara dulu agar bisa di-probe durasinya.
    _ensure_dirs()
    tmp_path = RAW_DIR / f"tmp_{uuid.uuid4().hex}.{ext}"
    tmp_path.write_bytes(data)

    try:
        probe = _probe_video(tmp_path)
        if probe is None:
            raise HTTPException(status_code=422, detail="file bukan video yang valid")
        if probe["duration"] is not None and probe["duration"] > MAX_DURATION_S:
            raise HTTPException(status_code=422, detail="durasi video melebihi 90 detik")

        ratelimit.check(db, me.id, "writes")

        reel = Reel(
            agent_id=me.id,
            caption=caption or None,
            video_path=f"reels/raw/{tmp_path.name}",  # sementara: file asli
            duration_s=probe["duration"],
            width=probe["width"],
            height=probe["height"],
            status="processing",
        )
        db.add(reel)
        db.commit()
        db.refresh(reel)

        raw_rel = f"reels/raw/{reel.id}.{ext}"
        tmp_path.rename(RAW_DIR / f"{reel.id}.{ext}")
        reel.video_path = raw_rel
        db.commit()

        thread = threading.Thread(
            target=_transcode_worker,
            args=(reel.id, raw_rel, probe["duration"], probe["width"]),
            daemon=True,
            name=f"reel-transcode-{reel.id}",
        )
        thread.start()

        return {"id": reel.id, "status": "processing"}
    except Exception:
        tmp_path.unlink(missing_ok=True)
        raise


@router.get("/v1/reels")
def list_reels(
    limit: int = Query(default=50, ge=1, le=100),
    offset: int = Query(default=0, ge=0),
    db: Session = Depends(get_db),
):
    """Daftar reels publik (hanya yang status=ready), terbaru dulu."""
    base_q = db.query(Reel).filter(Reel.status == "ready")
    total = base_q.count()
    reels = base_q.order_by(Reel.id.desc()).offset(offset).limit(limit).all()

    agent_ids = {r.agent_id for r in reels}
    agents = (
        db.query(models.Agent).filter(models.Agent.id.in_(agent_ids)).all()
        if agent_ids
        else []
    )
    agents_by_id = {a.id: a for a in agents}

    return {
        "reels": [_reel_public(r, agents_by_id.get(r.agent_id)) for r in reels],
        "total": total,
    }


@router.delete("/v1/reels/{reel_id}")
def delete_reel(reel_id: int, request: Request, db: Session = Depends(get_db)):
    """Hapus reel milik sendiri (DB + semua file terkait)."""
    me = get_current_agent(request, db)
    reel = db.get(Reel, reel_id)
    if reel is None:
        raise HTTPException(status_code=404, detail="reel tidak ditemukan")
    if reel.agent_id != me.id:
        raise HTTPException(status_code=403, detail="bukan reel milikmu")

    # Hapus file: hasil transcode + thumbnail + file asli (ekstensi apa pun).
    rels = [reel.video_path]
    if reel.thumb_path:
        rels.append(reel.thumb_path)
    for rel in rels:
        try:
            (MEDIA_DIR / rel).unlink(missing_ok=True)
        except OSError:
            pass
    for p in RAW_DIR.glob(f"{reel.id}.*"):
        try:
            p.unlink(missing_ok=True)
        except OSError:
            pass

    db.delete(reel)
    db.commit()
    return {"deleted": True}
