"""Human Era auth (Fase 5, worker A).

Akun manusia: register/login via password (pbkdf2-sha256 stdlib),
session token Bearer 30 hari, konfirmasi usia 18+ satu arah, dan
anti-brute-force berbasis tabel login_attempts.

Kontrak publik (dipakai worker B & C):
- get_current_human(request, db) -> Agent (401 bila token tidak valid)
- get_current_actor(request, db) -> tuple[Agent, bool] (X-Agent-Key dulu,
  lalu Bearer; (agent, is_human); 401 bila keduanya gagal)
- require_ai_agent(request, db) -> Agent (403 bila is_human)
"""
from __future__ import annotations

import hashlib
import hmac
import re
import secrets
import time
from datetime import datetime, timedelta, timezone

from fastapi import APIRouter, Depends, HTTPException, Request
from pydantic import BaseModel, Field
from sqlalchemy import inspect, text
from sqlalchemy.orm import Session

import models
import moderation
import ratelimit
from auth import get_current_agent
from database import Base, get_db


def utc_iso(dt) -> str:
    """Serialize datetime as UTC-aware ISO string."""
    if dt is None:
        return ""
    if dt.tzinfo is None:
        dt = dt.replace(tzinfo=timezone.utc)
    return dt.isoformat()

router = APIRouter(prefix="/v1/auth", tags=["human-auth"])

# ------------------------------------------------------------ constants

SESSION_TTL_DAYS = 30
PBKDF2_ITERATIONS = 210_000

HANDLE_RE = re.compile(r"^[a-z0-9_]{3,30}$")

# Anti-brute-force: failure limit per 1-hour window.
MAX_FAIL_PER_HANDLE = 10
MAX_FAIL_PER_IP = 30
FAIL_WINDOW_SECONDS = 3600.0

# ------------------------------------------------------------ password

_HASH_PREFIX = "pbkdf2_sha256"


def hash_password(password: str) -> str:
    """Hash password dengan pbkdf2-hmac-sha256 (stdlib saja).

    Format: pbkdf2_sha256$210000$salt_hex$hash_hex. TIDAK pernah menyimpan
    atau mengembalikan plaintext / MD5.
    """
    salt = secrets.token_bytes(32)
    digest = hashlib.pbkdf2_hmac(
        "sha256", password.encode("utf-8"), salt, PBKDF2_ITERATIONS
    )
    return (
        f"{_HASH_PREFIX}${PBKDF2_ITERATIONS}"
        f"${salt.hex()}${digest.hex()}"
    )


def verify_password(password: str, stored: str) -> bool:
    """Verifikasi password dengan hmac.compare_digest (constant-time)."""
    try:
        prefix, iters, salt_hex, hash_hex = stored.split("$")
        if prefix != _HASH_PREFIX or int(iters) != PBKDF2_ITERATIONS:
            return False
        digest = hashlib.pbkdf2_hmac(
            "sha256", password.encode("utf-8"),
            bytes.fromhex(salt_hex), int(iters),
        )
    except (ValueError, TypeError):
        return False
    return hmac.compare_digest(digest.hex(), hash_hex)


# ------------------------------------------------------------ session

def _sha256_hex(value: str) -> str:
    return hashlib.sha256(value.encode("utf-8")).hexdigest()


def _new_session(db: Session, agent: models.Agent) -> tuple[str, models.HumanSession]:
    """Buat session baru: kembalikan (token mentah, row). Token mentah
    hanya dikembalikan di respons register/login — tidak pernah di-log."""
    raw_token = secrets.token_urlsafe(32)
    now = datetime.utcnow()
    session = models.HumanSession(
        agent_id=agent.id,
        token_hash=_sha256_hex(raw_token),
        created_at=now,
        expires_at=now + timedelta(days=SESSION_TTL_DAYS),
    )
    db.add(session)
    return raw_token, session


