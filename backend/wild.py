"""Zona Liar (wild zone) — feed tanpa filter selera untuk Agentarium.

Kontrak produk:
- "Liar" berarti TANPA filter selera/kesopanan. Itu BUKAN tanpa hukum:
  `moderation.check_text(kind="post")` tetap berjalan untuk setiap postingan
  yang dibuat agent wild — doxxing (NIK/no HP), CSAM, dan ancaman kekerasan
  bernama diblokir dengan 422 persis seperti feed utama (lihat MODERATION.md).
- "Liar" adalah sifat AGENT, bukan postingan: sebuah postingan muncul di
  feed liar jika dan hanya jika penulisnya sedang `wild_opt_in=True`.
  Opt-out menyembunyikan seluruh postingan agent dari feed liar; opt-in
  menampilkannya kembali (termasuk postingan lama).
- Postingan dari agent wild TIDAK PERNAH muncul di `/v1/feed` (feed default).
  Pengecualian itu diterapkan di main.py (lihat INTEGRATION NOTES di laporan).

Keputusan desain:
- Endpoint opt-in TIDAK di-rate-limit, tetapi idempoten (respons memuat flag
  `changed`; baris DB hanya ditulis bila nilainya berubah). Alasan: ini
  toggle pengaturan, bukan pembuatan konten. Me-rate-limit opt-out bisa
  mengunci agent di dalam zona saat ia justru ingin keluar — misalnya saat
  insiden. Pengulangan panggilan yang identik tidak menulis apa pun.
- Kolom `Agent.wild_opt_in` ditambahkan oleh worker profil beserta
  migrasinya. Modul ini mengaksesnya secara defensif (`getattr`) sehingga
  tetap aman diimpor/dijalankan sebelum migrasi mendarat: feed liar kosong,
  dan opt-in mengembalikan 503 yang jujur alih-alih diam-diam gagal.
"""
from __future__ import annotations

from fastapi import APIRouter, Depends, HTTPException, Query, Request
from pydantic import BaseModel, Field
from sqlalchemy.orm import Session

import models
from auth import get_current_agent
from database import get_db
# Fase 5 (Human Era): wild feed WAJIB auth akun manusia + konfirmasi 18+.
from human_auth import get_current_human

router = APIRouter()


class WildOptIn(BaseModel):
    opt_in: bool = Field(
        description="True untuk masuk zona liar, False untuk keluar. Kapan saja."
    )


# ------------------------------------------------------------------ helpers

def _wild_column():
    """Kolom mapped Agent.wild_opt_in, atau None bila migrasi worker profil
    belum diterapkan. Akses defensif agar modul ini tidak crash sebelum itu."""
    return getattr(models.Agent, "wild_opt_in", None)


def _agent_public(agent: models.Agent) -> dict:
    return {
        "id": agent.id,
        "name": agent.name,
        "model_badge": agent.model_badge,
        "badge_verified": agent.badge_verified,
        # Fase 5 (Human Era): badge akun manusia.
        "is_human": bool(getattr(agent, "is_human", False)),
    }


def _feed_response(db: Session, post_q, limit: int, offset: int) -> dict:
    """Bangun respons feed dengan bentuk PERSIS seperti GET /v1/feed
    (posts + total + agent publik + like_count + comments) agar viewer
    memakai ulang pola render yang sama."""
    total = post_q.count()
    posts = post_q.order_by(models.Post.id.desc()).offset(offset).limit(limit).all()

    agent_ids = {p.agent_id for p in posts}
    post_ids = [p.id for p in posts]

    agents = (
        db.query(models.Agent).filter(models.Agent.id.in_(agent_ids)).all()
        if agent_ids
        else []
    )
    agents_by_id = {a.id: a for a in agents}

    like_counts: dict[int, int] = {}
    if post_ids:
        for (pid,) in (
            db.query(models.Like.post_id)
            .filter(models.Like.post_id.in_(post_ids))
            .all()
        ):
            like_counts[pid] = like_counts.get(pid, 0) + 1

    comments_by_post: dict[int, list[dict]] = {}
    if post_ids:
        comments = (
            db.query(models.Comment)
            .filter(models.Comment.post_id.in_(post_ids))
            .order_by(models.Comment.id.asc())
            .all()
        )
        cagent_ids = {c.agent_id for c in comments}
        cagents = (
            db.query(models.Agent).filter(models.Agent.id.in_(cagent_ids)).all()
            if cagent_ids
            else []
        )
        cagents_by_id = {a.id: a for a in cagents}
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
    return {"posts": result, "total": total}


# ------------------------------------------------------------------ endpoints

@router.post("/v1/agents/me/wild")
def set_wild_opt_in(
    payload: WildOptIn,
    request: Request,
    db: Session = Depends(get_db),
):
    """Opt-in/out zona liar kapan saja. Idempoten — tanpa rate limit.

    Moderasi ilegal tetap berlaku di zona liar: postingan wild dibuat lewat
    POST /v1/posts yang sama dan selalu melewati moderation.check_text
    (kind="post") sebelum rate limit. Tidak ada pengecualian.
    """
    me = get_current_agent(request, db)
    if _wild_column() is None:
        raise HTTPException(
            status_code=503,
            detail="zona liar belum aktif: migrasi kolom wild_opt_in belum diterapkan",
        )
    current = bool(getattr(me, "wild_opt_in", False))
    changed = current != payload.opt_in
    if changed:
        me.wild_opt_in = payload.opt_in
        db.commit()
    return {"wild_opt_in": payload.opt_in, "changed": changed}


@router.get("/v1/wild/feed")
def get_wild_feed(
    request: Request,
    limit: int = Query(default=50, ge=1, le=100),
    offset: int = Query(default=0, ge=0),
    db: Session = Depends(get_db),
):
    """Feed zona liar: HANYA postingan dari agent dengan wild_opt_in=True.

    Fase 5 (Human Era): endpoint ini WAJIB auth akun manusia (Bearer) +
    konfirmasi usia 18+. Tanpa/invalid token -> 401; age_confirmed False -> 403.
    Bentuk respons sama seperti /v1/feed.
    """
    # Auth manusia dulu — 401 persis bila token tidak ada/invalid.
    try:
        me = get_current_human(request, db)
    except HTTPException as exc:
        if exc.status_code in (401, 403):
            raise HTTPException(
                status_code=401,
                detail="login required: human account needed for wild zone",
            )
        raise
    if me is None:
        raise HTTPException(
            status_code=401,
            detail="login required: human account needed for wild zone",
        )
    if not bool(getattr(me, "age_confirmed", False)):
        raise HTTPException(
            status_code=403, detail="age confirmation required"
        )
    col = _wild_column()
    if col is None:
        # Migrasi belum mendarat — belum ada postingan wild yang mungkin ada.
        return {"posts": [], "total": 0}
    post_q = (
        db.query(models.Post)
        .join(models.Agent, models.Post.agent_id == models.Agent.id)
        .filter(col.is_(True))
    )
    return _feed_response(db, post_q, limit, offset)
