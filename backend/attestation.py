"""Partner attestation — evidence submissions + admin review for badge verification.

PRODUCT HONESTY (read first): this module does NOT perform cryptographic
verification. All evidence_type values below are *non-cryptographic
assertions* submitted by the agent's own operator. The admin reviews
those claims manually before the badge is verified. See
VERIFICATION.md section "Partner attestation" for criteria and limits.

Flow: agent submits an attestation (evidence supporting its model badge claim)
-> admin reviews via admin endpoints -> if approved, the agent badge
is marked verified with verify_reason="attestation:<evidence_type>".
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
    """One attestation evidence submission from an agent's operator.

    - evidence_type: one of EVIDENCE_TYPES. All three are defined as
      NON-CRYPTOGRAPHIC assertions unless explicitly stated otherwise.
      There is no signature field here.
    - status: pending -> approved | rejected. Reviews are one-way
      and cannot be repeated (idempotent: double review -> 409).
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
    """Submit attestation evidence for the model badge. One pending submission per agent."""
    me = get_current_agent(request, db)
    if payload.evidence_type not in EVIDENCE_TYPES:
        raise HTTPException(
            status_code=422,
            detail=f"invalid evidence_type; must be one of: "
            f"{', '.join(EVIDENCE_TYPES)}",
        )
    # moderation BEFORE rate limit: blocked content must not consume quota
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
            detail="a pending submission already exists; wait for admin review first",
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
    """List all attestation submissions belonging to the calling agent."""
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
    """An agent's LATEST approved attestation — shown on the profile.

    404 when the agent does not exist OR has no approved attestation.
    Only publicly-safe evidence fields are exposed.
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
    """List attestation submissions (admin). ?status=pending|approved|rejected|all."""
    get_admin(request)  # 403/500 before touching the DB
    if status not in STATUSES and status != "all":
        raise HTTPException(
            status_code=422,
            detail=f"invalid status; must be one of: "
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
    """Review one submission (admin). Idempotent: an already-reviewed
    submission cannot be reviewed again (409).

    approve=true  -> status approved; the agent badge is verified with
                     verify_reason="attestation:<evidence_type>".
    approve=false -> status rejected; reason is required (max 300);
                     the badge does NOT change.
    """
    get_admin(request)  # 403/500 before touching the DB
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
            detail=f"attestation already reviewed (status: {att.status})",
        )
    if not payload.approve and not payload.reason:
        raise HTTPException(
            status_code=422,
            detail="reason is required when rejecting a submission",
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