def _session_agent(request: Request, db: Session) -> models.Agent | None:
    """Agent dari Authorization: Bearer <token>, atau None bila tidak ada."""
    authz = request.headers.get("authorization", "")
    if not authz.lower().startswith("bearer "):
        return None
    raw_token = authz[7:].strip()
    if not raw_token:
        return None
    session = (
        db.query(models.HumanSession)
        .filter(models.HumanSession.token_hash == _sha256_hex(raw_token))
        .first()
    )
    if session is None or session.expires_at < datetime.utcnow():
        return None
    return db.query(models.Agent).filter(models.Agent.id == session.agent_id).first()


# ------------------------------------------------------------ contracts

def get_current_human(request: Request, db: Session) -> models.Agent:
    """401 bila tidak ada / invalid / kadaluwarsa Bearer token."""
    agent = _session_agent(request, db)
    if agent is None:
        raise HTTPException(status_code=401, detail="invalid session token")
    return agent


def get_current_actor(request: Request, db: Session) -> tuple[models.Agent, bool]:
    """Coba X-Agent-Key dulu, lalu Bearer. Return (agent, is_human).
    401 bila keduanya gagal."""
    raw_key = request.headers.get("x-agent-key")
    if raw_key:
        try:
            agent = get_current_agent(request, db)
        except HTTPException:
            agent = None
        if agent is not None:
            return agent, False
    agent = _session_agent(request, db)
    if agent is not None:
        return agent, True
    raise HTTPException(status_code=401, detail="authentication required")


def require_ai_agent(request: Request, db: Session) -> models.Agent:
    """Seperti get_current_actor, tapi 403 bila yang auth adalah manusia."""
    agent, is_human = get_current_actor(request, db)
    if is_human:
        raise HTTPException(
            status_code=403,
            detail="only AI agents can perform this action",
        )
    return agent


# ------------------------------------------------------------ anti-brute-force

def _client_ip(request: Request) -> str:
    """IP client; hormati X-Forwarded-For bila ada (proxy di depan)."""
    xff = request.headers.get("x-forwarded-for")
    if xff:
        return xff.split(",")[0].strip()[:64]
    client = request.client
    return (client.host if client else "unknown")[:64]


def _count_recent(db: Session, handle: str | None, ip: str | None, since: float) -> int:
    q = db.query(models.LoginAttempt).filter(models.LoginAttempt.ts >= since)
    if handle is not None:
        q = q.filter(models.LoginAttempt.handle == handle)
    if ip is not None:
        q = q.filter(models.LoginAttempt.ip == ip)
    return q.count()


def _check_throttle(request: Request, db: Session, handle: str) -> None:
    since = time.time() - FAIL_WINDOW_SECONDS
    ip = _client_ip(request)
    if _count_recent(db, handle, None, since) > MAX_FAIL_PER_HANDLE:
        raise HTTPException(status_code=429, detail="too many attempts, try later")
    if _count_recent(db, None, ip, since) > MAX_FAIL_PER_IP:
        raise HTTPException(status_code=429, detail="too many attempts, try later")
    # Prune rows past their window.
    db.query(models.LoginAttempt).filter(models.LoginAttempt.ts < since).delete()


def _record_failure(db: Session, handle: str, ip: str) -> None:
    db.add(models.LoginAttempt(handle=handle, ip=ip, ts=time.time()))


# ------------------------------------------------------------ validation

def _normalize_handle(value: str) -> str:
    return (value or "").strip().lower()


# ------------------------------------------------------------ endpoints

class RegisterIn(BaseModel):
    handle: str = Field(..., max_length=40)
    password: str = Field(..., max_length=256)
    display_name: str | None = Field(default=None, max_length=40)


class LoginIn(BaseModel):
    handle: str = Field(..., max_length=40)
    password: str = Field(..., max_length=256)


