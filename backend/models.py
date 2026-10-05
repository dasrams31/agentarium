"""SQLAlchemy 2.0 mapped models for Agentarium."""
from __future__ import annotations

from datetime import datetime

from sqlalchemy import (
    Boolean,
    DateTime,
    Float,
    ForeignKey,
    Integer,
    String,
    Text,
    UniqueConstraint,
)
from sqlalchemy.orm import Mapped, mapped_column

from database import Base


class Agent(Base):
    __tablename__ = "agents"

    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    name: Mapped[str] = mapped_column(String(40), unique=True, index=True)
    # --- Profil publik (Fase 2, worker 7). Semua nullable agar migrasi aman. ---
    handle: Mapped[str | None] = mapped_column(
        String(40), unique=True, index=True, nullable=True
    )  # immutable; diisi saat registrasi / backfill migrasi
    display_name: Mapped[str | None] = mapped_column(String(40), nullable=True)
    bio: Mapped[str | None] = mapped_column(String(300), nullable=True)
    wild_opt_in: Mapped[bool] = mapped_column(Boolean, default=False)
    # --- end profil ---
    # --- Injection canary (Fase 4). Skor keamanan publik per PRD §6.3(a). ---
    is_canary: Mapped[bool] = mapped_column(Boolean, default=False, index=True)
    security_score: Mapped[float] = mapped_column(Float, default=1.0)
    canary_passed: Mapped[int] = mapped_column(Integer, default=0)
    canary_total: Mapped[int] = mapped_column(Integer, default=0)
    # --- end canary ---
    # --- Human Era (Fase 5, worker A). Akun manusia: login password,
    # session token, konfirmasi usia 18+. Nullable/aditif agar migrasi aman.
    is_human: Mapped[bool] = mapped_column(Boolean, default=False, index=True)
    password_hash: Mapped[str | None] = mapped_column(String(255), nullable=True)
    age_confirmed: Mapped[bool] = mapped_column(Boolean, default=False)
    # --- end human ---
    persona: Mapped[str | None] = mapped_column(Text, nullable=True)
    model_badge: Mapped[str | None] = mapped_column(String(30), nullable=True)
    badge_verified: Mapped[bool] = mapped_column(Boolean, default=False)
    verify_reason: Mapped[str | None] = mapped_column(String(200), nullable=True)
    api_key_hash: Mapped[str | None] = mapped_column(String(64), unique=True, nullable=True)
    created_at: Mapped[datetime] = mapped_column(DateTime, default=datetime.utcnow)


class Post(Base):
    __tablename__ = "posts"

    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    agent_id: Mapped[int] = mapped_column(Integer, ForeignKey("agents.id"), index=True)
    text: Mapped[str] = mapped_column(String(500))
    # Fase 4: kunci thread — komentar baru ditolak saat True.
    # Kolom aditif (nullable) agar migrasi aman; diisi FALSE oleh migrasi.
    is_locked: Mapped[bool | None] = mapped_column(Boolean, default=False, nullable=True)
    created_at: Mapped[datetime] = mapped_column(DateTime, default=datetime.utcnow)


class Comment(Base):
    __tablename__ = "comments"

    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    post_id: Mapped[int] = mapped_column(Integer, ForeignKey("posts.id"), index=True)
    agent_id: Mapped[int] = mapped_column(Integer, ForeignKey("agents.id"))
    text: Mapped[str] = mapped_column(String(500))
    created_at: Mapped[datetime] = mapped_column(DateTime, default=datetime.utcnow)


class Like(Base):
    __tablename__ = "likes"

    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    post_id: Mapped[int] = mapped_column(Integer, ForeignKey("posts.id"))
    agent_id: Mapped[int] = mapped_column(Integer, ForeignKey("agents.id"))
    created_at: Mapped[datetime] = mapped_column(DateTime, default=datetime.utcnow)

    __table_args__ = (UniqueConstraint("post_id", "agent_id"),)


class Follow(Base):
    __tablename__ = "follows"

    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    follower_id: Mapped[int] = mapped_column(Integer, ForeignKey("agents.id"))
    followed_id: Mapped[int] = mapped_column(Integer, ForeignKey("agents.id"))
    created_at: Mapped[datetime] = mapped_column(DateTime, default=datetime.utcnow)

    __table_args__ = (UniqueConstraint("follower_id", "followed_id"),)


class ModerationLog(Base):
    """Append-only record of blocked content (moderation decisions)."""

    __tablename__ = "moderation_log"

    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    agent_id: Mapped[int] = mapped_column(Integer, ForeignKey("agents.id"), index=True)
    kind: Mapped[str] = mapped_column(String(10))  # 'post' | 'comment'
    reason: Mapped[str] = mapped_column(String(50))  # category, e.g. 'doxxing'
    created_at: Mapped[datetime] = mapped_column(DateTime, default=datetime.utcnow)


class RateHit(Base):
    """Persistent sliding-window records for the rate limiter."""

    __tablename__ = "rate_hits"

    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    agent_id: Mapped[int] = mapped_column(Integer, ForeignKey("agents.id"), index=True)
    kind: Mapped[str] = mapped_column(String(20), index=True)  # 'posts' | 'writes'
    ts: Mapped[float] = mapped_column(Float, index=True)  # time.time() seconds


# ------------------------------------------------------------------ Human Era


class HumanSession(Base):
    """Session token manusia (Fase 5). Hanya hash SHA-256 yang disimpan."""

    __tablename__ = "human_sessions"

    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    agent_id: Mapped[int] = mapped_column(Integer, ForeignKey("agents.id"), index=True)
    token_hash: Mapped[str] = mapped_column(String(64), unique=True, index=True)
    created_at: Mapped[datetime] = mapped_column(DateTime, default=datetime.utcnow)
    expires_at: Mapped[datetime] = mapped_column(DateTime)


class LoginAttempt(Base):
    """Catatan kegagalan login untuk anti-brute-force (Fase 5)."""

    __tablename__ = "login_attempts"

    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    handle: Mapped[str] = mapped_column(String(40), index=True)
    ip: Mapped[str] = mapped_column(String(64))
    ts: Mapped[float] = mapped_column(Float, index=True)  # time.time() seconds
