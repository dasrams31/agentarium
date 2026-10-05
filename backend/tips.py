"""Tipping for Agentarium — spectators can tip agents.

=====================================================================
JUJUR / HONEST: INI MOCK, BUKAN UANG SUNGGUHAN.
=====================================================================
Modul ini MENSIMULASIKAN pembayaran. Tidak ada panggilan ke payment
gateway mana pun, tidak ada uang sungguhan yang berpindah, tidak ada
entitas hukum yang menampung dana. Provider default `MockProvider`
hanya menulis status di database lokal.

Sebelum production sungguhan dibutuhkan:
  1. entitas hukum / rekening penampung yang jelas,
  2. kontrak dengan payment gateway (mis. Xendit / Midtrans / Stripe),
  3. implementasi PaymentProvider asli + webhook signature verification,
  4. hapus/ganti semua penanda "SIMULASI" di kode, docs, dan UI.
Lihat ~/workspace/agentarium/TIPPING.md.
"""
from __future__ import annotations

import time
import uuid
from abc import ABC, abstractmethod
from datetime import datetime

from fastapi import APIRouter, Depends, HTTPException, Request
from pydantic import BaseModel, Field
from sqlalchemy import DateTime, Float, ForeignKey, Integer, String, func
from sqlalchemy.exc import IntegrityError
from sqlalchemy.orm import Mapped, Session, mapped_column

import models
import moderation
import webhooks
from database import Base, get_db

# ------------------------------------------------------------------ revenue share
# KONSTANTA revenue share — satu-satunya tempat angka ini didefinisikan.
# 90% untuk agent penerima tip, 10% untuk operator platform.
AGENT_SHARE_PCT = 90
OPERATOR_SHARE_PCT = 10
assert AGENT_SHARE_PCT + OPERATOR_SHARE_PCT == 100, "share harus berjumlah 100%"

TIP_MIN_CENTS = 1_000          # Rp 10
TIP_MAX_CENTS = 100_000_000    # Rp 1.000.000
TIP_LABEL_MAX_LEN = 40

TIP_WINDOW_SECONDS = 3600
TIP_CHECKOUT_LIMIT_PER_HOUR = 20  # per IP

# Penanda mock yang WAJIB muncul di setiap respons checkout/confirm.
MOCK_NOTE = "SIMULASI — bukan pembayaran sungguhan"

# from_label berasal dari manusia penonton (bukan agent), jadi tidak ada
# agent_id yang valid untuk moderation log. 0 = sentinel "human spectator".
# ModerationLog.agent_id adalah FK ke agents.id; di Postgres insert dengan
# agent_id=0 akan melanggar FK — ditangani di _moderate_label().
HUMAN_SPECTATOR_AGENT_ID = 0


# ------------------------------------------------------------------ models

class Tip(Base):
    """Satu transaksi tip dari penonton ke agent. MOCK — bukan uang asli."""

    __tablename__ = "tips"

    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    to_agent_id: Mapped[int] = mapped_column(
        Integer, ForeignKey("agents.id"), index=True
    )
    from_label: Mapped[str | None] = mapped_column(
        String(40), nullable=True, default="anonim"
    )
    amount_cents: Mapped[int] = mapped_column(Integer)
    currency: Mapped[str] = mapped_column(String(3), default="IDR")
    provider: Mapped[str] = mapped_column(String(20), default="mockpay")
    provider_ref: Mapped[str | None] = mapped_column(String(64), nullable=True)
    status: Mapped[str] = mapped_column(String(12), default="pending")
    # "pending" | "completed" | "failed"
    agent_share_cents: Mapped[int] = mapped_column(Integer)
    operator_share_cents: Mapped[int] = mapped_column(Integer)
    created_at: Mapped[datetime] = mapped_column(DateTime, default=datetime.utcnow)
    completed_at: Mapped[datetime | None] = mapped_column(
        DateTime, nullable=True, default=None
    )


class TipHit(Base):
    """Sliding-window records untuk rate limit checkout per IP."""

    __tablename__ = "tip_hits"

    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    ip: Mapped[str] = mapped_column(String(45), index=True)
    ts: Mapped[float] = mapped_column(Float, index=True)


# ------------------------------------------------------------------ payment providers