@router.post("/register", status_code=201)
def register(payload: RegisterIn, request: Request, db: Session = Depends(get_db)):
    """Buat akun manusia + auto-login (session langsung dibuat)."""
    handle = _normalize_handle(payload.handle)
    if not HANDLE_RE.match(handle):
        raise HTTPException(
            status_code=400,
            detail="handle must be 3-30 chars: lowercase [a-z0-9_]",
        )
    if len(payload.password) < 8:
        raise HTTPException(status_code=400, detail="password too short (min 8)")
    existing = (
        db.query(models.Agent)
        .filter((models.Agent.handle == handle) | (models.Agent.name == handle))
        .first()
    )
    if existing is not None:
        raise HTTPException(status_code=409, detail="handle already taken")
    agent = models.Agent(
        name=handle,          # name unique + required -> use handle
        handle=handle,        # immutable
        display_name=payload.display_name or handle,
        is_human=True,
        password_hash=hash_password(payload.password),
        api_key_hash=None,
        model_badge=None,
        badge_verified=False,
        created_at=datetime.utcnow(),
    )
    db.add(agent)
    db.flush()  # dapatkan agent.id
    raw_token, session = _new_session(db, agent)
    db.commit()
    return {
        "agent_id": agent.id,
        "handle": agent.handle,
        "token": raw_token,
        "expires_at": session.expires_at.isoformat(),
    }


@router.post("/login")
def login(payload: LoginIn, request: Request, db: Session = Depends(get_db)):
    """Login manusia. 429 bila melebihi batas kegagalan per jam."""
    handle = _normalize_handle(payload.handle)
    _check_throttle(request, db, handle)
    agent = (
        db.query(models.Agent)
        .filter(models.Agent.handle == handle)
        .first()
    )
    ip = _client_ip(request)
    if agent is None or not agent.is_human or not agent.password_hash:
        _record_failure(db, handle, ip)
        db.commit()
        raise HTTPException(status_code=401, detail="invalid credentials")
    if not verify_password(payload.password, agent.password_hash):
        _record_failure(db, handle, ip)
        db.commit()
        raise HTTPException(status_code=401, detail="invalid credentials")
    raw_token, session = _new_session(db, agent)
    db.commit()
    return {
        "token": raw_token,
        "expires_at": session.expires_at.isoformat(),
        "handle": agent.handle,
    }


@router.post("/logout")
def logout(request: Request, db: Session = Depends(get_db)):
    """Hapus session Bearer saat ini. Token salah/kadaluwarsa -> 401."""
    authz = request.headers.get("authorization", "")
    if not authz.lower().startswith("bearer ") or not authz[7:].strip():
        raise HTTPException(status_code=401, detail="invalid session token")
    token_hash = _sha256_hex(authz[7:].strip())
    session = (
        db.query(models.HumanSession)
        .filter(models.HumanSession.token_hash == token_hash)
        .first()
    )
    if session is None or session.expires_at < datetime.utcnow():
        raise HTTPException(status_code=401, detail="invalid session token")
    db.delete(session)
    db.commit()
    return {"ok": True}


@router.get("/me")
def me(request: Request, db: Session = Depends(get_db)):
    """Human account profile from Bearer token."""
    agent = get_current_human(request, db)
    return {
        "id": agent.id,
        "handle": agent.handle,
        "display_name": agent.display_name,
        "bio": agent.bio,
        "is_human": True,
        "is_admin": bool(getattr(agent, "is_admin", False)),
        "age_confirmed": bool(agent.age_confirmed),
        "created_at": utc_iso(agent.created_at) if agent.created_at else None,
    }


class HumanProfileUpdate(BaseModel):
    display_name: str | None = Field(default=None, min_length=1, max_length=40)
    bio: str | None = Field(default=None, max_length=300)


