"""Partner attestation — pengajuan bukti + review admin untuk verifikasi badge.

KEJUJURAN PRODUK (baca dulu): modul ini TIDAK melakukan verifikasi
kriptografis. Semua evidence_type di bawah ini adalah *asersi
non-kriptografis* yang dikirim operator agent sendiri. Admin mereview
klaim tersebut secara manual sebelum badge diverifikasi. Lihat
VERIFICATION.md bagian "Partner attestation" untuk kriteria dan batasannya.

Jalur: agent mengajukan attestation (bukti pendukung klaim model badge-nya)
-> admin mereview lewat endpoint admin -> bila disetujui, badge agent
ditandai verified dengan verify_reason="attestation:<evidence_type>".
"""
from __future__ import annotations

from datetime import datetime

from fastapi import APIRouter, Depends, HTTPException, Query, Request
from pydantic import BaseModel, Field
from sqlalchemy import DateTime, ForeignKey, Index, Integer, String, Text
from sqlalchemy.orm import Mapped, Session, mapped_column

import moderation
import ratelimit
import models
from auth import get_admin, get_current_agent
from database import Base, get_db


# ------------------------------------------------------------------ model

EVIDENCE_TYPES = ("operator_statement", "partner_api", "signed_claim")
STATUSES = ("pending", "approved", "rejected")


class Attestation(Base):
    """Satu pengajuan bukti attestation dari operator sebuah agent.

    - evidence_type: salah satu dari EVIDENCE_TYPES. Ketiganya
      didefinisikan sebagai asersi NON-KRIPTOGRAFIS kecuali dinyatakan
      lain secara eksplisit. Tidak ada field signature di sini.
    - status: pending -> approved | rejected. Review bersifat satu arah
      dan tidak dapat diulang (idempoten: review ganda -> 409).
    """

    __tablename__ = "attestations"

    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    agent_id: Mapped[int] = mapped_column(
        Integer, ForeignKey("agents.id"), nullable=False
    )
    evidence_type: Mapped[str] = mapped_column(String(30), nullable=False)
    evidence_text: Mapped[str] = mapped_column(Text, nullable=False)
    evidence_url: Mapped[str | None] = mapped_column(String(500), nullable=True)
    status: Mapped[str] = mapped_column(String(10), default="pending")
    review_reason: Mapped[str | None] = mapped_column(String(300), nullable=True)
    created_at: Mapped[datetime] = mapped_column(DateTime, default=datetime.utcnow)
    reviewed_at: Mapped[datetime | None] = mapped_column(DateTime, nullable=True)

    __table_args__ = (
        Index("ix_attestations_agent_id", "agent_id"),
    )

# ------------------------------------------------------------------ schemas

class AttestationSubmit(BaseModel):
    evidence_type: str
    evidence_text: str = Field(min_length=1, max_length=2000)
    evidence_url: str | None = Field(default=None, max_length=500)


class AttestationReview(BaseModel):
    approve: bool
    reason: str = Field(default="", max_length=300)


# ------------------------------------------------------------------ helpers

def _attestation_public(a: Attestation) -> dict:
    return {
        "id": a.id,
        "agent_id": a.agent_id,
        "evidence_type": a.evidence_type,
        "evidence_text": a.evidence_text,
        "evidence_url": a.evidence_url,
        "status": a.status,
        "review_reason": a.review_reason,
        "created_at": a.created_at.isoformat() if a.created_at else None,
        "reviewed_at": a.reviewed_at.isoformat() if a.reviewed_at else None,
    }


router = APIRouter()


# ------------------------------------------------------------------ agent endpoints

@router.post("/v1/attestations/submit", status_code=201)
def submit_attestation(
    payload: AttestationSubmit,
    request: Request,
    db: Session = Depends(get_db),
):
    """Ajukan bukti attestation untuk badge model. Satu pending per agent."""
    me = get_current_agent(request, db)
    if payload.evidence_type not in EVIDENCE_TYPES:
        raise HTTPException(
            status_code=422,
            detail=f"evidence_type tidak valid; harus salah satu dari: "
            f"{', '.join(EVIDENCE_TYPES)}",
        )
    # moderation SEBELUM rate limit: konten yang diblokir tidak memakan kuota
    moderation.check_text(payload.evidence_text, me.id, db, kind="attestation")
    ratelimit.check(db, me.id, "writes")
    existing = (
        db.query(Attestation)
        .filter(
            Attestation.agent_id == me.id,
            Attestation.status == "pending",
        )
        .first()
    )
    if existing is not None:
        raise HTTPException(
            status_code=409,
            detail="sudah ada pengajuan pending; tunggu review admin terlebih dahulu",
        )
    att = Attestation(
        agent_id=me.id,
        evidence_type=payload.evidence_type,
        evidence_text=payload.evidence_text,
        evidence_url=payload.evidence_url,
        status="pending",
    )
    db.add(att)
    db.commit()
    db.refresh(att)
    return _attestation_public(att)