class PaymentProvider(ABC):
    """Interface pluggable untuk payment gateway.

    Provider asli (Xendit/Midtrans/Stripe/...) mengimplementasikan dua method
    ini. Satu-satunya titik ganti provider: fungsi get_provider() di bawah.
    """

    name: str = "base"

    @abstractmethod
    def create_checkout(self, tip: Tip) -> dict:
        """Buat sesi checkout untuk tip berstatus pending.

        Mengembalikan dict dengan minimal kunci "provider_ref" (boleh None
        bila provider belum memberi referensi di tahap checkout).
        """

    @abstractmethod
    def confirm(self, tip: Tip) -> dict:
        """Konfirmasi bahwa tip sudah dibayar.

        Mengembalikan dict dengan minimal kunci "provider_ref".
        """


class MockProvider(PaymentProvider):
    """Provider SIMULASI — tidak memanggil apa pun di luar proses ini.

    confirm() langsung menandai tip lunas tanpa verifikasi pembayaran,
    karena tidak ada pembayaran sungguhan yang terjadi.
    """

    name = "mockpay"

    def create_checkout(self, tip: Tip) -> dict:
        return {"provider_ref": None, "status": "pending"}

    def confirm(self, tip: Tip) -> dict:
        return {
            "provider_ref": f"mock-{tip.id}-{uuid.uuid4().hex[:8]}",
            "status": "completed",
        }


def get_provider() -> PaymentProvider:
    """SATU TITIK GANTI PROVIDER.

    Saat payment gateway asli sudah dikontrak, ganti return value di sini,
    mis. `return XenditProvider(...)`. Tidak ada kode lain yang perlu diubah.
    """
    return MockProvider()


# ------------------------------------------------------------------ schemas

class TipCheckoutBody(BaseModel):
    to_agent_id: int
    amount_cents: int = Field(ge=TIP_MIN_CENTS, le=TIP_MAX_CENTS)
    from_label: str = Field(default="", max_length=TIP_LABEL_MAX_LEN)


# ------------------------------------------------------------------ helpers

def _split_shares(amount_cents: int) -> tuple[int, int]:
    """Bagi nominal menjadi (agent_share, operator_share) sesuai konstanta."""
    agent_share = (amount_cents * AGENT_SHARE_PCT) // 100
    return agent_share, amount_cents - agent_share


def _moderate_label(label: str, db: Session) -> None:
    """Moderasi from_label via moderation.check_text (kind="tip").

    Label ditulis manusia penonton, bukan agent — dicatat dengan
    HUMAN_SPECTATOR_AGENT_ID. Di Postgres insert log dengan agent_id=0
    melanggar FK, jadi IntegrityError di-fallback menjadi 422 yang sama.
    """
    try:
        moderation.check_text(label, HUMAN_SPECTATOR_AGENT_ID, db, kind="tip")
    except HTTPException:
        raise
    except IntegrityError:
        # Postgres FK violation saat mencatat log blokir untuk manusia.
        db.rollback()
        raise HTTPException(
            status_code=422,
            detail="konten diblokir: doxxing — lihat MODERATION.md",
        )


def _tip_rate_limit(db: Session, ip: str) -> None:
    """Rate limit checkout: maks TIP_CHECKOUT_LIMIT_PER_HOUR per jam per IP.

    Pola sama seperti ratelimit.py: prune baris kedaluwarsa, hitung sisa,
    insert pada sukses. Check yang diblokir TIDAK mengonsumsi kuota.
    """
    now = time.time()
    cutoff = now - TIP_WINDOW_SECONDS
    db.query(TipHit).filter(
        TipHit.ip == ip,
        TipHit.ts <= cutoff,
    ).delete(synchronize_session=False)
    count = (
        db.query(TipHit)
        .filter(TipHit.ip == ip, TipHit.ts > cutoff)
        .count()
    )
    if count >= TIP_CHECKOUT_LIMIT_PER_HOUR:
        db.rollback()  # buang prune agar tidak setengah teraplikasi
        raise HTTPException(
            status_code=429,
            detail=f"rate limit exceeded: maks {TIP_CHECKOUT_LIMIT_PER_HOUR} checkout/jam per IP",
        )
    db.add(TipHit(ip=ip, ts=now))
    db.commit()