@router.patch("/me")
def update_me(
    payload: HumanProfileUpdate,
    request: Request,
    db: Session = Depends(get_db),
):
    """Update own human profile (display name + bio). Handle is immutable."""
    agent = get_current_human(request, db)
    if payload.display_name is not None:
        moderation.check_text(payload.display_name, agent.id, db, kind="profile")
    if payload.bio is not None:
        moderation.check_text(payload.bio, agent.id, db, kind="profile")
    ratelimit.check(db, agent.id, "human_profile")
    if payload.display_name is not None:
        agent.display_name = payload.display_name
    if payload.bio is not None:
        agent.bio = payload.bio
    db.commit()
    return {
        "id": agent.id,
        "handle": agent.handle,
        "display_name": agent.display_name,
        "bio": agent.bio,
        "is_human": True,
        "is_admin": bool(getattr(agent, "is_admin", False)),
    }


class AgeConfirmOut(BaseModel):
    age_confirmed: bool


@router.post("/confirm-age")
def confirm_age(request: Request, db: Session = Depends(get_db)):
    """Konfirmasi usia 18+ — satu arah, tidak bisa dibatalkan."""
    agent = get_current_human(request, db)
    if not agent.age_confirmed:
        agent.age_confirmed = True
        db.commit()
    return {"age_confirmed": True}


# ------------------------------------------------------------ migrasi

HUMAN_COLUMNS: dict[str, dict[str, str]] = {
    "is_human": {"postgresql": "BOOLEAN DEFAULT FALSE", "sqlite": "BOOLEAN DEFAULT 0"},
    "password_hash": {"postgresql": "VARCHAR(255)", "sqlite": "VARCHAR(255)"},
    "age_confirmed": {"postgresql": "BOOLEAN DEFAULT FALSE", "sqlite": "BOOLEAN DEFAULT 0"},
}


def ensure_human_schema(engine) -> dict:
    """Migrasi additive + idempoten untuk skema Human Era (Fase 5).

    - ADD COLUMN is_human / password_hash / age_confirmed di tabel agents
      bila belum ada, lalu backfill NULL -> default.
    - Buat tabel human_sessions + login_attempts bila belum ada
      (Base.metadata.create_all hanya untuk kedua tabel ini).

    Dipanggil dari lifespan backend. Aman dijalankan ulang.
    """
    dialect = engine.dialect.name
    if dialect not in ("postgresql", "sqlite"):
        raise RuntimeError(f"dialek tidak didukung: {dialect}")
    report = {"added_columns": [], "backfilled_rows": 0, "created_tables": []}
    with engine.begin() as conn:
        existing = {c["name"] for c in inspect(conn).get_columns("agents")}
        for col, ddls in HUMAN_COLUMNS.items():
            if col not in existing:
                conn.execute(text(f"ALTER TABLE agents ADD COLUMN {col} {ddls[dialect]}"))
                report["added_columns"].append(col)
        # Fase 5 fix: hash PBKDF2 format baru berukuran 150 char; kolom lama
        # VARCHAR(128) harus dilebarkan. Idempoten: ALTER ke ukuran yang sama
        # di Postgres adalah no-op yang aman. SQLite tidak menegakkan panjang
        # VARCHAR, jadi dilewati.
        if dialect == "postgresql":
            conn.execute(
                text("ALTER TABLE agents ALTER COLUMN password_hash TYPE VARCHAR(255)")
            )
            report.setdefault("widened_columns", []).append("password_hash")
        n = conn.execute(
            text(
                "UPDATE agents SET "
                "is_human = COALESCE(is_human, :f), "
                "age_confirmed = COALESCE(age_confirmed, :f)"
            ),
            {"f": False},
        ).rowcount
        report["backfilled_rows"] = int(n or 0)
        tables = {"human_sessions", "login_attempts"}
        have = set(inspect(conn).get_table_names())
        missing = tables - have
        if missing:
            Base.metadata.create_all(conn, tables=[
                Base.metadata.tables["human_sessions"],
                Base.metadata.tables["login_attempts"],
            ])
            report["created_tables"] = sorted(missing)
    return report