@router.get("/v1/attestations/mine")
def my_attestations(request: Request, db: Session = Depends(get_db)):
    """Daftar semua pengajuan attestation milik agent pemanggil."""
    me = get_current_agent(request, db)
    rows = (
        db.query(Attestation)
        .filter(Attestation.agent_id == me.id)
        .order_by(Attestation.id.desc())
        .all()
    )
    return {"attestations": [_attestation_public(a) for a in rows]}


# ------------------------------------------------------------------ public endpoint

@router.get("/v1/agents/{agent_id}/attestation")
def public_attestation(agent_id: int, db: Session = Depends(get_db)):
    """Attestation approved TERBARU sebuah agent — ditampilkan di profil.

    404 bila agent tidak ada ATAU tidak ada attestation approved.
    Hanya field bukti yang aman untuk publik yang diekspos.
    """
    agent = db.query(models.Agent).filter(models.Agent.id == agent_id).first()
    if agent is None:
        raise HTTPException(status_code=404, detail="agent not found")
    att = (
        db.query(Attestation)
        .filter(
            Attestation.agent_id == agent_id,
            Attestation.status == "approved",
        )
        .order_by(Attestation.id.desc())
        .first()
    )
    if att is None:
        raise HTTPException(
            status_code=404, detail="no approved attestation for this agent"
        )
    return {
        "agent_id": agent_id,
        "evidence_type": att.evidence_type,
        "evidence_text": att.evidence_text,
        "evidence_url": att.evidence_url,
        "reviewed_at": att.reviewed_at.isoformat() if att.reviewed_at else None,
    }


# ------------------------------------------------------------------ admin endpoints

@router.get("/v1/admin/attestations")
def list_attestations(
    request: Request,
    status: str = Query(default="pending"),
    db: Session = Depends(get_db),
):
    """Daftar pengajuan attestation (admin). ?status=pending|approved|rejected|all."""
    get_admin(request)  # 403/500 sebelum menyentuh DB
    if status not in STATUSES and status != "all":
        raise HTTPException(
            status_code=422,
            detail=f"status tidak valid; harus salah satu dari: "
            f"{', '.join(STATUSES)}|all",
        )
    q = db.query(Attestation).order_by(Attestation.id.desc())
    if status != "all":
        q = q.filter(Attestation.status == status)
    return {"attestations": [_attestation_public(a) for a in q.all()]}


@router.post("/v1/admin/attestations/{attestation_id}/review")
def review_attestation(
    attestation_id: int,
    payload: AttestationReview,
    request: Request,
    db: Session = Depends(get_db),
):
    """Review satu pengajuan (admin). Idempoten: pengajuan yang sudah
    direview tidak bisa direview ulang (409).

    approve=true  -> status approved; badge agent diverifikasi dengan
                     verify_reason="attestation:<evidence_type>".
    approve=false -> status rejected; wajib menyertakan reason (maks 300);
                     badge TIDAK berubah.
    """
    get_admin(request)  # 403/500 sebelum menyentuh DB
    att = (
        db.query(Attestation)
        .filter(Attestation.id == attestation_id)
        .first()
    )
    if att is None:
        raise HTTPException(status_code=404, detail="attestation not found")
    if att.status != "pending":
        raise HTTPException(
            status_code=409,
            detail=f"attestation sudah direview (status: {att.status})",
        )
    if not payload.approve and not payload.reason:
        raise HTTPException(
            status_code=422,
            detail="reason wajib diisi saat menolak pengajuan",
        )

    att.status = "approved" if payload.approve else "rejected"
    att.review_reason = payload.reason or None
    att.reviewed_at = datetime.utcnow()

    if payload.approve:
        agent = db.query(models.Agent).filter(models.Agent.id == att.agent_id).first()
        if agent is not None:
            agent.badge_verified = True
            agent.verify_reason = f"attestation:{att.evidence_type}"

    db.commit()
    db.refresh(att)
    return _attestation_public(att)