def _confirm_payload(tip: Tip, provider_name: str) -> dict:
    return {
        "tip_id": tip.id,
        "to_agent_id": tip.to_agent_id,
        "status": tip.status,
        "provider": provider_name,
        "provider_ref": tip.provider_ref,
        "amount_cents": tip.amount_cents,
        "agent_share_cents": tip.agent_share_cents,
        "operator_share_cents": tip.operator_share_cents,
        "currency": tip.currency,
        "completed_at": tip.completed_at.isoformat() if tip.completed_at else None,
        "mock_note": MOCK_NOTE,
    }


# ------------------------------------------------------------------ router (publik, tanpa API key)

router = APIRouter()


@router.post("/v1/tips/checkout", status_code=201)
def checkout_tip(
    body: TipCheckoutBody, request: Request, db: Session = Depends(get_db)
):
    """Mulai tip SIMULASI ke seorang agent. Penonton manusia tidak butuh key."""
    agent = (
        db.query(models.Agent).filter(models.Agent.id == body.to_agent_id).first()
    )
    if agent is None:
        raise HTTPException(status_code=404, detail="agent not found")
    # moderasi SEBELUM rate limit: konten diblokir tidak mengonsumsi kuota
    if body.from_label:
        _moderate_label(body.from_label, db)
    ip = request.client.host if request.client else "unknown"
    _tip_rate_limit(db, ip)

    provider = get_provider()
    agent_share, operator_share = _split_shares(body.amount_cents)
    tip = Tip(
        to_agent_id=agent.id,
        from_label=body.from_label or "anonim",
        amount_cents=body.amount_cents,
        currency="IDR",
        provider=provider.name,
        status="pending",
        agent_share_cents=agent_share,
        operator_share_cents=operator_share,
    )
    db.add(tip)
    db.flush()  # tip.id tersedia untuk provider bila dibutuhkan
    info = provider.create_checkout(tip)
    tip.provider_ref = info.get("provider_ref")
    db.commit()
    db.refresh(tip)
    return {
        "tip_id": tip.id,
        "status": "pending",
        "provider": provider.name,
        "mock_note": MOCK_NOTE,
    }


@router.post("/v1/tips/{tip_id}/confirm")
def confirm_tip(tip_id: int, db: Session = Depends(get_db)):
    """SIMULASI penonton menyelesaikan pembayaran mock.

    Idempoten: confirm dua kali (atau lebih) tetap mengembalikan completed
    dengan provider_ref yang sama — tidak ada double-counting.
    """
    tip = db.query(Tip).filter(Tip.id == tip_id).first()
    if tip is None:
        raise HTTPException(status_code=404, detail="tip not found")
    provider = get_provider()
    if tip.status == "completed":
        return _confirm_payload(tip, provider.name)
    info = provider.confirm(tip)
    tip.status = "completed"
    tip.provider_ref = info.get("provider_ref") or tip.provider_ref
    tip.completed_at = datetime.utcnow()
    db.commit()
    db.refresh(tip)
    # Fase 4: transisi pending -> completed -> event tip.received untuk
    # penerima. (Confirm idempoten: hanya transisi nyata yang memicu event.)
    webhooks.emit_event(
        db,
        "tip.received",
        tip.to_agent_id,
        {
            "tip_id": tip.id,
            "amount_cents": tip.amount_cents,
            "currency": tip.currency,
            "from_label": tip.from_label,
            "agent_share_cents": tip.agent_share_cents,
            "provider": provider.name,
        },
    )
    return _confirm_payload(tip, provider.name)


@router.get("/v1/agents/{agent_id}/tips/summary")
def tip_summary(agent_id: int, db: Session = Depends(get_db)):
    """Ringkasan tip SELESAI (completed) untuk seorang agent. Publik."""
    agent = db.query(models.Agent).filter(models.Agent.id == agent_id).first()
    if agent is None:
        raise HTTPException(status_code=404, detail="agent not found")
    count, total = (
        db.query(func.count(Tip.id), func.coalesce(func.sum(Tip.amount_cents), 0))
        .filter(Tip.to_agent_id == agent.id, Tip.status == "completed")
        .one()
    )
    return {
        "agent_id": agent.id,
        "count": count,
        "total_cents": int(total),
        "currency": "IDR",
    }
