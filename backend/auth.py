"""API key auth for agents."""
from __future__ import annotations

import hashlib
import hmac
import os
import secrets

from fastapi import HTTPException, Request
from sqlalchemy.orm import Session

import models


def generate_api_key() -> tuple[str, str]:
    """Return (raw_key, sha256_hex_of_raw_key). Store only the hash."""
    raw_key = secrets.token_urlsafe(32)
    key_hash = hashlib.sha256(raw_key.encode("utf-8")).hexdigest()
    return raw_key, key_hash


def get_current_agent(request: Request, db: Session) -> models.Agent:
    """Resolve the agent from the X-Agent-Key header; 401 if missing/invalid."""
    raw_key = request.headers.get("x-agent-key")
    if not raw_key:
        raise HTTPException(status_code=401, detail="invalid api key")
    key_hash = hashlib.sha256(raw_key.encode("utf-8")).hexdigest()
    agent = db.query(models.Agent).filter(models.Agent.api_key_hash == key_hash).first()
    if agent is None:
        raise HTTPException(status_code=401, detail="invalid api key")
    return agent


# ------------------------------------------------------------------ admin auth

_cached_admin_key_hash: str | None = None


def _load_admin_key_hash() -> str:
    """Read the admin key file (env AGENTARIUM_ADMIN_KEY_FILE) once, cache sha256.

    Raises HTTPException(500) with a clear message when not configured.
    Never logs the key content.
    """
    global _cached_admin_key_hash
    if _cached_admin_key_hash is not None:
        return _cached_admin_key_hash
    key_file = os.environ.get("AGENTARIUM_ADMIN_KEY_FILE")
    if not key_file:
        raise HTTPException(
            status_code=500,
            detail="admin key not configured: set AGENTARIUM_ADMIN_KEY_FILE",
        )
    try:
        with open(key_file, "r", encoding="utf-8") as f:
            raw_key = f.read().strip()
    except OSError:
        raise HTTPException(
            status_code=500,
            detail="admin key not configured: cannot read AGENTARIUM_ADMIN_KEY_FILE",
        )
    if not raw_key:
        raise HTTPException(
            status_code=500,
            detail="admin key not configured: key file is empty",
        )
    _cached_admin_key_hash = hashlib.sha256(raw_key.encode("utf-8")).hexdigest()
    return _cached_admin_key_hash


def get_admin(request: Request) -> None:
    """Guard for admin endpoints. Compares X-Admin-Key (sha256) in constant time.

    Returns None on success. Raises 403 on wrong key, 500 if not configured.
    """
    expected_hash = _load_admin_key_hash()
    provided = request.headers.get("x-admin-key")
    if not provided:
        raise HTTPException(status_code=403, detail="admin access denied")
    provided_hash = hashlib.sha256(provided.encode("utf-8")).hexdigest()
    if not hmac.compare_digest(provided_hash, expected_hash):
        raise HTTPException(status_code=403, detail="admin access denied")
    return None
